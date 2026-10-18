"""Cached, read-only dependency checks, independent of RSS submissions and task progress."""

import hashlib
import logging
import re
import threading
import time
from copy import deepcopy

import requests
from defusedxml import ElementTree as ET
from requests.adapters import HTTPAdapter

from .db import now
from .downloaders import Aria2, client
from .network import fetch_bytes, upstream_session

log = logging.getLogger(__name__)


class DependencyHealth:
    interval = 60
    stale_after = 180

    def __init__(self, config):
        self.config = config
        self.lock = threading.Lock()
        self.check_lock = threading.Lock()
        self.wakeup = threading.Event()
        self.stopping = threading.Event()
        self.thread = None
        self.signature = None
        self.checked_at = None
        self.checked_monotonic = None
        self.checks = {}

    @staticmethod
    def _signature(cfg):
        return hashlib.sha256(cfg.model_dump_json().encode()).digest()

    def start(self):
        self.thread = threading.Thread(target=self._run, name="ani-health", daemon=True)
        self.thread.start()

    def stop(self):
        self.stopping.set()
        self.wakeup.set()
        if self.thread:
            self.thread.join(timeout=1)

    def _run(self):
        while not self.stopping.is_set():
            self.wakeup.clear()
            try:
                self.refresh()
            except Exception as exc:
                # An overdue or failed monitor must not continue reporting old successes.
                with self.lock:
                    self.checked_monotonic = None
                log.warning("依赖健康检查失败 (%s)", type(exc).__name__)
            self.wakeup.wait(self.interval)

    @staticmethod
    def _rss(cfg):
        source = cfg.source
        with upstream_session(source.proxy) as session:
            # Health reads do not retry or use the sync scheduler's request budget.
            for scheme in ("http://", "https://"):
                session.mount(scheme, HTTPAdapter(max_retries=0))
            xml = fetch_bytes(
                session,
                source.rss_link_torrent if source.api_type == "torrent" else source.rss_link,
                min(source.request_timeout, 5),
                source.max_feed_bytes,
            )
        root = ET.fromstring(xml)
        if root.tag != "rss" or root.find("channel") is None:
            raise ValueError("Invalid RSS")

    @staticmethod
    def _downloader(cfg):
        selected = cfg.downloaders[cfg.source.downloader]
        engine = client(selected.model_copy(update={"timeout": min(selected.timeout, 5)}))
        try:
            if isinstance(engine, Aria2):
                result = engine.rpc("getGlobalStat")
                if not isinstance(result, dict) or "numActive" not in result:
                    raise ValueError("Invalid aria2 response")
            else:
                response = engine.session.get(
                    engine.cfg.endpoint.rstrip("/") + "/api/v2/app/version", timeout=5
                )
                response.raise_for_status()
                if not re.fullmatch(r"v?\d+\.\d+[^\s<>]*", response.text.strip()):
                    raise ValueError("Invalid qBittorrent response")
        finally:
            engine.close()

    def refresh(self):
        if not self.check_lock.acquire(blocking=False):
            return
        try:
            cfg = self.config.get()
            signature = self._signature(cfg)
            checks = {}
            if cfg.source.enabled:
                for name, probe in (("rss", self._rss), ("downloader", self._downloader)):
                    try:
                        probe(cfg)
                        checks[name] = {"status": "ok"}
                    except requests.Timeout:
                        checks[name] = {"status": "warning", "code": name + "_timeout"}
                    except Exception:
                        # Never expose upstream error bodies, URLs, credentials or raw exception text.
                        checks[name] = {"status": "warning", "code": name + "_unavailable"}
            # A result for old endpoints/credentials must not clear a new configuration's warning.
            if signature == self._signature(self.config.get()):
                with self.lock:
                    self.signature = signature
                    self.checked_at = now()
                    self.checked_monotonic = time.monotonic()
                    self.checks = checks
        finally:
            self.check_lock.release()

    def snapshot(self):
        cfg = self.config.get()
        if not cfg.source.enabled:
            return {"status": "ok", "checks": {"dependencies": {"status": "disabled"}}}
        with self.lock:
            if self.signature != self._signature(cfg):
                return {"status": "warning", "checks": {"monitor": {"status": "warning", "code": "pending"}}}
            if self.checked_monotonic is None or time.monotonic() - self.checked_monotonic > self.stale_after:
                return {
                    "status": "warning",
                    "checked_at": self.checked_at,
                    "checks": {"monitor": {"status": "warning", "code": "stale"}},
                }
            checks = deepcopy(self.checks)
            return {
                "status": "warning" if any(c["status"] == "warning" for c in checks.values()) else "ok",
                "checked_at": self.checked_at,
                "checks": checks,
            }
