import sqlite3
from xml.sax.saxutils import escape

import pytest
from conftest import TITLE, sync

from aniopen_sync.config import Source
from aniopen_sync.db import Database
from aniopen_sync.parser import AniParser, parse_rss_size

MIB = 1024**2


def feed(url, size=None):
    size_xml = f"<a:size>{escape(size)}</a:size>" if size is not None else ""
    return (
        f'<rss xmlns:a="https://resources.ani.rip"><channel><item>'
        f"<title>{escape(TITLE)}</title><guid>{escape(url)}</guid>{size_xml}"
        "</item></channel></rss>"
    ).encode()


@pytest.mark.parametrize(
    "value,expected",
    [
        ("284.5 MB", int(284.5 * MIB)),
        (" 1.5 GiB ", 3 * 1024**3 // 2),
        ("2 mb", 2 * MIB),
        ("1 KB", 1024),
        ("1 TB", 1024**4),
        ("1000 B", 1000),
        (None, 0),
        ("", 0),
        ("0 MB", 0),
        ("-1 MB", 0),
        ("NaN MB", 0),
        ("Infinity MB", 0),
        ("1e100 MB", 0),
        ("999999999999999999 TB", 0),
        ("1 MB<script>", 0),
        ("12345", 0),
        ("9" * 65, 0),
    ],
)
def test_parse_rounded_binary_rss_size(value, expected):
    assert parse_rss_size(value) == expected


def test_namespaced_size_is_optional_and_does_not_change_identity_or_torrent_parsing():
    url = "https://resources.ani.rip/episode.mp4"
    parser = AniParser(Source())
    missing = parser.parse(feed(url))[0]
    release = parser.parse(feed(url, "284.5 MB"))[0]
    malformed = parser.parse(feed(url, "unknown"))[0]
    assert release.rss_size_bytes == int(284.5 * MIB)
    assert missing.rss_size_bytes == malformed.rss_size_bytes == 0
    assert missing.identity == release.identity == malformed.identity
    assert release.file_name == missing.file_name
    assert AniParser(Source(api_type="torrent")).parse(feed(url, "1 GB"))[0].rss_size_bytes == 0


def test_http_estimate_uses_downloaded_bytes_and_yields_to_engine_size(app_client, engine):
    client, app, _ = app_client
    engine.feed_content = feed(engine.url + "/file.mp4", "200 MB")
    row = sync(client, app)[0]
    item = engine.aria[row["engine_id"]]
    item.update(totalLength="0", completedLength=str(50 * MIB))
    app.state.service.poll()
    row = client.get("/api/tasks").json()["data"][0]
    assert row["rss_size_bytes"] == 200 * MIB
    assert row["progress"] == 25 and row["progress_estimated"] is True
    assert row["total"] == 0 and row["downloaded"] == 50 * MIB
    persisted = Database(app.state.service.db.path).task(row["id"])
    assert persisted["rss_size_bytes"] == 200 * MIB and persisted["progress_estimated"]

    item["totalLength"] = str(100 * MIB)
    app.state.service.poll()
    row = client.get("/api/tasks").json()["data"][0]
    assert row["progress"] == 50 and row["progress_estimated"] is False
    assert row["total"] == 100 * MIB

    actual = 200 * MIB + 50000  # Rounded RSS size can be smaller than the real file.
    item.update(totalLength="0", completedLength=str(actual))
    for state, expected in (("active", "downloading"), ("paused", "paused"), ("error", "error")):
        item["status"] = state
        app.state.service.poll()
        row = client.get("/api/tasks").json()["data"][0]
        assert row["status"] == expected and row["progress"] == 99.9
        assert row["progress_estimated"] is True
    item.update(status="complete", downloadSpeed="0")
    app.state.service.poll()
    row = client.get("/api/tasks").json()["data"][0]
    assert row["status"] == "completed" and row["progress"] == 100
    assert row["total"] == row["downloaded"] == actual
    assert row["progress_estimated"] is False
    assert engine.add_count == 1


@pytest.mark.parametrize("size", [None, "invalid"])
def test_unknown_size_without_valid_rss_is_not_guessed(app_client, engine, size):
    client, app, _ = app_client
    engine.feed_content = feed(engine.url + "/file.mp4", size)
    row = sync(client, app)[0]
    item = engine.aria[row["engine_id"]]
    item.update(totalLength="0", completedLength="250")
    app.state.service.poll()
    row = client.get("/api/tasks").json()["data"][0]
    assert row["total"] == row["progress"] == row["rss_size_bytes"] == 0
    assert row["progress_estimated"] is False
    item.update(status="complete")
    app.state.service.poll()
    row = client.get("/api/tasks").json()["data"][0]
    assert row["progress"] == 100 and row["total"] == 250


def test_resync_backfills_existing_rewritten_http_task_without_resubmitting(app_client, engine):
    client, app, cfg = app_client
    cfg["source"]["url_rewrites"] = {"127.0.0.1": "cdn.example"}
    client.put("/api/config", json=cfg).raise_for_status()
    engine.feed_content = feed(engine.url + "/file.mp4")
    row = sync(client, app)[0]
    engine.aria[row["engine_id"]].update(totalLength="0", completedLength=str(20 * MIB))
    app.state.service.poll()
    assert client.get("/api/tasks").json()["data"][0]["progress"] == 0

    engine.feed_content = feed(engine.url + "/file.mp4", "80 MB")
    sync(client, app)
    app.state.service.poll()
    row = client.get("/api/tasks").json()["data"][0]
    assert row["rss_size_bytes"] == 80 * MIB and row["progress"] == 25
    assert row["progress_estimated"] is True and engine.add_count == 1

    engine.feed_content = feed(engine.url + "/file.mp4", "unknown")
    sync(client, app)
    assert app.state.service.db.task(row["id"])["rss_size_bytes"] == 80 * MIB

    engine.feed_content = feed(engine.url + "/different.mp4", "160 MB")
    sync(client, app)
    assert app.state.service.db.task(row["id"])["rss_size_bytes"] == 80 * MIB
    assert engine.add_count == 2  # A different URL is a separate release even with the same name.


def test_v3_upgrade_preserves_tasks_dedup_and_backs_up_before_rss_columns(tmp_path):
    from test_persistence import row_values

    path = tmp_path / "history.sqlite3"
    db = Database(path)
    values = row_values("existing-task")
    values.update(submission="submitted", engine_id="original-gid")
    task_id = db.reserve_task(values)
    with db.connect() as connection:
        connection.execute("INSERT INTO legacy_seen VALUES ('legacy-seen')")
        for name in ("rss_size_bytes", "progress_estimated"):
            connection.execute(f"ALTER TABLE tasks DROP COLUMN {name}")
        connection.execute("PRAGMA user_version=3")
    db = Database(path)
    assert db.task(task_id)["rss_size_bytes"] == 0
    assert db.tasks()["data"][0]["progress_estimated"] is False
    assert db.legacy_seen("legacy-seen") and db.reserve_task(values) is None
    backups = list((tmp_path / "backups").glob("history-v3-*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert "rss_size_bytes" not in {r[1] for r in connection.execute("PRAGMA table_info(tasks)")}
    Database(path)
    assert len(list((tmp_path / "backups").glob("history-v3-*.sqlite3"))) == 1
