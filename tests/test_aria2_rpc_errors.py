import json

import pytest
import requests

from aniopen_sync.config import Downloader
from aniopen_sync.downloaders import Aria2, MissingTask, Rejected, safe_error

GID = "0123456789abcdef"
MISSING = {"code": 1, "message": f"GID {GID} is not found"}


@pytest.fixture
def aria_response(monkeypatch):
    engine = Aria2(Downloader())

    def respond(status, payload):
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps(payload).encode() if not isinstance(payload, bytes) else payload
        monkeypatch.setattr(engine.session, "post", lambda *args, **kwargs: response)
        return engine

    yield respond
    engine.close()


@pytest.mark.parametrize("status", [200, 400])
def test_missing_gid_is_a_missing_task(aria_response, status):
    engine = aria_response(status, {"jsonrpc": "2.0", "error": MISSING})
    with pytest.raises(MissingTask, match="引擎中未找到任务"):
        engine.rpc("tellStatus", GID)


@pytest.mark.parametrize("status", [200, 400])
@pytest.mark.parametrize(
    "error",
    [
        {"code": 1, "message": "Unauthorized"},
        {"code": 1, "message": "GID fedcba9876543210 is not found"},
        {"code": -32602, "message": "Invalid params: sensitive server details"},
    ],
)
def test_other_rpc_errors_are_rejected_without_exposing_server_messages(aria_response, status, error):
    engine = aria_response(status, {"jsonrpc": "2.0", "error": error})
    with pytest.raises(Rejected) as caught:
        engine.rpc("tellStatus", GID)
    assert safe_error(caught.value) == f"aria2 拒绝请求 (code {error['code']})"


def test_missing_gid_message_from_another_method_is_not_a_missing_task(aria_response):
    engine = aria_response(400, {"jsonrpc": "2.0", "error": MISSING})
    with pytest.raises(Rejected):
        engine.rpc("addUri", ["https://example.invalid/video.mp4"])


@pytest.mark.parametrize("status", [401, 403, 404, 500, 502])
def test_other_http_failures_are_not_overridden_by_rpc_body(aria_response, status):
    engine = aria_response(status, {"jsonrpc": "2.0", "error": MISSING})
    with pytest.raises(requests.HTTPError):
        engine.rpc("tellStatus", GID)


@pytest.mark.parametrize(
    "payload",
    [
        b"<html>Bad gateway</html>",
        None,
        [],
        {"error": "not an RPC error"},
        {"error": {"code": 1}},
        {"error": {"code": "1", "message": MISSING["message"]}},
        {"result": {"status": "complete"}},
    ],
)
def test_bad_request_without_valid_rpc_error_remains_http_failure(aria_response, payload):
    engine = aria_response(400, payload)
    with pytest.raises(requests.HTTPError):
        engine.rpc("tellStatus", GID)


@pytest.mark.parametrize("failure", [requests.ConnectionError, requests.Timeout])
def test_transport_failures_remain_transport_failures(monkeypatch, failure):
    engine = Aria2(Downloader())

    def fail(*args, **kwargs):
        raise failure("transport failure")

    monkeypatch.setattr(engine.session, "post", fail)
    try:
        with pytest.raises(failure):
            engine.rpc("tellStatus", GID)
    finally:
        engine.close()
