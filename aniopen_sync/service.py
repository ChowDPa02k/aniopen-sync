import hashlib
import json
import logging
import posixpath
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler

from .backup import create_backup
from .config import ConfigStore, Source, build_trigger
from .db import Database, later, now
from .downloaders import Aria2, MissingTask, QBittorrent, Rejected, client, identity_endpoint, safe_error
from .health import DependencyHealth
from .network import fetch_bytes, upstream_session
from .parser import AniParser, Release
from .retry import HttpDownloadRetry
from .storage import atomic_write
from .torrents import hash_aliases

log = logging.getLogger(__name__)


def close_safely(resource):
    if resource is not None:
        try:
            resource.close()
        except Exception as exc:
            log.warning("连接清理失败 (%s)", type(exc).__name__)


class Service:
    def __init__(self, config: ConfigStore, db: Database, data_dir: Path):
        self.config, self.db, self.data_dir = config, db, data_dir
        self.scheduler = BackgroundScheduler(timezone="UTC")
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ani-sync")
        self.sync_lock = threading.Lock()
        self.poll_lock = threading.Lock()
        self.settings_lock = threading.RLock()
        self.running = False
        self.stopping = threading.Event()
        self.mapping_status = {"state": "not_loaded"}
        self.observation_note = None
        self.dependency_health = DependencyHealth(config)
        self.download_retry = HttpDownloadRetry(config, db)

    def start(self):
        self.maintain()
        self.scheduler.start(paused=True)
        self.reschedule(self.config.get())
        self.scheduler.resume()
        self.dependency_health.start()
        cfg = self.config.get()
        if cfg.source.enabled and cfg.maintenance.sync_on_startup:
            self.trigger("startup")

    def stop(self):
        self.stopping.set()
        self.dependency_health.stop()
        if self.scheduler.running:
            self.scheduler.shutdown(wait=True)
        self.executor.shutdown(wait=True)

    def reschedule(self, cfg):
        with self.settings_lock:
            if cfg.schedule.enabled:
                self.scheduler.add_job(
                    self.trigger,
                    trigger=build_trigger(cfg.schedule),
                    id="sync",
                    replace_existing=True,
                    kwargs={"trigger_type": "scheduled"},
                    max_instances=1,
                    coalesce=cfg.schedule.coalesce,
                    misfire_grace_time=cfg.schedule.misfire_grace_time,
                )
            elif self.scheduler.get_job("sync"):
                self.scheduler.remove_job("sync")
            self.scheduler.add_job(
                self.poll,
                "interval",
                seconds=cfg.progress_poll_seconds,
                id="progress",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
                kwargs={"force": False},
            )
            self.scheduler.add_job(
                self.maintain,
                "interval",
                hours=6,
                id="maintenance",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )

    def maintain(self):
        policy = self.config.get().maintenance
        self.db.maintenance(policy)
        if policy.automatic_backups:
            directory = self.data_dir / "backups"
            target = directory / ("automatic-" + datetime.now(UTC).strftime("%Y-%m-%d") + ".tar.gz")
            try:
                create_backup(self.data_dir, target)
            except OSError as exc:
                log.warning("自动备份失败，请检查数据卷空间和权限 (%s)", type(exc).__name__)
            for path in sorted(directory.glob("automatic-*.tar.gz"), reverse=True)[policy.backup_keep :]:
                path.unlink()
            for path in sorted(directory.glob("history-v*.sqlite3"), reverse=True)[3:]:
                path.unlink()

    def save_config(self, cfg):
        with self.settings_lock:
            old = self.config.get()
            self.config.save(cfg)
            try:
                self.reschedule(cfg)
            except Exception:
                self.config.save(old)
                self.reschedule(old)
                raise
            self.dependency_health.wakeup.set()
            if not cfg.download_retry.enabled:
                self.db.cancel_download_retries()

    def status(self):
        cfg = self.config.get()
        job = self.scheduler.get_job("sync")
        return {
            "running": self.running,
            "source_enabled": cfg.source.enabled,
            "schedule_enabled": cfg.schedule.enabled,
            "timezone": cfg.schedule.timezone,
            "next_run": job.next_run_time.isoformat() if job and cfg.source.enabled else None,
            "summary": self.db.summary(),
            "runs": self.db.runs(),
            "mapping": self.mapping_status,
            "observation_note": self.observation_note,
            "download_retry_enabled": cfg.download_retry.enabled,
        }

    def trigger(self, trigger_type="manual"):
        if not self.sync_lock.acquire(blocking=False):
            return False
        self.running = True
        try:
            self.executor.submit(self._sync, trigger_type)
        except Exception:
            self.running = False
            self.sync_lock.release()
            raise
        return True

    @staticmethod
    def _validate_mappings(payload):
        if not isinstance(payload, dict):
            raise ValueError("映射数据不是对象")
        validated = Source(
            custom_season_mapping=payload.get("custom_season_mapping", {}),
            season_episode_adjustment=payload.get("season_episode_adjustment", {}),
        )
        values = validated.model_dump()
        return {key: values[key] for key in ("custom_season_mapping", "season_episode_adjustment")}

    def _mappings(self, cfg, session):
        if not cfg.online_mappings_enabled:
            self.mapping_status = {"state": "disabled"}
            return {}
        path = self.data_dir / "mappings-cache.json"
        try:
            raw = fetch_bytes(session, cfg.online_mappings_url, cfg.request_timeout, 4 * 1024 * 1024)
            payload = self._validate_mappings(json.loads(raw))
            metadata = {
                "url": cfg.online_mappings_url,
                "data": payload,
                "fetched_at": now(),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
            try:
                atomic_write(path, json.dumps(metadata, ensure_ascii=False))
            except OSError:
                log.warning("映射已获取，但无法更新缓存，请检查数据卷空间和权限")
            self.mapping_status = {key: value for key, value in metadata.items() if key != "data"} | {
                "state": "fresh"
            }
            return payload
        except Exception as exc:
            log.warning("在线映射不可用 (%s)，尝试同源有效缓存", type(exc).__name__)
            try:
                cached = json.loads(path.read_text())
                if cached["url"] != cfg.online_mappings_url:
                    raise ValueError("缓存来源不同")
                payload = self._validate_mappings(cached["data"])
                self.mapping_status = {key: value for key, value in cached.items() if key != "data"} | {
                    "state": "cached"
                }
                return payload
            except (OSError, ValueError, KeyError, TypeError):
                self.mapping_status = {"state": "unavailable"}
                return {}

    def _recovery_releases(self, cfg):
        releases, identities = [], {}
        for row in self.db.recoveries():
            downloader = cfg.downloaders.get(row["downloader"])
            if (
                not downloader
                or row["downloader"] != cfg.source.downloader
                or row["link_type"] != cfg.source.api_type
                or row["engine_endpoint"] != identity_endpoint(downloader)
            ):
                continue
            directory = row["target_directory"] or posixpath.dirname(row["target_path"])
            path = posixpath.relpath(directory, downloader.download_base_path)
            if path == ".." or path.startswith("../"):
                continue
            releases.append(
                Release(
                    row["original_url"] or row["source_url"],
                    row["source_url"],
                    row["original_name"],
                    row["file_name"],
                    row["show_title"],
                    row["season"],
                    row["episode"],
                    path,
                    rss_size_bytes=row["rss_size_bytes"],
                )
            )
            identities[row["source_url"]] = row["identity"]
        return releases, identities

    def _sync(self, trigger_type):
        run_id, created, skipped, failed = None, 0, 0, 0
        engine, session, init_error = None, None, None
        try:
            cfg = self.config.get()
            source = cfg.source
            session = upstream_session(source.proxy)
            run_id = self.db.insert(
                "runs", {"triggered_at": now(), "trigger_type": trigger_type, "status": "running"}
            )
            if not source.enabled:
                self.db.update("runs", run_id, status="disabled", finished_at=now())
                return
            xml = fetch_bytes(
                session,
                source.rss_link_torrent if source.api_type == "torrent" else source.rss_link,
                source.request_timeout,
                source.max_feed_bytes,
            )
            if self.stopping.is_set():
                raise InterruptedError()
            releases = AniParser(source, self._mappings(source, session)).parse(xml)
            if source.api_type == "http":
                self.db.backfill_rss_sizes({r.original_url: r.rss_size_bytes for r in releases})
            recoveries, recovery_identities = self._recovery_releases(cfg)
            releases = recoveries + [r for r in releases if r.url not in recovery_identities]
            downloader = cfg.downloaders[source.downloader]
            for release in releases:
                if self.stopping.is_set():
                    raise InterruptedError()
                legacy_urls = {release.url, release.original_url, release.identity}
                if any(
                    self.db.legacy_seen(hashlib.md5(url.encode()).hexdigest()) for url in legacy_urls if url
                ):
                    skipped += 1
                    continue
                identity = recovery_identities.get(release.url) or self.db.identity_for_url(
                    release.original_url or release.url, hashlib.sha256(release.identity.encode()).hexdigest()
                )
                directory = posixpath.normpath(posixpath.join(downloader.download_base_path, release.path))
                task_id = self.db.reserve_task(
                    {
                        "identity": identity,
                        "run_id": run_id,
                        "triggered_at": now(),
                        "trigger_type": trigger_type,
                        "original_name": release.original_name,
                        "file_name": release.file_name,
                        "show_title": release.show_title,
                        "season": release.season,
                        "episode": release.episode,
                        "downloader": source.downloader,
                        "engine_type": downloader.type,
                        "engine_endpoint": identity_endpoint(downloader),
                        "updated_at": now(),
                        "source_url": release.url,
                        "original_url": release.original_url or release.url,
                        "link_type": source.api_type,
                        "target_directory": directory,
                        "target_path": posixpath.join(directory, release.file_name),
                        "known_uris": json.dumps([release.url]),
                        "recovery_requested": 0,
                        **({"rss_size_bytes": release.rss_size_bytes} if release.rss_size_bytes else {}),
                    }
                )
                if task_id is None:
                    skipped += 1
                    continue
                submitting = False
                try:
                    if init_error:
                        raise init_error
                    if engine is None:
                        try:
                            engine = client(downloader)
                            if isinstance(engine, Aria2):
                                engine.begin_batch(cfg.tracking)
                        except Exception as exc:
                            init_error = exc
                            raise
                    engine_id, prepared = engine.prepare(release, source, session)
                    self.db.update(
                        "tasks",
                        task_id,
                        engine_id=engine_id,
                        current_engine_id=engine_id,
                        content_hashes=json.dumps(engine.content_hashes),
                        target_path=engine.target_path
                        if isinstance(engine, Aria2)
                        else posixpath.join(directory, release.file_name),
                        excluded_engine_ids=json.dumps(
                            engine.baseline_ids if isinstance(engine, Aria2) else []
                        ),
                        rebuild_baseline_complete=int(engine.baseline_complete)
                        if isinstance(engine, Aria2)
                        else 1,
                        submission="submitting",
                        updated_at=now(),
                    )
                    self.db.attempt(task_id, engine_id, "submitted")
                    submitting = True
                    engine.submit(prepared)
                    self.db.update(
                        "tasks", task_id, submission="submitted", updated_at=now(), retry_after=None
                    )
                    created += 1
                except Exception as exc:
                    ambiguous = submitting and not isinstance(exc, Rejected)
                    self.db.update(
                        "tasks",
                        task_id,
                        submission="unknown" if ambiguous else "failed",
                        error=("提交结果待确认；" if ambiguous else "") + safe_error(exc),
                        updated_at=now(),
                    )
                    if not ambiguous:
                        self.db.backoff(task_id, cfg.maintenance)
                    failed += 1
            self.db.update(
                "runs",
                run_id,
                status="partial" if failed else "success",
                created=created,
                skipped=skipped,
                finished_at=now(),
                error=f"{failed} 个任务发送失败或结果待确认" if failed else None,
            )
        except Exception as exc:
            log.warning("同步失败 (%s)", type(exc).__name__)
            if run_id:
                self.db.update(
                    "runs",
                    run_id,
                    status="interrupted" if self.stopping.is_set() else "error",
                    error="服务停止，中断本次同步" if self.stopping.is_set() else safe_error(exc),
                    finished_at=now(),
                    created=created,
                    skipped=skipped,
                )
        finally:
            try:
                close_safely(session)
                close_safely(engine)
            finally:
                self.running = False
                self.sync_lock.release()
        self.poll(force=False)

    def poll(self, force=True, task_id=None):
        if not self.poll_lock.acquire(blocking=False):
            return False
        try:
            cfg = self.config.get()
            groups = defaultdict(list)
            for row in self.db.observable(cfg.tracking, force, task_id):
                groups[row["downloader"]].append(row)
            self.observation_note = None
            retry_budget = cfg.download_retry.max_per_poll
            for name, rows in groups.items():
                if self.stopping.is_set():
                    break
                engine = None
                try:
                    downloader = cfg.downloaders.get(name)
                    if not downloader or not downloader.enabled:
                        raise ValueError("该下载器已停用或移除，无法读取状态")
                    eligible = []
                    for row in rows:
                        if (row["engine_type"], row["engine_endpoint"]) != (
                            downloader.type,
                            identity_endpoint(downloader),
                        ):
                            self.db.update(
                                "tasks",
                                row["id"],
                                observation_error="下载器地址或类型已改变，保留最后状态",
                                next_observe_at=later(cfg.tracking.error_poll_seconds),
                            )
                        else:
                            eligible.append(row)
                    if not eligible:
                        continue
                    engine = client(downloader)
                    snapshots, uri_index = {}, defaultdict(set)
                    scanned = False
                    if isinstance(engine, QBittorrent):
                        ids = set()
                        for row in eligible:
                            ids.update(self._aliases(row))
                        ids = sorted(ids)
                        for offset in range(0, len(ids), 100):
                            snapshots.update(engine.observe_many(ids[offset : offset + 100]))
                    elif cfg.tracking.external_rebuild_enabled or (cfg.download_retry.enabled and not force):
                        snapshots = engine.list_snapshots(cfg.tracking.max_engine_results)
                        scanned = True
                        if engine.scan_truncated or len(snapshots) >= cfg.tracking.max_engine_results:
                            self.observation_note = (
                                "已达到引擎扫描上限，部分外部重建可能暂不可见；可提高扫描上限"
                            )
                        for gid, item in snapshots.items():
                            for uri in engine.uris(item) | {item.get("infoHash", "")}:
                                if uri:
                                    uri_index[uri].add(gid)
                    claimed = self.db.attempt_owners(name, identity_endpoint(downloader))
                    for row in eligible:
                        if self.stopping.is_set():
                            break
                        try:
                            if isinstance(engine, QBittorrent):
                                item = next(
                                    (snapshots[a] for a in self._aliases(row) if a in snapshots), None
                                )
                                if item is None:
                                    raise MissingTask()
                                values = engine.snapshot(item) | {"current_engine_id": item["hash"]}
                                self.db.attempt(row["id"], item["hash"], values["status"])
                            else:
                                row = self.download_retry.reconcile(engine, row, snapshots)
                                values = self._observe_aria(
                                    engine, row, snapshots, claimed, uri_index, cfg.tracking
                                )
                            status = values["status"]
                            delay = (
                                cfg.tracking.completed_poll_seconds
                                if status in ("completed", "removed")
                                else (
                                    cfg.tracking.error_poll_seconds
                                    if status == "error"
                                    else cfg.progress_poll_seconds
                                )
                            )
                            self.db.update(
                                "tasks",
                                row["id"],
                                **values,
                                submission="submitted",
                                observed_at=now(),
                                first_engine_seen_at=row["first_engine_seen_at"] or now(),
                                last_engine_seen_at=now(),
                                terminal_at=(row["terminal_at"] or now())
                                if status in ("completed", "removed")
                                else None,
                                next_observe_at=later(delay),
                                updated_at=now(),
                                observation_error=(
                                    "提交时队列基线不完整，仅观察已知 GID 与原生后继；无法可靠关联外部重建"
                                    if isinstance(engine, Aria2)
                                    and cfg.tracking.external_rebuild_enabled
                                    and not row["rebuild_baseline_complete"]
                                    else None
                                ),
                                last_missing_at=None,
                            )
                            if isinstance(engine, Aria2) and not force:
                                sent = self.download_retry.process(
                                    engine,
                                    row["id"],
                                    snapshots,
                                    scanned
                                    and not engine.scan_truncated
                                    and len(snapshots) < cfg.tracking.max_engine_results,
                                    retry_budget,
                                )
                                retry_budget -= int(sent)
                        except MissingTask as exc:
                            self.db.update(
                                "tasks",
                                row["id"],
                                status=row["status"]
                                if row["status"] in ("completed", "error", "removed")
                                else "tracking_lost",
                                observation_error=safe_error(exc),
                                last_missing_at=now(),
                                next_observe_at=later(cfg.tracking.lost_poll_seconds),
                            )
                        except Exception as exc:
                            self.db.update(
                                "tasks",
                                row["id"],
                                observation_error=safe_error(exc),
                                next_observe_at=later(cfg.tracking.error_poll_seconds),
                            )
                except Exception as exc:
                    for row in rows:
                        self.db.update(
                            "tasks",
                            row["id"],
                            observation_error=safe_error(exc),
                            next_observe_at=later(cfg.tracking.error_poll_seconds),
                        )
                finally:
                    close_safely(engine)
            return True
        finally:
            self.poll_lock.release()

    @staticmethod
    def _aliases(row):
        return {row["engine_id"], row["current_engine_id"]} - {None} | hash_aliases(
            json.loads(row["content_hashes"])
        )

    def _observe_aria(self, engine, row, snapshots, claimed, uri_index, tracking):
        current_id = row["current_engine_id"] or row["engine_id"]
        current = snapshots.get(current_id)
        if current is None:
            try:
                current = engine.rpc("tellStatus", current_id, engine.STATUS_KEYS)
            except MissingTask:
                pass
        known = set(json.loads(row["known_uris"]))
        if current:
            known |= engine.uris(current)
            self.db.attempt(
                row["id"],
                current_id,
                {"complete": "completed", "active": "downloading"}.get(current["status"], current["status"]),
            )
        if (
            tracking.external_rebuild_enabled
            and row["rebuild_baseline_complete"]
            and not self.observation_note
            and (current is None or current["status"] in ("error", "removed", "complete"))
        ):
            previous = (
                self.db.attempt_ids(row["id"])
                | {row["engine_id"], current_id}
                | set(json.loads(row["excluded_engine_ids"]))
            )
            hashes = json.loads(row["content_hashes"])
            candidates = []
            for gid in set().union(*(uri_index.get(key, set()) for key in known | hash_aliases(hashes))):
                if gid in previous or (gid in claimed and claimed[gid] != row["id"]):
                    continue
                if engine.matches(snapshots[gid], known, row["target_path"], hashes, row["target_directory"]):
                    candidates.append(snapshots[gid])
            if len(candidates) > 1:
                raise ValueError("发现多个匹配的后继任务，无法唯一关联")
            if candidates:
                current = candidates[0]
                current_id = current["gid"]
                claimed[current_id] = row["id"]
        if current is None:
            raise MissingTask()
        values = engine.observe(current_id, snapshots | {current_id: current})
        values["progress_estimated"] = False
        if row["link_type"] == "http" and values["total"] == 0:
            if values["status"] == "completed":
                values["total"] = values["downloaded"]
            elif row["rss_size_bytes"] > 0:
                values["progress"] = min(99.9, values["downloaded"] * 100 / row["rss_size_bytes"])
                values["progress_estimated"] = True
        result = engine.last_result
        known |= engine.uris(result)
        target = row["target_path"]
        if len(result.get("files", [])) == 1 and result["files"][0].get("path"):
            target = result["files"][0]["path"]
        self.db.update("tasks", row["id"], known_uris=json.dumps(sorted(known)), target_path=target)
        self.db.attempt(row["id"], values["current_engine_id"], values["status"])
        return values

    def resolve_unknown(self, task_id, confirmed=False):
        if not self.sync_lock.acquire(blocking=False):
            raise ValueError("同步正在运行，请稍后核对")
        try:
            before = now()
            row = self.db.task(task_id)
            if not row:
                raise ValueError("任务记录不存在")
            if row["submission"] != "unknown":
                raise ValueError("仅提交结果不确定的记录可使用此流程")
            if not self.poll(force=True, task_id=task_id):
                raise ValueError("正在读取状态，请稍后核对")
            row = self.db.task(task_id)
            if row["submission"] == "submitted":
                return {"state": "found"}
            if not confirmed:
                return {
                    "state": "not_found",
                    "can_confirm": not self.observation_note
                    and bool(row["last_missing_at"] and row["last_missing_at"] >= before),
                }
            if self.observation_note:
                raise ValueError("引擎扫描不完整，请提高扫描上限后再核对")
            if row["first_engine_seen_at"] or not row["last_missing_at"] or row["last_missing_at"] < before:
                raise ValueError("无法确认未入队，或此任务曾被引擎观察到；请在下载器中核查")
            cfg = self.config.get()
            downloader = cfg.downloaders.get(row["downloader"])
            if (
                not downloader
                or not downloader.enabled
                or cfg.source.downloader != row["downloader"]
                or cfg.source.api_type != row["link_type"]
            ):
                raise ValueError("请恢复此记录原先的下载器和资源类型后再确认")
            directory = row["target_directory"] or posixpath.dirname(row["target_path"])
            relative = posixpath.relpath(directory, downloader.download_base_path)
            if relative == ".." or relative.startswith("../"):
                raise ValueError("下载根目录已改变，请先恢复原先配置")
            self.db.update(
                "tasks",
                task_id,
                submission="failed",
                retry_after=None,
                recovery_requested=1,
                error="管理员核查引擎后确认未入队，等待下一次 RSS 同步重新提交",
            )
            return {"state": "retry_on_sync"}
        finally:
            self.sync_lock.release()
