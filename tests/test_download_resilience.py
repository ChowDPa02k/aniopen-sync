import json

import bencodepy
import pytest
from conftest import HASH, TITLE, sync

from aniopen_sync.config import Source
from aniopen_sync.db import now
from aniopen_sync.torrents import hash_aliases, magnet_hashes, torrent_id, torrent_info


def test_qbit_missingfiles_can_recover_without_resubmitting(app_client, engine):
    client, app, cfg = app_client
    engine.mode = "torrent"
    cfg["source"].update(api_type="torrent", downloader="qbittorrent")
    client.put("/api/config", json=cfg)
    sync(client, app)
    engine.qb[HASH].update(state="missingFiles", progress=0)
    app.state.service.poll()
    assert client.get("/api/tasks").json()["data"][0]["status"] == "error"
    engine.qb[HASH].update(state="downloading", progress=0.5)
    app.state.service.poll()
    assert client.get("/api/tasks").json()["data"][0]["progress"] == 50
    engine.qb[HASH].update(state="stalledUP", progress=1)
    app.state.service.poll()
    assert client.get("/api/tasks").json()["data"][0]["status"] == "completed"
    assert engine.add_count == 1


def test_one_queue_baseline_per_sync_batch(app_client, engine):
    client, app, _ = app_client
    engine.titles = [TITLE, TITLE.replace(" - 13 ", " - 14 "), TITLE.replace(" - 13 ", " - 15 ")]
    sync(client, app)
    assert engine.add_count == 3
    assert engine.calls.count(("RPC", "tellActive")) == 2  # One baseline, one observation.


def test_external_rebuild_can_be_disabled_without_queue_scans(app_client, engine):
    client, app, cfg = app_client
    cfg["tracking"]["external_rebuild_enabled"] = False
    client.put("/api/config", json=cfg)
    row = sync(client, app)[0]
    assert row["progress"] == 25
    assert not any(
        method in ("tellActive", "tellWaiting", "tellStopped")
        for kind, method in engine.calls
        if kind == "RPC"
    )


def test_torrent_rebuild_uses_content_hash_and_directory(app_client, engine):
    client, app, cfg = app_client
    engine.mode = "torrent"
    engine.titles = [TITLE.replace(".mp4", ".torrent")]
    cfg["source"]["api_type"] = "torrent"
    client.put("/api/config", json=cfg)
    row = sync(client, app)[0]
    assert app.state.service.db.task(row["id"])["target_path"].endswith("episode.mp4")
    replacement = engine.aria.pop(row["engine_id"])
    replacement["gid"] = "123456789abcdef0"
    replacement["files"][0]["uris"] = []
    replacement["completedLength"] = "500"
    engine.aria[replacement["gid"]] = replacement
    app.state.service.poll()
    result = client.get("/api/tasks").json()["data"][0]
    assert result["current_engine_id"] == replacement["gid"] and result["progress"] == 50


@pytest.mark.parametrize("v1_server", [False, True])
def test_hybrid_hash_aliases_across_qbit_versions(app_client, engine, v1_server):
    client, app, cfg = app_client
    info = {
        b"name": b"hybrid.mp4",
        b"length": 1000,
        b"pieces": b"a" * 20,
        b"piece length": 16384,
        b"meta version": 2,
    }
    engine.mode = "torrent"
    engine.qb_use_v1_hash = v1_server
    engine.torrent_content = bencodepy.encode({b"info": info})
    cfg["source"].update(api_type="torrent", downloader="qbittorrent")
    client.put("/api/config", json=cfg)
    row = sync(client, app)[0]
    assert row["status"] == "downloading" and row["progress"] == 25
    hashes, _ = torrent_info(engine.torrent_content)
    assert row["engine_id"] == hashes["v2"][:40]
    assert row["current_engine_id"] == (hashes["v1"] if v1_server else hashes["v2"][:40])


def test_v2_and_base32_magnet_identity():
    v2 = "ab" * 32
    hashes = magnet_hashes("magnet:?xt=urn:btmh:1220" + v2)
    assert hashes == {"v2": v2} and torrent_id(hashes) == v2[:40]
    assert v2 in hash_aliases(hashes)
    assert len(magnet_hashes("magnet:?xt=urn:btih:" + "A" * 32)["v1"]) == 40


def test_unknown_submission_has_guarded_recovery_even_after_feed_rollover(app_client, engine):
    client, app, _ = app_client
    engine.aria_drop_before_add = True
    row = sync(client, app)[0]
    assert row["submission"] == "unknown" and row["status"] == "tracking_lost"
    assert len(sync(client, app)) == 1 and engine.add_count == 1  # Never blindly retry.
    response = client.post(f"/api/tasks/{row['id']}/resolve-submission", json={})
    assert response.json()["can_confirm"]
    assert (
        client.post(
            f"/api/tasks/{row['id']}/resolve-submission", json={"confirmed_not_submitted": True}
        ).json()["state"]
        == "retry_on_sync"
    )
    engine.aria_drop_before_add = False
    engine.titles = []
    recovered = sync(client, app)[0]
    assert recovered["id"] == row["id"] and recovered["submission"] == "submitted"
    assert engine.add_count == 2


def test_invalid_feed_is_reported_and_never_creates_tasks(app_client, engine):
    client, app, _ = app_client
    engine.feed_content = b"<html><body>OFFLINE</body></html>"
    assert sync(client, app) == []
    run = client.get("/api/status").json()["runs"][0]
    assert run["status"] == "error" and "RSS" in run["error"]
    assert engine.add_count == 0


def test_cleanup_exception_does_not_wedge_sync_lock(app_client, engine, monkeypatch):
    import requests

    client, app, _ = app_client

    def broken_close(_):
        raise RuntimeError("test teardown failure")

    monkeypatch.setattr(requests.Session, "close", broken_close)
    sync(client, app)
    sync(client, app)
    assert not app.state.service.running and engine.add_count == 1


def test_malformed_online_mapping_keeps_last_good_cache(app_client, engine):
    client, app, cfg = app_client
    source = Source(online_mappings_url=engine.url + "/feed")
    cache = {
        "url": source.online_mappings_url,
        "data": {"custom_season_mapping": {"第二季": 5}, "season_episode_adjustment": {}},
        "fetched_at": now(),
        "sha256": "old-hash",
    }
    path = app.state.service.data_dir / "mappings-cache.json"
    path.write_text(json.dumps(cache))
    engine.feed_content = b'{"custom_season_mapping": {"invalid": {"season": -1}}}'
    from aniopen_sync.network import upstream_session

    with upstream_session() as session:
        data = app.state.service._mappings(source, session)
    assert data["custom_season_mapping"]["第二季"] == 5
    assert app.state.service.mapping_status["state"] == "cached"
    assert json.loads(path.read_text()) == cache


def test_qb_52_no_content_login_and_json_submission_are_success(app_client, engine):
    client, app, cfg = app_client
    engine.mode = "torrent"
    engine.qb_new_api = True
    cfg["source"].update(api_type="torrent", downloader="qbittorrent")
    cfg["downloaders"]["qbittorrent"]["enabled"] = True
    assert client.put("/api/config", json=cfg).status_code == 200
    row = sync(client, app)[0]
    assert row["submission"] == "submitted" and row["status"] == "downloading"
    assert engine.add_count == 1


def test_scanning_budget_counts_duplicate_results_not_only_unique_ids():
    from aniopen_sync.config import Downloader
    from aniopen_sync.downloaders import Aria2

    engine = Aria2(Downloader())
    calls = []

    def rpc(method, *params):
        calls.append(method)
        if method == "tellActive":
            return []
        return [{"gid": "same-gid"}] * params[1]

    engine.rpc = rpc
    try:
        assert len(engine.list_snapshots(limit=100)) == 1
        assert engine.scan_truncated and calls == ["tellActive", "tellWaiting"]
    finally:
        engine.close()


def test_incomplete_baseline_does_not_stop_submissions_or_guess_successor(app_client, engine, monkeypatch):
    from aniopen_sync.downloaders import Aria2

    client, app, cfg = app_client
    cfg["tracking"]["max_engine_results"] = 100
    assert client.put("/api/config", json=cfg).status_code == 200
    real = Aria2.list_snapshots

    def capped(self, limit):
        if not engine.aria:
            self.scan_truncated = True
            return {}
        return real(self, limit)

    monkeypatch.setattr(Aria2, "list_snapshots", capped)
    row = sync(client, app)[0]
    assert row["submission"] == "submitted" and row["rebuild_baseline_complete"] == 0
    assert "基线不完整" in row["observation_error"]
    replacement = engine.aria.pop(row["engine_id"])
    replacement["gid"] = "eeeeeeeeeeeeeeee"
    engine.aria[replacement["gid"]] = replacement
    app.state.service.poll()
    observed = client.get("/api/tasks").json()["data"][0]
    assert observed["current_engine_id"] == row["engine_id"] and observed["status"] == "tracking_lost"
    assert engine.add_count == 1


def test_live_mapping_season_zero_is_cached_instead_of_rejecting_entire_source(app_client, engine):
    from aniopen_sync.network import upstream_session
    from aniopen_sync.parser import AniParser

    _, app, _ = app_client
    source = Source(online_mappings_url=engine.url + "/feed")
    engine.feed_content = json.dumps(
        {
            "custom_season_mapping": {"特別篇": {"season": 0, "reserve_keywords": "特別篇"}, "第二季": 2},
            "season_episode_adjustment": {},
        }
    ).encode()
    with upstream_session() as session:
        mapping = app.state.service._mappings(source, session)
    assert app.state.service.mapping_status["state"] == "fresh"
    assert mapping["custom_season_mapping"]["特別篇"]["season"] == 0
    assert AniParser(source, mapping).get_season("測試動畫 特別篇")[0] == 0
    assert (
        json.loads((app.state.service.data_dir / "mappings-cache.json").read_text())["data"][
            "custom_season_mapping"
        ]["特別篇"]["season"]
        == 0
    )
