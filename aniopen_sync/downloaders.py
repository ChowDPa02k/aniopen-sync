"""RSS submission, read-only observation and opt-in aria2 HTTP retry preparation."""

import posixpath
import secrets

import requests

from .config import Downloader, Source, Tracking
from .network import fetch_bytes
from .parser import Release
from .torrents import hash_aliases, magnet_hashes, torrent_id, torrent_info


class Rejected(Exception):
    """The engine explicitly rejected this request."""


class MissingTask(Exception):
    def __init__(self):
        super().__init__("Task Missing")


def safe_error(exc: Exception) -> str:
    if isinstance(exc, requests.Timeout):
        return "连接超时，请检查上游或下载引擎连接"
    if isinstance(exc, requests.RequestException):
        return "网络或 HTTP 请求失败，请检查地址、凭据和服务状态"
    if isinstance(exc, (Rejected, MissingTask, ValueError)):
        return str(exc)
    return f"操作失败 ({type(exc).__name__})"


def identity_endpoint(cfg: Downloader) -> str:
    return cfg.endpoint.rstrip("/")


def engine_session(cfg: Downloader):
    session = requests.Session()
    session.trust_env = False  # Local downloader RPCs should not use the RSS proxy.
    session.verify = cfg.verify_certificate
    return session


class Aria2:
    def __init__(self, cfg: Downloader):
        self.cfg = cfg
        self.session = engine_session(cfg)
        self.baseline_snapshots = None
        self.scan_truncated = False
        self.tracking = Tracking()
        self.content_hashes = {}
        self.target_path = ""

    def close(self):
        self.session.close()

    def rpc(self, method, *args):
        params = list(args)
        if self.cfg.secret:
            params.insert(0, "token:" + self.cfg.secret)
        response = self.session.post(
            self.cfg.endpoint,
            json={
                "jsonrpc": "2.0",
                "id": secrets.token_hex(8),
                "method": "aria2." + method,
                "params": params,
            },
            timeout=self.cfg.timeout,
        )
        # aria2 returns JSON-RPC errors with HTTP 400; other HTTP failures still
        # belong to the transport layer (including proxy/authentication errors).
        if response.status_code != 400:
            response.raise_for_status()
        try:
            payload = response.json()
        except ValueError:
            response.raise_for_status()
            raise
        error = payload.get("error") if isinstance(payload, dict) else None
        if (
            isinstance(error, dict)
            and type(error.get("code")) is int
            and isinstance(error.get("message"), str)
        ):
            # Code 1 also covers Unauthorized and invalid parameters. Only the
            # requested GID's explicit absence should become MissingTask.
            if (
                method == "tellStatus"
                and args
                and error["code"] == 1
                and error["message"] == f"GID {args[0]} is not found"
            ):
                raise MissingTask()
            raise Rejected(f"aria2 拒绝请求 (code {error['code']})")
        response.raise_for_status()
        return payload["result"]

    def begin_batch(self, tracking):
        self.tracking = tracking
        self.baseline_snapshots = (
            self.list_snapshots(tracking.max_engine_results) if tracking.external_rebuild_enabled else {}
        )
        self.baseline_complete = (
            not self.scan_truncated and len(self.baseline_snapshots) < tracking.max_engine_results
        )

    def prepare(self, release: Release, source: Source, upstream):
        gid = secrets.token_hex(8)
        directory = posixpath.join(self.cfg.download_base_path, release.path)
        self.content_hashes = {}
        self.target_path = posixpath.normpath(posixpath.join(directory, release.file_name))
        if source.api_type == "torrent":
            if release.url.startswith("magnet:"):
                self.content_hashes = magnet_hashes(release.url)
                self.target_path = ""  # Learned from the native metadata/content task.
            else:
                content = fetch_bytes(upstream, release.url, source.request_timeout, source.max_torrent_bytes)
                self.content_hashes, name = torrent_info(content)
                self.target_path = posixpath.normpath(posixpath.join(directory, name))
            if "v1" not in self.content_hashes:
                raise ValueError("aria2 不支持仅 v2 的 Torrent，请选择 qBittorrent")
        if self.baseline_snapshots is None:
            self.begin_batch(self.tracking)
        self.baseline_ids = [
            item["gid"]
            for item in self.baseline_snapshots.values()
            if self.matches(item, {release.url}, self.target_path, self.content_hashes, directory)
        ]
        options = {"gid": gid, "dir": directory}
        if source.api_type == "http":
            options["out"] = release.file_name
        return gid, (release.url, options)

    def submit(self, prepared):
        url, options = prepared
        gid = self.rpc("addUri", [url], options)
        self.baseline_snapshots[gid] = {
            "gid": gid,
            "dir": options["dir"],
            "infoHash": self.content_hashes.get("v1", ""),
            "files": [{"path": self.target_path, "uris": [{"uri": url}]}],
        }
        return gid

    STATUS_KEYS = [
        "gid",
        "status",
        "totalLength",
        "completedLength",
        "downloadSpeed",
        "errorCode",
        "followedBy",
        "files",
        "dir",
        "infoHash",
    ]

    def list_snapshots(self, limit=5000):
        # The custom retry callback copies URIs and options, creates a new GID, then
        # deletes the old result. Read all three queues to catch even fast retries.
        active = self.rpc("tellActive", self.STATUS_KEYS)
        self.scan_truncated = len(active) >= limit
        scanned = min(len(active), limit)
        snapshots = {item["gid"]: item for item in active[:limit]}
        for method in ("tellWaiting", "tellStopped"):
            offset = 0
            while scanned < limit:
                size = min(500, limit - scanned)
                # Read stopped results newest first; active/waiting tasks keep queue order.
                position = -offset - 1 if method == "tellStopped" else offset
                items = self.rpc(method, position, size, self.STATUS_KEYS)
                snapshots.update({item["gid"]: item for item in items})
                scanned += len(items)
                if len(items) < size:
                    break
                offset += len(items)
        self.scan_truncated = scanned >= limit
        return snapshots

    @staticmethod
    def uris(item):
        return {
            uri["uri"] for file in item.get("files", []) for uri in file.get("uris", []) if uri.get("uri")
        }

    def matches(self, item, source_uris, target_path, content_hashes=None, directory=""):
        info_hash = item.get("infoHash", "").lower()
        if info_hash and info_hash in hash_aliases(content_hashes or {}):
            # A torrent's content hash AND exact save directory identify its payload, never the .torrent filename.
            return bool(directory) and posixpath.normpath(item.get("dir", "")) == posixpath.normpath(
                directory
            )
        if not target_path or not self.uris(item).intersection(source_uris):
            return False
        paths = {posixpath.normpath(file.get("path", "")) for file in item.get("files", [])}
        if posixpath.normpath(target_path) in paths:
            return True
        # aria2 may have auto-renamed the actual file; the callback preserves out/dir.
        options = self.rpc("getOption", item["gid"])
        out = options.get("out", "")
        return bool(out) and posixpath.normpath(posixpath.join(options.get("dir", ""), out)) == target_path

    @staticmethod
    def snapshot(result):
        total, downloaded = int(result.get("totalLength", 0)), int(result.get("completedLength", 0))
        state = result["status"]
        snapshot = {
            "current_engine_id": result["gid"],
            "status": {"complete": "completed", "active": "downloading"}.get(state, state),
            "progress": 100 if state == "complete" else (min(100, downloaded * 100 / total) if total else 0),
            "total": total,
            "downloaded": downloaded,
            "speed": int(result.get("downloadSpeed", 0)),
            "error": f"aria2 下载错误 (code {result.get('errorCode')})" if state == "error" else None,
        }

        files = result.get("files", [])
        if len(files) == 1 and files[0].get("path"):
            snapshot["file_name"] = posixpath.basename(files[0]["path"])
        return snapshot

    def observe(self, engine_id, snapshots=None):
        seen = set()
        while engine_id not in seen:
            seen.add(engine_id)
            result = (snapshots or {}).get(engine_id) or self.rpc("tellStatus", engine_id, self.STATUS_KEYS)
            following = result.get("followedBy", [])
            if following:
                engine_id = following[0]
                continue
            self.last_result = result
            return self.snapshot(result)
        raise ValueError("aria2 元数据任务引用形成循环")

    def prepare_retry(self, row, error_codes):
        gid = row["current_engine_id"] or row["engine_id"]
        result = self.rpc("tellStatus", gid, self.STATUS_KEYS)
        if result["status"] != "error" or str(result.get("errorCode", "")) not in {
            str(c) for c in error_codes
        }:
            raise ValueError("任务已不符合自动重试条件，等待下一次状态读取")
        if result.get("infoHash") or len(result.get("files", [])) != 1:
            raise ValueError("自动重试仅支持 aria2 HTTP 单文件任务")
        entries = result["files"][0].get("uris", [])
        used = [item for item in entries if item.get("status") == "used"]
        urls = list(
            dict.fromkeys(
                item["uri"]
                for item in (used or entries)
                if item.get("uri", "").startswith(("http://", "https://"))
            )
        )
        if not urls:
            raise ValueError("失败任务没有可重试的 HTTP 来源地址")
        options = dict(self.rpc("getOption", gid))
        directory = posixpath.normpath(options.get("dir", ""))
        if directory != posixpath.normpath(row["target_directory"] or ""):
            raise ValueError("任务保存目录已改变，停止自动重试")
        target = row["target_path"] or ""
        if not target or posixpath.dirname(target) != directory:
            raise ValueError("无法确认原任务保存文件，停止自动重试")
        options.pop("gid", None)
        options["out"] = posixpath.basename(target)
        return urls, options


class QBittorrent:
    def __init__(self, cfg: Downloader):
        self.cfg = cfg
        self.session = engine_session(cfg)
        self.content_hashes = {}
        self.session.headers["Referer"] = cfg.endpoint.rstrip("/") + "/"
        try:
            response = self.session.post(
                cfg.endpoint.rstrip("/") + "/api/v2/auth/login",
                data={
                    "username": cfg.username,
                    "password": cfg.password,
                },
                timeout=cfg.timeout,
            )
            response.raise_for_status()
            if response.status_code != 204 and response.text.strip() != "Ok.":
                self.session.close()
                raise Rejected("qBittorrent 登录失败")
        except Exception:
            self.session.close()
            raise

    def close(self):
        self.session.close()

    def prepare(self, release: Release, source: Source, upstream):
        params = source.download_param
        category = params.category
        if source.use_sub_category and release.sub_category:
            category = "/".join(filter(None, [category, release.sub_category]))
        data = {
            "savepath": posixpath.join(self.cfg.download_base_path, release.path),
            "category": category,
            "tags": ",".join(params.tags),
            "autoTMM": str(bool(category) and params.use_auto_torrent_management).lower(),
        }
        files = None
        if release.url.startswith("magnet:"):
            self.content_hashes = magnet_hashes(release.url)
            data["urls"] = release.url
        else:
            content = fetch_bytes(upstream, release.url, source.request_timeout, source.max_torrent_bytes)
            self.content_hashes, _ = torrent_info(content)
            files = {"torrents": ("ani.torrent", content, "application/x-bittorrent")}
        return torrent_id(self.content_hashes), (data, files)

    def submit(self, prepared):
        data, files = prepared
        response = self.session.post(
            self.cfg.endpoint.rstrip("/") + "/api/v2/torrents/add",
            data=data,
            files=files,
            timeout=self.cfg.timeout,
        )
        response.raise_for_status()
        if response.text.strip() == "Ok.":
            return
        if response.text.strip() == "Fails.":
            raise Rejected("qBittorrent 未接受下载任务；请检查种子格式与引擎支持范围")
        # qB 5.2 returns structured add results instead of the old Ok./Fails. text.
        result = response.json()
        if isinstance(result, dict):
            counts = [result.get(key) for key in ("success_count", "pending_count", "failure_count")]
            if all(type(count) is int and count >= 0 for count in counts):
                if counts[0] or counts[1]:
                    return
                if counts[2]:
                    raise Rejected("qBittorrent 未接受下载任务；请检查种子格式与引擎支持范围")
        raise ValueError("qBittorrent 提交响应无法确认，请先核对引擎任务")

    def observe_many(self, ids):
        response = self.session.get(
            self.cfg.endpoint.rstrip("/") + "/api/v2/torrents/info",
            params={"hashes": "|".join(ids)},
            timeout=self.cfg.timeout,
        )
        response.raise_for_status()
        snapshots = {}
        for item in response.json():
            aliases = {item["hash"].lower()}
            aliases |= hash_aliases(
                {"v1": item.get("infohash_v1", "").lower(), "v2": item.get("infohash_v2", "").lower()}
            )
            for alias in aliases:
                snapshots[alias] = item
        return snapshots

    @staticmethod
    def snapshot(item):
        progress = float(item.get("progress", 0))
        state = item["state"]
        if state in ("error", "missingFiles"):
            status = "error"
        elif progress >= 1:
            status = "completed"
        elif state in ("pausedDL", "stoppedDL"):
            status = "paused"
        elif state == "queuedDL":
            status = "waiting"
        else:
            status = "downloading"
        snapshot = {
            "status": status,
            "progress": max(0, min(100, progress * 100)),
            "downloaded": int(item.get("completed", 0)),
            "total": int(item.get("size", 0)),
            "speed": int(item.get("dlspeed", 0)),
            "error": f"qBittorrent: {state}" if status == "error" else None,
        }

        if item.get("name"):
            snapshot["file_name"] = item["name"]
        return snapshot


def client(cfg: Downloader):
    return Aria2(cfg) if cfg.type == "aria2" else QBittorrent(cfg)
