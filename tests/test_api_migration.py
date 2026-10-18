import pytest
import yaml
from fastapi.testclient import TestClient

from aniopen_sync.app import create_app
from aniopen_sync.config import ConfigStore
from aniopen_sync.db import Database
from aniopen_sync.migrate import migrate


def test_config_invalid_update_is_atomic_and_reschedules(app_client):
    client, app, cfg = app_client
    bad = {**cfg, "schedule": {**cfg["schedule"], "cron": {"minute": "61"}}}
    assert client.put("/api/config", json=bad).status_code == 422
    assert client.get("/api/config").json()["config"] == cfg
    cfg["schedule"]["enabled"] = True
    assert client.put("/api/config", json=cfg).status_code == 200
    assert app.state.service.scheduler.get_job("sync")
    assert client.get("/api/status").json()["next_run"].endswith("+08:00")
    cfg["schedule"]["enabled"] = False
    client.put("/api/config", json={"yaml": yaml.safe_dump(cfg)})
    assert app.state.service.scheduler.get_job("sync") is None
    assert client.put("/api/config", json={"yaml": "source: ["}).status_code == 422
    assert client.get("/api/config/schema").json()["$defs"]["Source"]


def test_token_auth_and_same_origin(monkeypatch, tmp_path):
    monkeypatch.setenv("ANIOPEN_API_TOKEN", "test-only-token")
    with TestClient(create_app(tmp_path)) as client:
        assert client.get("/api/health").json()["authentication_required"]
        assert client.get("/api/config").status_code == 401
        headers = {"Authorization": "Bearer test-only-token"}
        assert client.get("/api/config", headers=headers).status_code == 200
        assert (
            client.post("/api/sync", headers={**headers, "Origin": "https://evil.example"}).status_code == 403
        )
        assert client.post("/api/sync", headers=headers).status_code == 409  # Disabled by default.


def test_single_worker_guard(tmp_path):
    with TestClient(create_app(tmp_path)):
        with pytest.raises(RuntimeError, match="单个 worker"):
            with TestClient(create_app(tmp_path)):
                pass


def test_migration_filters_other_providers_and_preserves_legacy_state(tmp_path):
    old, target = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    source = {
        "ani": {
            "type": "ani_source_provider",
            "enable": True,
            "api_type": "http",
            "blacklist": "配音",
            "custom_season_mapping": {"Season 2": 2},
            "season_episode_adjustment": None,
        },
        "other": {"type": "mikanani_source_provider", "enable": True},
    }
    downloads = {
        "aria2": {
            "type": "aria2_download_provider",
            "enable": True,
            "rpc_endpoint_host": "http://aria2",
            "rpc_endpoint_port": 6800,
            "secret": "test-secret",
            "download_base_path": "/downloads",
        },
        "other": {"type": "transmission_download_provider"},
    }
    for name, value in [
        ("source_provider.yaml", source),
        ("download_provider.yaml", downloads),
        ("state.yaml", {"ani": ["legacy-uid"], "other": ["should-not-import"]}),
    ]:
        (old / name).write_text(yaml.safe_dump(value))
    assert migrate(old, target) == 1
    config = ConfigStore(target / "config.yaml").get()
    assert not config.source.enabled
    assert config.source.blacklist == ["配音"]
    assert config.downloaders["aria2"].endpoint == "http://aria2:6800/jsonrpc"
    assert config.downloaders["aria2"].secret == "test-secret"
    db = Database(target / "history.sqlite3")
    assert db.legacy_seen("legacy-uid") and not db.legacy_seen("should-not-import")
    assert db.tasks()["total"] == 0
    assert (target / "config.yaml").stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="目标目录已有"):
        migrate(old, target)


def test_browser_yaml_12_roundtrip_unquoted_date_and_on_keyword(app_client):
    client, app, cfg = app_client
    text = "schedule:\n  run_date: 2030-01-01T00:00:00+08:00\nsource:\n  blacklist: [on, no, yes, off]\n"
    response = client.post("/api/config/validate", json={"yaml": text})
    assert response.status_code == 200
    assert response.json()["config"]["source"]["blacklist"] == ["on", "no", "yes", "off"]
    assert client.put("/api/config", json={"yaml": text}).status_code == 200


def test_migration_normalizes_legacy_scalars_and_preserves_proxy_paths(tmp_path):
    old = tmp_path / "old"
    old.mkdir()
    (old / "source_provider.yaml").write_text("""ani:
  type: ani_source_provider
  api_type: torrent
  downloader: null
  download_param:
    tags: "ANi, weekly"
    category: 123
    unrelated: true
""")
    (old / "download_provider.yaml").write_text("""qb:
  type: qbittorrent_download_provider
  http_endpoint_host: https://downloads.example/reverse/qbit
  http_endpoint_port: 8443
  username: 0012
  password: 123456
  download_base_path: 123
  priority: null
  tags: null
aria:
  type: aria2_download_provider
  rpc_endpoint_host: https://downloads.example/reverse/aria/jsonrpc
  rpc_endpoint_port: 443
  secret: 01234
""")
    target = tmp_path / "new"
    assert migrate(old, target) == 0
    cfg = ConfigStore(target / "config.yaml").get()
    assert cfg.source.downloader == "qb"
    assert cfg.source.download_param.tags == ["ANi", "weekly"]
    assert cfg.source.download_param.category == "123"
    assert cfg.downloaders["qb"].endpoint == "https://downloads.example:8443/reverse/qbit"
    assert cfg.downloaders["qb"].username == "0012" and cfg.downloaders["qb"].password == "123456"
    assert cfg.downloaders["qb"].download_base_path == "123"
    assert cfg.downloaders["aria"].secret == "01234"
    assert cfg.downloaders["aria"].endpoint == "https://downloads.example:443/reverse/aria/jsonrpc"
