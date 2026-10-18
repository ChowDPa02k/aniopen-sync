import copy
import hashlib

from conftest import HASH, sync

from aniopen_sync.db import Database


def test_http_sync_dedup_and_readonly_observation(app_client, engine):
    client, app, cfg = app_client
    rows = sync(client, app)
    assert len(rows) == 1
    task = rows[0]
    assert task["submission"] == "submitted"
    assert task["progress"] == 25
    assert task["show_title"] == "測試動畫"
    assert task["season"] == 2
    assert "S02E13" in task["file_name"]
    assert "source_url" not in task
    assert engine.add_count == 1
    sync(client, app)
    assert engine.add_count == 1
    item = engine.aria[task["engine_id"]]
    item.update(status="complete", completedLength="1000", downloadSpeed="0")
    app.state.service.poll()
    assert client.get("/api/tasks").json()["data"][0]["status"] == "completed"
    assert client.get("/api/status").json()["summary"]["completed"] == 1
    assert {method for kind, method in engine.calls if kind == "RPC"} <= {
        "addUri",
        "tellStatus",
        "tellActive",
        "tellWaiting",
        "tellStopped",
        "getOption",
        "getGlobalStat",
    }
    assert client.post("/api/tasks/1/pause").status_code == 404
    assert client.delete("/api/tasks/1").status_code == 404


def test_callback_rebuild_tracks_new_gid_and_preserves_failure(app_client, engine):
    client, app, _ = app_client
    task = sync(client, app)[0]
    original = task["engine_id"]
    engine.aria[original].update(status="error", errorCode="22")
    app.state.service.poll()
    assert client.get("/api/tasks").json()["data"][0]["status"] == "error"
    replacement = copy.deepcopy(engine.aria.pop(original))
    replacement.update(gid="123456789abcdef0", status="active", completedLength="600")
    engine.aria[replacement["gid"]] = replacement
    app.state.service.poll()
    row = client.get("/api/tasks").json()["data"][0]
    assert row["engine_id"] == original
    assert row["current_engine_id"] == replacement["gid"]
    assert row["progress"] == 60
    assert row["status"] == "downloading"
    assert row["error"] is None
    attempts = {attempt["engine_id"]: attempt["status"] for attempt in row["engine_attempts"]}
    assert attempts[original] == "error"
    assert attempts[replacement["gid"]] == "downloading"
    assert engine.add_count == 1  # Only the original submission came from us.


def test_callback_rebuild_after_complete_is_still_discovered(app_client, engine):
    client, app, _ = app_client
    task = sync(client, app)[0]
    original = engine.aria[task["engine_id"]]
    original.update(status="complete", completedLength="1000")
    app.state.service.poll()
    replacement = copy.deepcopy(engine.aria.pop(task["engine_id"]))
    replacement.update(gid="123456789abcdef0", status="active", completedLength="200")
    engine.aria[replacement["gid"]] = replacement
    app.state.service.poll()
    row = client.get("/api/tasks").json()["data"][0]
    assert row["status"] == "downloading" and row["progress"] == 20


def test_same_filename_wrong_uri_never_relinks(app_client, engine):
    client, app, _ = app_client
    task = sync(client, app)[0]
    replacement = engine.aria.pop(task["engine_id"])
    replacement["gid"] = "123456789abcdef0"
    replacement["files"][0]["uris"][0]["uri"] = "http://unrelated.example/file.mp4"
    engine.aria[replacement["gid"]] = replacement
    app.state.service.poll()
    row = client.get("/api/tasks").json()["data"][0]
    assert row["status"] == "tracking_lost"
    assert row["current_engine_id"] == task["engine_id"]


def test_ambiguous_candidates_never_relink(app_client, engine):
    client, app, _ = app_client
    task = sync(client, app)[0]
    original = engine.aria.pop(task["engine_id"])
    for gid in ("1111111111111111", "2222222222222222"):
        item = copy.deepcopy(original)
        item["gid"] = gid
        engine.aria[gid] = item
    app.state.service.poll()
    row = client.get("/api/tasks").json()["data"][0]
    assert "多个" in row["observation_error"]
    assert row["current_engine_id"] == task["engine_id"]


def test_unknown_send_reconciles_without_resubmission(app_client, engine):
    client, app, _ = app_client
    engine.aria_error_after_add = True
    task = sync(client, app)[0]
    assert task["submission"] == "submitted"  # Initial response was ambiguous; observation confirmed it.
    sync(client, app)
    assert engine.add_count == 1


def test_explicit_rejection_can_retry_next_sync(app_client, engine):
    client, app, _ = app_client
    engine.reject = True
    assert sync(client, app)[0]["submission"] == "failed"
    engine.reject = False
    assert sync(client, app)[0]["submission"] == "failed"  # Backoff prevents a request storm.
    app.state.service.db.update("tasks", 1, retry_after="2000-01-01T00:00:00+00:00")
    row = sync(client, app)[0]
    assert row["submission"] == "submitted"
    assert row["attempts"] == 2
    assert len(client.get("/api/tasks").json()["data"]) == 1


def test_qbittorrent_torrent_hash_and_progress(app_client, engine):
    client, app, cfg = app_client
    engine.mode = "torrent"
    cfg["source"].update(api_type="torrent", downloader="qbittorrent")
    assert client.put("/api/config", json=cfg).status_code == 200
    row = sync(client, app)[0]
    assert row["engine_id"] == HASH
    assert row["file_name"] == "episode.mp4"
    assert row["progress"] == 25
    assert "S02E" not in row["file_name"]  # Actual torrent filename is not rewritten.
    engine.qb[HASH].update(state="stalledUP", progress=1, completed=1000)
    app.state.service.poll()
    row = client.get("/api/tasks").json()["data"][0]
    assert row["status"] == "completed"
    assert not any("delete" in path or "pause" in path or "stop" in path for _, path in engine.calls)


def test_changed_endpoint_does_not_match_other_engine(app_client, engine):
    client, app, cfg = app_client
    sync(client, app)
    cfg["downloaders"]["aria2"]["endpoint"] = engine.url + "/different"
    client.put("/api/config", json=cfg)
    app.state.service.poll()
    assert "地址或类型已改变" in client.get("/api/tasks").json()["data"][0]["observation_error"]


def test_restart_keeps_dedup(app_client, engine, tmp_path):
    client, app, _ = app_client
    sync(client, app)
    assert len(Database(tmp_path / "data" / "history.sqlite3").tasks()["data"]) == 1
    sync(client, app)
    assert engine.add_count == 1


def test_duplicate_trigger_is_rejected(app_client, engine):
    client, app, _ = app_client
    engine.feed_delay = 0.2
    assert client.post("/api/sync").status_code == 202
    assert client.post("/api/sync").status_code == 409


def test_legacy_seen_skips_without_synthetic_task_rows(app_client, engine):
    client, app, _ = app_client
    url = engine.url + "/file.mp4?i=0"
    with app.state.service.db.connect() as db:
        db.execute("INSERT INTO legacy_seen VALUES (?)", (hashlib.md5(url.encode()).hexdigest(),))
    assert sync(client, app) == []
    assert engine.add_count == 0


def test_preexisting_same_artifact_does_not_become_successor(app_client, engine):
    import posixpath

    from conftest import TITLE

    from aniopen_sync.config import Source
    from aniopen_sync.parser import AniParser

    client, app, cfg = app_client
    release = AniParser(Source.model_validate(cfg["source"])).parse(
        f"<rss><channel><item><title>{TITLE}</title><guid>{engine.url}/file.mp4?i=0</guid></item></channel></rss>".encode()
    )[0]
    path = posixpath.join("/downloads", release.path, release.file_name)
    engine.aria["0000000000000001"] = {
        "gid": "0000000000000001",
        "status": "complete",
        "totalLength": "1000",
        "completedLength": "1000",
        "downloadSpeed": "0",
        "files": [{"path": path, "uris": [{"uri": release.url}]}],
        "options": {"dir": posixpath.dirname(path), "out": release.file_name},
    }
    task = sync(client, app)[0]
    engine.aria[task["engine_id"]].update(status="complete", completedLength="1000", downloadSpeed="0")
    app.state.service.poll()
    task_after = client.get("/api/tasks").json()["data"][0]
    assert task_after["current_engine_id"] == task["engine_id"]
    assert len(task_after["engine_attempts"]) == 1


def test_restart_restores_history_and_dedup_with_new_service(tmp_path, engine):
    from fastapi.testclient import TestClient

    from aniopen_sync.app import create_app

    directory = tmp_path / "restart-data"
    first = create_app(directory)
    with TestClient(first) as client:
        client.headers["Authorization"] = "Bearer " + first.state.access_token
        cfg = client.get("/api/config").json()["config"]
        cfg["source"].update(enabled=True, rss_link=engine.url + "/feed", online_mappings_enabled=False)
        cfg["schedule"]["enabled"] = False
        cfg["maintenance"]["sync_on_startup"] = False
        cfg["downloaders"]["aria2"]["endpoint"] = engine.url + "/jsonrpc"
        assert client.put("/api/config", json=cfg).status_code == 200
        original = sync(client, first)[0]
    second = create_app(directory)
    with TestClient(second) as client:
        client.headers["Authorization"] = "Bearer " + second.state.access_token
        recovered = sync(client, second)[0]
        assert recovered["id"] == original["id"]
        assert recovered["engine_id"] == original["engine_id"]
        assert recovered["engine_attempts"]
        assert engine.add_count == 1


def test_aria_native_metadata_followedby(app_client, engine):
    client, app, _ = app_client
    task = sync(client, app)[0]
    original = engine.aria[task["engine_id"]]
    child = copy.deepcopy(original)
    child.update(gid="1111111111111111", completedLength="750")
    child["files"][0]["uris"] = [{"uri": "https://different-webseed.example/video.mp4"}]
    engine.aria[child["gid"]] = child
    original.update(status="complete", followedBy=[child["gid"]])
    app.state.service.poll()
    recovered = client.get("/api/tasks").json()["data"][0]
    assert recovered["current_engine_id"] == child["gid"]
    assert recovered["progress"] == 75


def test_external_retry_with_auto_renamed_output_matches_original_options(app_client, engine):
    client, app, _ = app_client
    task = sync(client, app)[0]
    replacement = engine.aria.pop(task["engine_id"])
    replacement["gid"] = "1111111111111111"
    replacement["files"][0]["path"] += ".1"
    engine.aria[replacement["gid"]] = replacement
    app.state.service.poll()
    assert client.get("/api/tasks").json()["data"][0]["current_engine_id"] == replacement["gid"]
