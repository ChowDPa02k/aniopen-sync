import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from conftest import TITLE

from aniopen_sync.config import Maintenance
from aniopen_sync.db import SCHEMA, Database, now


def row_values(identity):
    return {
        "identity": identity,
        "triggered_at": now(),
        "trigger_type": "manual",
        "original_name": TITLE,
        "file_name": TITLE,
        "show_title": "測試動畫",
        "season": 2,
        "episode": "13",
        "downloader": "aria2",
        "engine_type": "aria2",
        "engine_endpoint": "http://aria2:6800/jsonrpc",
        "updated_at": now(),
    }


def test_unversioned_database_upgrade_backs_up_and_preserves_dedup(tmp_path):
    path = tmp_path / "history.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    values = row_values("legacy-identity")
    values["submission"] = "submitted"
    values["engine_id"] = "original-gid"
    connection.execute(
        f"INSERT INTO tasks ({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
        list(values.values()),
    )
    connection.execute("INSERT INTO legacy_seen VALUES ('md5-old')")
    connection.commit()
    connection.close()
    db = Database(path)
    assert db.task(1)["identity"] == "legacy-identity"
    assert db.legacy_seen("md5-old")
    assert db.reserve_task(values) is None
    backups = list((tmp_path / "backups").glob("history-v0-*.sqlite3"))
    assert len(backups) == 1
    original = sqlite3.connect(backups[0])
    assert "next_observe_at" not in {r[1] for r in original.execute("PRAGMA table_info(tasks)")}
    original.close()
    Database(path)
    assert len(list((tmp_path / "backups").glob("history-v0-*"))) == 1


def test_newer_database_is_never_silently_downgraded(tmp_path):
    path = tmp_path / "history.sqlite3"
    db = sqlite3.connect(path)
    db.execute("PRAGMA user_version=99")
    db.close()
    with pytest.raises(ValueError, match="更新版本"):
        Database(path)
    db = sqlite3.connect(path)
    assert db.execute("PRAGMA user_version").fetchone()[0] == 99
    db.close()


def test_retention_does_not_replay_old_feeds(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    task = db.reserve_task(row_values("old-completed"))
    db.update("tasks", task, status="completed", terminal_at="2000-01-01T00:00:00+00:00")
    pending = db.reserve_task(row_values("active"))
    db.maintenance(Maintenance(history_retention_days=1))
    assert db.task(task) is None and db.task(pending)
    assert db.reserve_task(row_values("old-completed")) is None
    assert db.tasks()["total"] == 1


def test_terminal_window_and_error_recovery_polling_are_bounded(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    for identity, status in [("old", "completed"), ("error", "error"), ("recent", "completed")]:
        task = db.reserve_task(row_values(identity))
        db.update(
            "tasks",
            task,
            engine_id=identity,
            submission="submitted",
            engine_type="qbittorrent",
            status=status,
            terminal_at="2000-01-01T00:00:00+00:00" if identity == "old" else now(),
        )
    assert {r["identity"] for r in db.observable()} == {"error", "recent"}
    db.update("tasks", 2, next_observe_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat())
    assert {r["identity"] for r in db.observable()} == {"recent"}
    assert len(db.observable(force=True)) == 3


def test_upgrade_rewritten_url_identity_remains_retryable_and_deduplicated(tmp_path):
    import hashlib

    path = tmp_path / "history.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    url = "https://cloud.ani-download.workers.dev/old.mp4"
    original = "https://resources.ani.rip/old.mp4"
    values = row_values(hashlib.sha256(url.encode()).hexdigest())
    values.update(source_url=url, submission="failed")
    connection.execute(
        f"INSERT INTO tasks ({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
        list(values.values()),
    )
    connection.commit()
    connection.close()
    db = Database(path)
    identity = db.identity_for_url(original, hashlib.sha256(original.encode()).hexdigest())
    assert identity == values["identity"]
    assert db.reserve_task(values) == 1  # Old safe failure can still retry under its original row.
    db.update("tasks", 1, submission="submitted", status="completed", terminal_at="2000-01-01T00:00:00+00:00")
    db.maintenance(Maintenance())
    assert db.reserve_task(row_values(hashlib.sha256(original.encode()).hexdigest())) is None


def test_archived_url_alias_prevents_replay_if_guid_changes(tmp_path):
    db = Database(tmp_path / "history.sqlite3")
    values = row_values("old-guid-id")
    values["original_url"] = "https://cdn.example/same-release.mp4"
    row = db.reserve_task(values)
    db.update(
        "tasks", row, submission="submitted", status="completed", terminal_at="2000-01-01T00:00:00+00:00"
    )
    db.maintenance()
    values["identity"] = "changed-guid-id"
    assert db.reserve_task(values) is None


def test_v2_upgrade_preserves_history_and_backs_up_before_retry_columns(tmp_path):
    path = tmp_path / "history.sqlite3"
    db = Database(path)
    values = row_values("v2-existing")
    values.update(submission="submitted", engine_id="v2-original-gid")
    task_id = db.reserve_task(values)
    with db.connect() as connection:
        connection.execute("INSERT INTO legacy_seen VALUES ('nas-existing-dedup')")
        names = [r["name"] for r in connection.execute("PRAGMA table_info(tasks)")]
        for name in names:
            if name.startswith("auto_retry_"):
                connection.execute(f"ALTER TABLE tasks DROP COLUMN {name}")
        connection.execute("PRAGMA user_version=2")
    db = Database(path)
    row = db.task(task_id)
    assert row["identity"] == "v2-existing" and row["auto_retry_count"] == 0
    assert row["auto_retry_state"] is None and db.legacy_seen("nas-existing-dedup")
    assert db.reserve_task(values) is None
    backups = list((tmp_path / "backups").glob("history-v2-*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as old:
        assert old.execute("PRAGMA user_version").fetchone()[0] == 2
        assert not any(r[1].startswith("auto_retry_") for r in old.execute("PRAGMA table_info(tasks)"))
    Database(path)
    assert len(list((tmp_path / "backups").glob("history-v2-*.sqlite3"))) == 1
