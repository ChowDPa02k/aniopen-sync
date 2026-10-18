import io
import json
from urllib.error import HTTPError, URLError

import pytest

from aniopen_sync import healthcheck
from aniopen_sync.config import ConfigStore
from aniopen_sync.health import DependencyHealth


def configured_monitor(tmp_path, engine, kind="aria2"):
    store = ConfigStore(tmp_path / "config.yaml")
    cfg = store.get()
    cfg.source.enabled = True
    cfg.source.rss_link = cfg.source.rss_link_torrent = engine.url + "/feed"
    cfg.source.api_type = "torrent" if kind == "qbittorrent" else "http"
    cfg.source.downloader = kind
    cfg.downloaders[kind].enabled = True
    cfg.downloaders[kind].endpoint = engine.url + ("/jsonrpc" if kind == "aria2" else "")
    cfg.downloaders[kind].secret = "never-print-rpc-secret"
    cfg.downloaders[kind].password = "never-print-password"
    store.save(cfg)
    return DependencyHealth(store)


def test_disabled_subscription_does_not_require_external_services(tmp_path, monkeypatch):
    monitor = DependencyHealth(ConfigStore(tmp_path / "config.yaml"))
    monkeypatch.setattr(monitor, "_rss", lambda cfg: pytest.fail("Disabled RSS was probed"))
    monkeypatch.setattr(monitor, "_downloader", lambda cfg: pytest.fail("Unconfigured downloader was probed"))
    monitor.refresh()
    assert monitor.snapshot()["status"] == "ok"


@pytest.mark.parametrize("kind", ["aria2", "qbittorrent"])
def test_dependency_fault_and_recovery_without_submitting_downloads(tmp_path, engine, kind):
    monitor = configured_monitor(tmp_path, engine, kind)
    assert monitor.snapshot()["checks"]["monitor"]["code"] == "pending"
    monitor.refresh()
    assert monitor.snapshot()["status"] == "ok"
    engine.feed_content = b"<html>site maintenance</html>"
    monitor.refresh()
    result = monitor.snapshot()
    assert result["status"] == "warning"
    assert result["checks"]["rss"]["code"] == "rss_unavailable"
    assert result["checks"]["downloader"]["status"] == "ok"
    engine.feed_content = None
    monitor.refresh()
    assert monitor.snapshot()["status"] == "ok"
    cfg = monitor.config.get()
    cfg.downloaders[kind].endpoint = "http://127.0.0.1:1/private?token=never-print-query"
    monitor.config.save(cfg)
    monitor.refresh()
    result = monitor.snapshot()
    assert result["status"] == "warning"
    assert result["checks"]["rss"]["status"] == "ok"
    assert result["checks"]["downloader"]["code"] == "downloader_unavailable"
    assert "never-print" not in json.dumps(result)
    assert engine.add_count == 0


def test_expired_or_superseded_probe_cannot_report_healthy(tmp_path, engine, monkeypatch):
    monitor = configured_monitor(tmp_path, engine)
    monitor.refresh()
    assert monitor.snapshot()["status"] == "ok"
    monkeypatch.setattr("aniopen_sync.health.time.monotonic", lambda: monitor.checked_monotonic + 181)
    assert monitor.snapshot()["checks"]["monitor"]["code"] == "stale"
    monkeypatch.undo()

    def change_config_during_probe(cfg):
        changed = monitor.config.get()
        changed.downloaders["aria2"].secret = "changed-credential"
        monitor.config.save(changed)

    monkeypatch.setattr(monitor, "_rss", change_config_during_probe)
    monitor.refresh()
    assert monitor.snapshot()["checks"]["monitor"]["code"] == "pending"


def test_readiness_warning_keeps_liveness_and_configuration_available(app_client, engine):
    api, app, cfg = app_client
    monitor = app.state.service.dependency_health
    monitor.stop()
    engine.feed_content = b"not RSS"
    monitor.refresh()
    response = api.get("/api/health/ready", headers={"Authorization": ""})
    assert response.status_code == 503
    assert response.json()["status"] == "warning"
    assert response.headers["Cache-Control"] == "no-store"
    assert api.get("/api/health").status_code == 200
    assert api.get("/api/config").status_code == 200
    engine.feed_content = None
    monitor.refresh()
    assert api.get("/api/health/ready").status_code == 200


@pytest.mark.parametrize("status", [200, 503])
def test_docker_healthcheck_passes_readiness_status_and_reason(monkeypatch, status):
    payload = (
        {"status": "ok"}
        if status == 200
        else {"status": "warning", "checks": {"rss": {"code": "rss_timeout"}}}
    )
    response = HTTPError("http://localhost", status, "", {}, io.BytesIO(json.dumps(payload).encode()))

    class Opener:
        def open(self, *args, **kwargs):
            if status == 503:
                raise response
            return response

    monkeypatch.setattr(healthcheck, "build_opener", lambda *args: Opener())
    assert healthcheck.check("http://localhost") == (int(status != 200), payload)


def test_unreachable_application_is_not_healthy(monkeypatch):
    class Opener:
        def open(self, *args, **kwargs):
            raise URLError("private?token=do-not-print")

    monkeypatch.setattr(healthcheck, "build_opener", lambda *args: Opener())
    code, result = healthcheck.check("http://localhost")
    assert code == 1 and result["status"] == "warning"
    assert "do-not-print" not in json.dumps(result)
