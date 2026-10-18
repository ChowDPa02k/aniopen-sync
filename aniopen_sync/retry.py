"""Optional aria2 HTTP retries; never resubmit an uncertain RPC outcome."""

import json
import secrets

from .db import later, now
from .downloaders import MissingTask, Rejected, identity_endpoint, safe_error


class HttpDownloadRetry:
    def __init__(self, config, db):
        self.config, self.db = config, db

    def reconcile(self, engine, row, snapshots):
        # Reconcile even after the switch is turned off: an already sent task
        # must remain observable, but no new addUri is allowed here.
        if row["auto_retry_state"] not in ("submitting", "unknown"):
            return row
        gid = row["auto_retry_gid"]
        try:
            item = snapshots.get(gid) or engine.rpc("tellStatus", gid, engine.STATUS_KEYS)
            if item.get("gid") != gid:
                raise ValueError("重试任务 GID 响应不一致")
            self.db.accept_download_retry(row["id"], gid)
            snapshots[gid] = item
        except MissingTask:
            self.db.update(
                "tasks",
                row["id"],
                auto_retry_state="unknown",
                auto_retry_error="重试提交结果待确认，尚未读到预留 GID；不会再次发送",
            )
        except Exception as exc:
            self.db.update("tasks", row["id"], auto_retry_state="unknown", auto_retry_error=safe_error(exc))
        return self.db.task(row["id"])

    def process(self, engine, task_id, snapshots, scan_complete, budget):
        policy = self.config.get().download_retry
        row = self.db.task(task_id)
        if row["auto_retry_state"] in ("submitting", "unknown"):
            return False
        if not policy.enabled or row["status"] != "error":
            if row["auto_retry_state"] == "waiting":
                self.db.update(
                    "tasks",
                    task_id,
                    auto_retry_state="cancelled",
                    auto_retry_due_at=None,
                    auto_retry_error=None,
                )
            return False
        if row["link_type"] != "http" or json.loads(row["content_hashes"]):
            return False
        result = engine.last_result
        if result.get("infoHash") or str(result.get("errorCode", "")) not in {
            str(c) for c in policy.error_codes
        }:
            if row["auto_retry_state"] == "waiting":
                self.db.update("tasks", task_id, auto_retry_state="cancelled", auto_retry_due_at=None)
            return False
        source_gid = row["current_engine_id"] or row["engine_id"]
        if policy.max_attempts and row["auto_retry_count"] >= policy.max_attempts:
            self.db.update(
                "tasks",
                task_id,
                auto_retry_state="exhausted",
                auto_retry_due_at=None,
                auto_retry_error="已达到自动重试次数上限",
            )
            return False
        if row["auto_retry_state"] != "waiting" or row["auto_retry_source_gid"] != source_gid:
            delay = min(
                policy.max_delay_seconds, policy.delay_seconds * 2 ** min(row["auto_retry_count"], 16)
            )
            due = later(delay)
            self.db.update(
                "tasks",
                task_id,
                auto_retry_state="waiting",
                auto_retry_source_gid=source_gid,
                auto_retry_gid=None,
                auto_retry_due_at=due,
                auto_retry_error=None,
                next_observe_at=min(row["next_observe_at"] or due, due),
            )
            return False
        self.db.update(
            "tasks",
            task_id,
            next_observe_at=min(row["next_observe_at"] or row["auto_retry_due_at"], row["auto_retry_due_at"]),
        )
        if row["auto_retry_due_at"] > now() or budget <= 0:
            return False
        if not scan_complete:
            self.db.update(
                "tasks",
                task_id,
                auto_retry_error="引擎扫描不完整，暂不重建以避免重复任务",
                auto_retry_due_at=later(policy.delay_seconds),
            )
            return False
        known = set(json.loads(row["known_uris"]))
        for gid, item in snapshots.items():
            if gid != source_gid and item.get("status") in ("active", "waiting", "paused", "complete"):
                if engine.matches(item, known, row["target_path"], directory=row["target_directory"]):
                    self.db.update(
                        "tasks",
                        task_id,
                        auto_retry_error="已有相同来源与保存位置的任务，暂不重建",
                        auto_retry_due_at=later(policy.delay_seconds),
                    )
                    return False
        urls, options = engine.prepare_retry(row, policy.error_codes)
        # Saving configuration can disable retries while getOption is in flight.
        with self.config.lock:
            current = self.config.get()
            downloader = current.downloaders.get(row["downloader"])
            if (
                not current.download_retry.enabled
                or not downloader
                or not downloader.enabled
                or downloader != engine.cfg
                or downloader.type != "aria2"
                or identity_endpoint(downloader) != row["engine_endpoint"]
                or (
                    current.download_retry.max_attempts
                    and row["auto_retry_count"] >= current.download_retry.max_attempts
                )
                or str(result.get("errorCode", ""))
                not in {str(c) for c in current.download_retry.error_codes}
            ):
                return False
            gid = secrets.token_hex(8)
            if not self.db.begin_download_retry(task_id, source_gid, gid):
                return False
        options["gid"] = gid
        try:
            actual = engine.rpc("addUri", urls, options)
            if actual != gid:
                raise ValueError("重试提交响应的 GID 与预留 GID 不一致")
        except Rejected as exc:
            self.db.update("tasks", task_id, auto_retry_state="rejected", auto_retry_error=safe_error(exc))
            self.db.attempt(task_id, gid, "rejected")
        except Exception as exc:
            self.db.update(
                "tasks",
                task_id,
                auto_retry_state="unknown",
                auto_retry_error="重试提交结果待确认；" + safe_error(exc),
            )
        else:
            self.db.accept_download_retry(task_id, gid)
            snapshots[gid] = {
                "gid": gid,
                "status": "waiting",
                "dir": options["dir"],
                "files": [{"path": row["target_path"], "uris": [{"uri": url} for url in urls]}],
            }
        return True
