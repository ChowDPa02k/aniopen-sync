import copy
from datetime import datetime

import pytest
from conftest import TITLE, sync
from pydantic import ValidationError

from aniopen_sync.config import Config, DownloadRetry
from aniopen_sync.db import Database, later
from aniopen_sync.downloaders import Aria2
from aniopen_sync.retry import HttpDownloadRetry


def configure(client, cfg, **values):
    cfg["tracking"]["external_rebuild_enabled"] = False
    cfg["download_retry"].update(enabled=True, delay_seconds=5, max_delay_seconds=20)
    cfg["download_retry"].update(values)
    assert client.put("/api/config", json=cfg).status_code == 200


def poll(app, task_id=1, due=False):
    db = app.state.service.db
    values = {"next_observe_at": None}
    if due:
        values["auto_retry_due_at"] = later(-1)
    db.update("tasks", task_id, **values)
    assert app.state.service.poll(force=False)
    return db.task(task_id)


def failed(app_client, engine):
    client, app, cfg = app_client
    task = sync(client, app)[0]
    engine.aria[task["engine_id"]].update(status="error", errorCode="22")
    configure(client, cfg)
    return client, app, cfg, task


def test_retry_switch_and_policy_validation():
    assert not Config().download_retry.enabled
    for values in [
        {"delay_seconds": 0},
        {"max_attempts": -1},
        {"max_per_poll": 0},
        {"error_codes": [99]},
        {"delay_seconds": 600, "max_delay_seconds": 5},
    ]:
        with pytest.raises(ValidationError):
            DownloadRetry(**values)


def test_retry_preserves_options_identity_and_follows_new_gid(app_client, engine):
    client, app, _, original = failed(app_client, engine)
    old = engine.aria[original["engine_id"]]
    old["options"].update(header="X-Contract: preserved", **{"max-tries": "3"})
    first = poll(app)
    assert first["auto_retry_state"] == "waiting" and engine.add_count == 1
    assert datetime.fromisoformat(first["next_observe_at"]) <= datetime.fromisoformat(
        first["auto_retry_due_at"]
    )
    assert poll(app)["auto_retry_due_at"] == first["auto_retry_due_at"]
    retried = poll(app, due=True)
    new = retried["current_engine_id"]
    assert new != original["engine_id"] and retried["engine_id"] == original["engine_id"]
    assert engine.add_count == 2 and retried["auto_retry_count"] == 1
    assert engine.aria[new]["options"] == old["options"] | {"gid": new}
    assert engine.aria[new]["files"] == old["files"]
    assert retried["identity"] == first["identity"] and retried["original_url"] == first["original_url"]
    assert app.state.service.db.attempt_ids(1) == {new, original["engine_id"]}
    assert engine.aria[original["engine_id"]]["status"] == "error"
    assert poll(app)["progress"] == 25
    engine.aria[new].update(status="complete", completedLength="1000")
    assert poll(app)["status"] == "completed"
    sync(client, app)
    assert engine.add_count == 2  # Retry is a new engine attempt of the same RSS record.
    assert client.get("/api/tasks").json()["total"] == 1
    assert not any(method == "removeDownloadResult" for kind, method in engine.calls if kind == "RPC")


def test_switch_off_and_manual_observation_never_create_retries(app_client, engine):
    client, app, cfg = app_client
    task = sync(client, app)[0]
    engine.aria[task["engine_id"]].update(status="error", errorCode="22")
    assert poll(app)["auto_retry_state"] is None
    configure(client, cfg)
    poll(app)
    app.state.service.db.update("tasks", 1, auto_retry_due_at=later(-1))
    app.state.service.poll()  # A forced, read-only observation must not retry.
    assert engine.add_count == 1
    cfg["download_retry"]["enabled"] = False
    assert client.put("/api/config", json=cfg).status_code == 200
    assert app.state.service.db.task(1)["auto_retry_state"] == "cancelled"
    assert poll(app)["auto_retry_state"] == "cancelled"
    assert engine.add_count == 1


@pytest.mark.parametrize("accepted", [True, False])
def test_uncertain_rpc_survives_restart_and_disabled_switch_without_resending(app_client, engine, accepted):
    client, app, cfg, original = failed(app_client, engine)
    poll(app)
    engine.aria_error_after_add = accepted
    engine.aria_drop_before_add = not accepted
    row = poll(app, due=True)
    assert row["auto_retry_state"] == "unknown"
    reserved = row["auto_retry_gid"]
    assert engine.add_count == 2
    cfg["download_retry"]["enabled"] = False
    assert client.put("/api/config", json=cfg).status_code == 200
    service = app.state.service
    service.download_retry = HttpDownloadRetry(service.config, Database(service.db.path))
    for _ in range(2):
        row = poll(app)
    assert row["auto_retry_count"] == 1 and engine.add_count == 2
    if accepted:
        assert row["current_engine_id"] == reserved and row["status"] == "downloading"
    else:
        assert row["current_engine_id"] == original["engine_id"]
        assert row["auto_retry_state"] == "unknown"


def test_crash_after_acceptance_recovers_preallocated_gid(app_client, engine, monkeypatch):
    _, app, _, _ = failed(app_client, engine)
    poll(app)
    rpc = Aria2.rpc

    def crash(engine_client, method, *args):
        result = rpc(engine_client, method, *args)
        if method == "addUri":
            raise SystemExit("simulated crash after engine accepted")
        return result

    monkeypatch.setattr(Aria2, "rpc", crash)
    with pytest.raises(SystemExit):
        poll(app, due=True)
    row = app.state.service.db.task(1)
    assert row["auto_retry_state"] == "submitting"
    monkeypatch.setattr(Aria2, "rpc", rpc)
    service = app.state.service
    service.download_retry = HttpDownloadRetry(service.config, Database(service.db.path))
    row = poll(app)
    assert row["current_engine_id"] == row["auto_retry_gid"]
    assert row["auto_retry_state"] == "submitted" and engine.add_count == 2


def test_maximum_attempts_and_backoff_are_persistent(app_client, engine):
    client, app, cfg, _ = failed(app_client, engine)
    configure(client, cfg, max_attempts=2)
    poll(app)
    row = poll(app, due=True)
    engine.aria[row["current_engine_id"]].update(status="error", errorCode="22")
    row = poll(app)
    seconds = (
        datetime.fromisoformat(row["auto_retry_due_at"]) - datetime.fromisoformat(row["observed_at"])
    ).total_seconds()
    assert 9 <= seconds <= 11
    row = poll(app, due=True)
    engine.aria[row["current_engine_id"]].update(status="error", errorCode="22")
    assert poll(app)["auto_retry_state"] == "exhausted"
    assert poll(app)["auto_retry_count"] == 2
    assert engine.add_count == 3


@pytest.mark.parametrize("change", ["torrent", "disk_error", "paused", "missing", "directory"])
def test_nonretryable_tasks_are_never_rebuilt(app_client, engine, change):
    _, app, _, task = failed(app_client, engine)
    old = engine.aria[task["engine_id"]]
    if change == "torrent":
        app.state.service.db.update("tasks", 1, link_type="torrent")
    elif change == "disk_error":
        old["errorCode"] = "9"
    elif change == "paused":
        old["status"] = "paused"
    elif change == "missing":
        engine.aria.pop(task["engine_id"])
    else:
        old["options"]["dir"] = "/different"
    poll(app)
    poll(app, due=True)
    assert engine.add_count == 1


def test_existing_external_successor_prevents_duplicate_without_adopting_it(app_client, engine):
    _, app, _, task = failed(app_client, engine)
    poll(app)
    replacement = copy.deepcopy(engine.aria[task["engine_id"]])
    replacement.update(gid="1234567890abcdef", status="active")
    engine.aria[replacement["gid"]] = replacement
    row = poll(app, due=True)
    assert row["current_engine_id"] == task["engine_id"]
    assert "已有" in row["auto_retry_error"] and engine.add_count == 1


def test_truncated_queue_refuses_to_create_a_retry(app_client, engine, monkeypatch):
    _, app, _, _ = failed(app_client, engine)
    poll(app)
    scan = Aria2.list_snapshots

    def truncated(engine_client, limit):
        result = scan(engine_client, limit)
        engine_client.scan_truncated = True
        return result

    monkeypatch.setattr(Aria2, "list_snapshots", truncated)
    row = poll(app, due=True)
    assert "扫描不完整" in row["auto_retry_error"] and engine.add_count == 1


def test_rpc_rejection_is_not_blindly_repeated(app_client, engine):
    _, app, _, _ = failed(app_client, engine)
    poll(app)
    engine.reject = True
    assert poll(app, due=True)["auto_retry_state"] == "rejected"
    poll(app)
    assert engine.add_count == 2
    engine.reject = False
    assert poll(app, due=True)["auto_retry_state"] == "submitted"
    assert engine.add_count == 3


def test_disabling_during_preparation_prevents_submission(app_client, engine, monkeypatch):
    client, app, cfg, _ = failed(app_client, engine)
    poll(app)
    prepare = Aria2.prepare_retry

    def disable(engine_client, row, codes):
        result = prepare(engine_client, row, codes)
        cfg["download_retry"]["enabled"] = False
        assert client.put("/api/config", json=cfg).status_code == 200
        return result

    monkeypatch.setattr(Aria2, "prepare_retry", disable)
    assert poll(app, due=True)["auto_retry_count"] == 0
    assert engine.add_count == 1


def test_retry_budget_does_not_starve_the_remaining_tasks(app_client, engine):
    client, app, cfg = app_client
    engine.titles = [TITLE, TITLE.replace(" - 13 ", " - 14 ")]
    tasks = sync(client, app)
    configure(client, cfg, max_per_poll=1)
    for task in tasks:
        engine.aria[task["engine_id"]].update(status="error", errorCode="22")
        poll(app, task["id"])
    for task in tasks:
        app.state.service.db.update("tasks", task["id"], auto_retry_due_at=later(-1), next_observe_at=None)
    assert app.state.service.poll(force=False)
    assert engine.add_count == 3
    assert app.state.service.poll(force=False)
    assert engine.add_count == 4


def test_unlimited_attempts_keep_the_backoff_bounded(app_client, engine):
    client, app, cfg, _ = failed(app_client, engine)
    configure(client, cfg, max_attempts=0)
    app.state.service.db.update("tasks", 1, auto_retry_count=10000)
    row = poll(app)
    seconds = (
        datetime.fromisoformat(row["auto_retry_due_at"]) - datetime.fromisoformat(row["observed_at"])
    ).total_seconds()
    assert 19 <= seconds <= 21
    assert poll(app, due=True)["auto_retry_count"] == 10001
    assert engine.add_count == 2
