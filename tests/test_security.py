import tarfile

import pytest
from fastapi.testclient import TestClient

from aniopen_sync.app import create_app
from aniopen_sync.backup import create_backup
from aniopen_sync.config import ConfigStore
from aniopen_sync.db import Database
from aniopen_sync.security import REDACTED


def test_generated_token_persists_and_default_is_authenticated(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        token = app.state.access_token
        assert len(token) >= 32
        assert client.get("/api/config").status_code == 401
        assert client.get("/api/config", headers={"Authorization": "Bearer " + token}).status_code == 200
        assert (tmp_path / "access-token").stat().st_mode & 0o777 == 0o600
    with TestClient(create_app(tmp_path)) as client:
        assert client.get("/api/config", headers={"Authorization": "Bearer " + token}).status_code == 200


def test_unicode_token_fails_with_actionable_startup_error(monkeypatch, tmp_path):
    monkeypatch.setenv("ANIOPEN_API_TOKEN", "中文-token")
    with pytest.raises(ValueError, match="ASCII"):
        with TestClient(create_app(tmp_path)):
            pass


def test_rebinding_host_is_rejected_even_with_matching_origin(app_client):
    client, app, _ = app_client
    response = client.get(
        "/api/config", headers={"Host": "attacker.example:8090", "Origin": "http://attacker.example:8090"}
    )
    assert response.status_code == 400
    assert "secret" not in response.text
    assert client.get("/api/config", headers={"Host": "127.0.0.1:8090"}).status_code == 200


def test_masked_config_roundtrip_and_explicit_reveal(app_client):
    client, app, cfg = app_client
    cfg["downloaders"]["aria2"]["secret"] = "private-test-secret"
    cfg["downloaders"]["qbittorrent"]["password"] = "private-test-password"
    assert client.put("/api/config", json=cfg).status_code == 200
    response = client.get("/api/config")
    assert response.headers["Cache-Control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert "private-test-secret" not in response.text
    masked = response.json()
    assert masked["config"]["downloaders"]["aria2"]["secret"] == REDACTED
    assert client.put("/api/config", json={"yaml": masked["yaml"]}).status_code == 200
    assert app.state.service.config.get().downloaders["aria2"].secret == "private-test-secret"
    revealed = client.post("/api/config/reveal").json()
    assert revealed["config"]["downloaders"]["qbittorrent"]["password"] == "private-test-password"
    masked["config"]["downloaders"]["aria2"]["secret"] = ""
    client.put("/api/config", json=masked["config"])
    assert app.state.service.config.get().downloaders["aria2"].secret == ""


@pytest.mark.parametrize("payload", [{"yaml": None}, {"yaml": 23}, {"yaml": []}, {"yaml": False}])
def test_invalid_yaml_payload_is_422_not_500(app_client, payload):
    client, _, _ = app_client
    assert client.post("/api/config/validate", json=payload).status_code == 422
    assert client.put("/api/config", json=payload).status_code == 422


def test_backup_is_complete_and_can_restore_into_an_empty_directory(app_client, tmp_path):
    client, app, _ = app_client
    source = app.state.service.data_dir
    target = create_backup(source, tmp_path / "backup.tar.gz")
    assert target.stat().st_mode & 0o777 == 0o600
    restored = tmp_path / "restored"
    restored.mkdir()
    with tarfile.open(target) as archive:
        assert {"history.sqlite3", "config.yaml", "access-token"} <= set(archive.getnames())
        archive.extractall(restored, filter="data")
    assert ConfigStore(restored / "config.yaml").get() == app.state.service.config.get()
    assert (restored / "access-token").read_text() == (source / "access-token").read_text()
    assert Database(restored / "history.sqlite3").summary()["total"] == 0
