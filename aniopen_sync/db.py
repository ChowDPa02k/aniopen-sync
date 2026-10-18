"""SQLite history, durable deduplication and backwards-compatible upgrades."""

import hashlib
import os
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .config import Maintenance, Tracking

SCHEMA_VERSION = 3
SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY, triggered_at TEXT NOT NULL, trigger_type TEXT NOT NULL,
    finished_at TEXT, status TEXT NOT NULL, created INTEGER DEFAULT 0,
    skipped INTEGER DEFAULT 0, error TEXT
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY, identity TEXT NOT NULL UNIQUE, run_id INTEGER,
    triggered_at TEXT NOT NULL, trigger_type TEXT NOT NULL,
    original_name TEXT NOT NULL, file_name TEXT NOT NULL, show_title TEXT NOT NULL,
    season INTEGER NOT NULL, episode TEXT NOT NULL, downloader TEXT NOT NULL,
    engine_type TEXT NOT NULL, engine_endpoint TEXT NOT NULL,
    engine_id TEXT, current_engine_id TEXT, source_url TEXT, target_path TEXT,
    known_uris TEXT NOT NULL DEFAULT '[]', excluded_engine_ids TEXT NOT NULL DEFAULT '[]', attempts INTEGER NOT NULL DEFAULT 1,
    submission TEXT NOT NULL DEFAULT 'preparing', status TEXT NOT NULL DEFAULT 'pending',
    progress REAL DEFAULT 0, downloaded INTEGER DEFAULT 0, total INTEGER DEFAULT 0,
    speed INTEGER DEFAULT 0, error TEXT, observation_error TEXT,
    observed_at TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS engine_attempts (
    task_id INTEGER NOT NULL, engine_id TEXT NOT NULL, first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL, status TEXT NOT NULL, PRIMARY KEY(task_id, engine_id)
);
CREATE TABLE IF NOT EXISTS legacy_seen (uid TEXT PRIMARY KEY);
CREATE INDEX IF NOT EXISTS tasks_time ON tasks(triggered_at DESC);
"""


def now():
    return datetime.now(UTC).isoformat()


def later(seconds):
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tasks'").fetchone()
        if version > SCHEMA_VERSION:
            raise ValueError("数据库来自更新版本，请升级程序；不会修改原数据库")
        if exists and version < SCHEMA_VERSION:
            timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
            self.backup(path.parent / "backups" / f"history-v{version}-{timestamp}.sqlite3")
        with self.connect() as db:
            db.executescript(SCHEMA)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(tasks)")}
            additions = {
                "original_url": "TEXT",
                "target_directory": "TEXT",
                "link_type": "TEXT NOT NULL DEFAULT 'http'",
                "content_hashes": "TEXT NOT NULL DEFAULT '{}'",
                "first_engine_seen_at": "TEXT",
                "last_engine_seen_at": "TEXT",
                "terminal_at": "TEXT",
                "next_observe_at": "TEXT",
                "retry_after": "TEXT",
                "last_missing_at": "TEXT",
                "recovery_requested": "INTEGER NOT NULL DEFAULT 0",
                "rebuild_baseline_complete": "INTEGER NOT NULL DEFAULT 1",
                "auto_retry_count": "INTEGER NOT NULL DEFAULT 0",
                "auto_retry_gid": "TEXT",
                "auto_retry_source_gid": "TEXT",
                "auto_retry_state": "TEXT",
                "auto_retry_due_at": "TEXT",
                "auto_retry_error": "TEXT",
            }
            for name, kind in additions.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE tasks ADD COLUMN {name} {kind}")
            db.execute(
                "CREATE TABLE IF NOT EXISTS seen (identity TEXT PRIMARY KEY, first_seen TEXT NOT NULL)"
            )
            db.execute("INSERT OR IGNORE INTO seen SELECT identity,triggered_at FROM tasks")
            # 0.1 used rewritten URLs (or arbitrary GUIDs) as identity. Preserve
            # old IDs for retry/history, while anchoring durable dedup to original URLs.
            if version < 2:
                for row in db.execute("SELECT id,original_url,source_url,triggered_at FROM tasks").fetchall():
                    original = row["original_url"] or row["source_url"]
                    if not original:
                        continue
                    parsed = urlsplit(original)
                    if not row["original_url"] and parsed.hostname == "cloud.ani-download.workers.dev":
                        original = urlunsplit(parsed._replace(netloc="resources.ani.rip"))
                    db.execute("UPDATE tasks SET original_url=? WHERE id=?", (original, row["id"]))
                    db.execute(
                        "INSERT OR IGNORE INTO seen VALUES (?,?)",
                        (hashlib.sha256(original.encode()).hexdigest(), row["triggered_at"]),
                    )
            db.execute("CREATE INDEX IF NOT EXISTS tasks_original_url ON tasks(original_url)")
            db.execute("CREATE INDEX IF NOT EXISTS tasks_observation ON tasks(next_observe_at)")
            db.execute("CREATE INDEX IF NOT EXISTS tasks_terminal ON tasks(terminal_at)")
            db.execute(
                "UPDATE tasks SET terminal_at=updated_at WHERE status IN ('completed','removed') AND terminal_at IS NULL"
            )
            db.execute("UPDATE runs SET status='interrupted',finished_at=? WHERE status='running'", (now(),))
            db.execute(
                "UPDATE tasks SET submission='failed',error='进程在准备阶段中断' WHERE submission='preparing'"
            )
            db.execute(
                "UPDATE tasks SET submission='unknown',error='发送时进程中断，等待读取引擎确认' WHERE submission='submitting'"
            )
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        os.chmod(path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def insert(self, table, values, ignore=False):
        if table not in ("tasks", "runs"):
            raise ValueError("Unknown table")
        with self.connect() as db:
            cursor = db.execute(
                f"INSERT {'OR IGNORE' if ignore else ''} INTO {table} ({','.join(values)}) "
                f"VALUES ({','.join('?' for _ in values)})",
                list(values.values()),
            )
            return cursor.lastrowid if cursor.rowcount else None

    def update(self, table, row_id, **values):
        if table not in ("tasks", "runs"):
            raise ValueError("Unknown table")
        with self.connect() as db:
            db.execute(
                f"UPDATE {table} SET {','.join(f'{key}=?' for key in values)} WHERE id=?",
                [*values.values(), row_id],
            )

    def task(self, task_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            return dict(row) if row else None

    def tasks(self, page=1, page_size=30, search="", status=""):
        clauses, args = [], []
        if search:
            clauses.append("(file_name LIKE ? OR show_title LIKE ?)")
            args += [f"%{search}%", f"%{search}%"]
        if status:
            clauses.append("status=?")
            args.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as db:
            total = db.execute("SELECT COUNT(*) FROM tasks" + where, args).fetchone()[0]
            rows = db.execute(
                "SELECT * FROM tasks" + where + " ORDER BY id DESC LIMIT ? OFFSET ?",
                [*args, page_size, (page - 1) * page_size],
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                for key in ("original_url", "source_url"):
                    raw = item.get(key) or ""
                    parsed = urlsplit(raw)
                    if parsed.scheme in ("http", "https"):
                        raw = urlunsplit(
                            (parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, "", "")
                        )
                    elif parsed.scheme == "magnet":
                        raw = "magnet:（内容标识见下方 hash）"
                    item["original_url" if key == "original_url" else "download_url"] = raw
                for key in (
                    "source_url",
                    "target_path",
                    "known_uris",
                    "excluded_engine_ids",
                    "engine_endpoint",
                    "content_hashes",
                ):
                    item.pop(key, None)
                item["engine_attempts"] = [
                    dict(a)
                    for a in db.execute(
                        "SELECT engine_id,first_seen,last_seen,status FROM engine_attempts WHERE task_id=? ORDER BY first_seen",
                        (row["id"],),
                    )
                ]
                result.append(item)
            return {"data": result, "total": total, "success": True}

    def observable(self, tracking=None, force=False, task_id=None):
        tracking = tracking or Tracking()
        conditions = ["engine_id IS NOT NULL", "submission IN ('submitted','unknown','submitting')"]
        args = []
        if not force:
            cutoff = (datetime.now(UTC) - timedelta(hours=tracking.terminal_watch_hours)).isoformat()
            conditions += [
                "(next_observe_at IS NULL OR next_observe_at<=?)",
                "(status NOT IN ('completed','removed') OR terminal_at IS NULL OR terminal_at>?)",
            ]
            args += [now(), cutoff]
        if task_id is not None:
            conditions.append("id=?")
            args.append(task_id)
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM tasks WHERE "
                    + " AND ".join(conditions)
                    + " ORDER BY COALESCE(next_observe_at,triggered_at),id LIMIT ?",
                    [*args, tracking.max_tasks_per_poll],
                )
            ]

    def runs(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 20")]

    def summary(self):
        with self.connect() as db:
            return dict(
                db.execute("""SELECT COUNT(*) AS total,
                COALESCE(SUM(status='completed'),0) AS completed,
                COALESCE(SUM(submission='failed' OR status='error'),0) AS failed,
                COALESCE(SUM(submission='unknown'),0) AS uncertain,
                COALESCE(SUM(status NOT IN ('completed','error','removed') AND submission!='failed'),0) AS active
                FROM tasks""").fetchone()
            )

    def legacy_seen(self, uid):
        with self.connect() as db:
            return db.execute("SELECT 1 FROM legacy_seen WHERE uid=?", (uid,)).fetchone() is not None

    def attempt(self, task_id, engine_id, status):
        with self.connect() as db:
            timestamp = now()
            db.execute(
                """INSERT INTO engine_attempts VALUES (?,?,?,?,?)
                ON CONFLICT(task_id,engine_id) DO UPDATE SET last_seen=excluded.last_seen,status=excluded.status""",
                (task_id, engine_id, timestamp, timestamp, status),
            )

    def attempt_ids(self, task_id):
        with self.connect() as db:
            return {
                row[0]
                for row in db.execute("SELECT engine_id FROM engine_attempts WHERE task_id=?", (task_id,))
            }

    def begin_download_retry(self, task_id, source_gid, new_gid):
        """Reserve the GID durably before any RPC can create a replacement task."""
        with self.connect() as db:
            timestamp = now()
            cursor = db.execute(
                """UPDATE tasks SET auto_retry_count=auto_retry_count+1,
                auto_retry_gid=?,auto_retry_state='submitting',auto_retry_error=NULL,
                auto_retry_due_at=NULL,updated_at=? WHERE id=? AND submission='submitted'
                AND status='error' AND auto_retry_state='waiting' AND auto_retry_source_gid=?
                AND COALESCE(current_engine_id,engine_id)=? AND auto_retry_due_at<=?""",
                (new_gid, timestamp, task_id, source_gid, source_gid, timestamp),
            )
            if not cursor.rowcount:
                return False
            db.execute(
                "INSERT INTO engine_attempts VALUES (?,?,?,?,?)",
                (task_id, new_gid, timestamp, timestamp, "submitting"),
            )
            return True

    def cancel_download_retries(self):
        with self.connect() as db:
            db.execute(
                """UPDATE tasks SET auto_retry_state='cancelled',auto_retry_due_at=NULL,
                auto_retry_error=NULL WHERE auto_retry_state='waiting'"""
            )

    def accept_download_retry(self, task_id, gid):
        with self.connect() as db:
            timestamp = now()
            db.execute(
                """UPDATE tasks SET current_engine_id=?,auto_retry_state='submitted',
                auto_retry_due_at=NULL,auto_retry_error=NULL,status='waiting',progress=0,
                downloaded=0,speed=0,error=NULL,terminal_at=NULL,next_observe_at=?,updated_at=?
                WHERE id=? AND auto_retry_gid=? AND auto_retry_state IN ('submitting','unknown')""",
                (gid, timestamp, timestamp, task_id, gid),
            )
            db.execute(
                "UPDATE engine_attempts SET status='submitted',last_seen=? WHERE task_id=? AND engine_id=?",
                (timestamp, task_id, gid),
            )

    def identity_for_url(self, original_url, fallback):
        with self.connect() as db:
            row = db.execute(
                "SELECT identity FROM tasks WHERE original_url=? ORDER BY id LIMIT 1", (original_url,)
            ).fetchone()
            return row[0] if row else fallback

    def reserve_task(self, values):
        with self.connect() as db:
            row = db.execute(
                "SELECT id,submission,retry_after FROM tasks WHERE identity=?", (values["identity"],)
            ).fetchone()
            if row:
                if row["submission"] != "failed" or (row["retry_after"] and row["retry_after"] > now()):
                    return None
                db.execute(
                    f"UPDATE tasks SET {','.join(f'{key}=?' for key in values)},submission='preparing',"
                    "error=NULL,observation_error=NULL,engine_id=NULL,current_engine_id=NULL,attempts=attempts+1 WHERE id=?",
                    [*values.values(), row["id"]],
                )
                return row["id"]
            aliases = {values["identity"]}
            if values.get("original_url"):
                aliases.add(hashlib.sha256(values["original_url"].encode()).hexdigest())
            if any(
                db.execute("SELECT 1 FROM seen WHERE identity=?", (alias,)).fetchone() for alias in aliases
            ):
                return None
            cursor = db.execute(
                f"INSERT INTO tasks ({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
                list(values.values()),
            )
            db.execute("INSERT INTO seen VALUES (?,?)", (values["identity"], values["triggered_at"]))
            if values.get("original_url"):
                db.execute(
                    "INSERT OR IGNORE INTO seen VALUES (?,?)",
                    (hashlib.sha256(values["original_url"].encode()).hexdigest(), values["triggered_at"]),
                )
            return cursor.lastrowid

    def attempt_owners(self, downloader, endpoint):
        with self.connect() as db:
            return {
                row["engine_id"]: row["task_id"]
                for row in db.execute(
                    """SELECT a.engine_id,a.task_id FROM engine_attempts a JOIN tasks t ON a.task_id=t.id
                   WHERE t.downloader=? AND t.engine_endpoint=?""",
                    (downloader, endpoint),
                )
            }

    def backoff(self, task_id, policy):
        row = self.task(task_id)
        seconds = min(policy.retry_max_seconds, policy.retry_base_seconds * 2 ** min(row["attempts"] - 1, 16))
        self.update("tasks", task_id, retry_after=later(seconds))

    def recoveries(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM tasks WHERE recovery_requested=1")]

    def maintenance(self, policy=None):
        policy = policy or Maintenance()
        cutoff = (datetime.now(UTC) - timedelta(days=policy.history_retention_days)).isoformat()
        with self.connect() as db:
            expired = [
                row[0]
                for row in db.execute(
                    "SELECT id FROM tasks WHERE status IN ('completed','removed') AND terminal_at<?",
                    (cutoff,),
                )
            ]
            overflow = [
                row[0]
                for row in db.execute(
                    "SELECT id FROM tasks WHERE status IN ('completed','removed') ORDER BY id DESC LIMIT -1 OFFSET ?",
                    (policy.history_max_records,),
                )
            ]
            for task_id in set(expired + overflow):
                db.execute("DELETE FROM engine_attempts WHERE task_id=?", (task_id,))
                db.execute("DELETE FROM tasks WHERE id=?", (task_id,))
            db.execute("DELETE FROM runs WHERE finished_at IS NOT NULL AND finished_at<?", (cutoff,))
            db.execute(
                """DELETE FROM runs WHERE status!='running' AND id NOT IN
                         (SELECT id FROM runs ORDER BY id DESC LIMIT ?)""",
                (policy.history_max_records,),
            )
        with self.connect() as db:
            db.execute("PRAGMA wal_checkpoint(PASSIVE)")

    def backup(self, target):
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as source:
            destination = sqlite3.connect(target)
            try:
                source.backup(destination)
            finally:
                destination.close()
        os.chmod(target, 0o600)
        return target
