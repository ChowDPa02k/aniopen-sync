import hashlib
import json
import posixpath
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from xml.sax.saxutils import escape

import bencodepy
import pytest
from fastapi.testclient import TestClient

from aniopen_sync.app import create_app


@pytest.fixture(autouse=True)
def test_host(monkeypatch):
    monkeypatch.setenv("ANIOPEN_ALLOWED_HOSTS", "testserver")


TITLE = "[ANi] 測試動畫 第二季 - 13 [1080P][Baha][WEB-DL][AAC AVC][CHT].mp4"
TORRENT = bencodepy.encode(
    {b"info": {b"name": b"episode.mp4", b"length": 1000, b"piece length": 16384, b"pieces": b"a" * 20}}
)
HASH = hashlib.sha1(bencodepy.encode(bencodepy.decode(TORRENT)[b"info"])).hexdigest()


@pytest.fixture
def engine():
    state = SimpleNamespace(
        aria={},
        qb={},
        calls=[],
        reject=False,
        add_count=0,
        mode="http",
        feed_delay=0,
        titles=[TITLE],
        aria_error_after_add=False,
        aria_drop_before_add=False,
        aria_missing_http_status=400,
        torrent_content=TORRENT,
        feed_content=None,
        qb_use_v1_hash=False,
        qb_new_api=False,
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, payload, content_type="application/json", status=200):
            body = json.dumps(payload).encode() if content_type == "application/json" else payload
            self.send_response(
                204
                if state.qb_new_api
                and self.path in ("/api/v2/auth/login", "/api/v2/torrents/add")
                and payload == b"Ok."
                else status
            )
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if not (
                state.qb_new_api
                and self.path in ("/api/v2/auth/login", "/api/v2/torrents/add")
                and payload == b"Ok."
            ):
                self.wfile.write(body)

        def do_GET(self):
            state.calls.append(("GET", self.path))
            if self.path == "/api/v2/app/version":
                return self.reply(b"v5.2.4", "text/plain")
            if self.path == "/feed":
                time.sleep(state.feed_delay)
                if state.feed_content is not None:
                    return self.reply(state.feed_content, "text/html")
                extension = "mp4" if state.mode == "http" else "torrent"
                items = "".join(
                    f"<item><title>{escape(title)}</title><guid>{state.url}/file.{extension}?i={index}</guid></item>"
                    for index, title in enumerate(state.titles)
                )
                return self.reply(f"<rss><channel>{items}</channel></rss>".encode(), "application/xml")
            if self.path.startswith("/file.torrent"):
                return self.reply(state.torrent_content, "application/x-bittorrent")
            if self.path.startswith("/api/v2/torrents/info"):
                ids = parse_qs(urlsplit(self.path).query).get("hashes", [""])[0].split("|")
                return self.reply(
                    [
                        value
                        for key, value in state.qb.items()
                        if key in ids or value.get("infohash_v1") in ids or value.get("infohash_v2") in ids
                    ]
                )
            self.send_error(404)

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            state.calls.append(("POST", self.path))
            if self.path == "/api/v2/auth/login":
                return self.reply(b"Ok.", "text/plain")
            if self.path == "/api/v2/torrents/add":
                state.add_count += 1
                if state.reject:
                    return self.reply(b"Fails.", "text/plain")
                info = bencodepy.decode(state.torrent_content)[b"info"]
                v1 = hashlib.sha1(bencodepy.encode(info)).hexdigest() if b"pieces" in info else ""
                v2 = (
                    hashlib.sha256(bencodepy.encode(info)).hexdigest()
                    if info.get(b"meta version") == 2
                    else ""
                )
                identity = v2[:40] if v2 and not state.qb_use_v1_hash else v1
                state.qb[identity] = {
                    "hash": identity,
                    "infohash_v1": v1,
                    "infohash_v2": v2,
                    "name": "episode.mp4",
                    "state": "downloading",
                    "progress": 0.25,
                    "size": 1000,
                    "completed": 250,
                    "dlspeed": 100,
                }
                if state.qb_new_api:
                    return self.reply(
                        {
                            "success_count": 1,
                            "pending_count": 0,
                            "failure_count": 0,
                            "added_torrent_ids": [identity],
                        }
                    )
                return self.reply(b"Ok.", "text/plain")
            if self.path != "/jsonrpc":
                return self.send_error(404)
            request = json.loads(raw)
            method = request["method"].split(".")[-1]
            params = request["params"]
            if params and isinstance(params[0], str) and params[0].startswith("token:"):
                params = params[1:]
            state.calls.append(("RPC", method))
            result = None
            if method == "getGlobalStat":
                result = {"numActive": str(len(state.aria)), "numWaiting": "0", "numStopped": "0"}
            elif method == "addUri":
                state.add_count += 1
                if state.reject:
                    return self.reply(
                        {"error": {"code": 1, "message": "Rejected by test engine"}, "id": request["id"]}
                    )
                if state.aria_drop_before_add:
                    return self.reply({"id": request["id"]})
                uris, options = params
                gid = options["gid"]
                state.aria[gid] = {
                    "gid": gid,
                    "status": "active",
                    "totalLength": "1000",
                    "completedLength": "250",
                    "downloadSpeed": "100",
                    "dir": options["dir"],
                    "infoHash": HASH if state.mode == "torrent" else "",
                    "options": options,
                    "files": [
                        {
                            "path": posixpath.join(options["dir"], options.get("out", "episode.mp4")),
                            "uris": [{"uri": uri, "status": "used"} for uri in uris],
                        }
                    ],
                }
                if state.aria_error_after_add:
                    return self.reply({"id": request["id"]})  # Simulate an ambiguous reply after accepting.
                result = gid
            elif method == "tellActive":
                result = [v for v in state.aria.values() if v["status"] == "active"]
            elif method in ("tellWaiting", "tellStopped"):
                states = (
                    ("waiting", "paused") if method == "tellWaiting" else ("complete", "error", "removed")
                )
                candidates = [v for v in state.aria.values() if v["status"] in states]
                if params[0] < 0:
                    candidates = list(reversed(candidates))
                    offset = -params[0] - 1
                else:
                    offset = params[0]
                result = candidates[offset : offset + params[1]]
            elif method in ("tellStatus", "getOption"):
                item = state.aria.get(params[0])
                if item is None:
                    return self.reply(
                        {
                            "jsonrpc": "2.0",
                            "error": {"code": 1, "message": f"GID {params[0]} is not found"},
                            "id": request["id"],
                        },
                        status=state.aria_missing_http_status,
                    )
                result = item if method == "tellStatus" else item["options"]
            else:
                raise AssertionError(f"Unexpected mutating or unsupported RPC: {method}")
            return self.reply({"jsonrpc": "2.0", "id": request["id"], "result": result})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.url = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield state
    server.shutdown()
    thread.join()
    server.server_close()


@pytest.fixture
def app_client(tmp_path, engine):
    app = create_app(tmp_path / "data")
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + app.state.access_token
        cfg = client.get("/api/config").json()["config"]
        cfg["schedule"]["enabled"] = False
        cfg["maintenance"]["sync_on_startup"] = False
        cfg["source"].update(
            enabled=True,
            rss_link=engine.url + "/feed",
            rss_link_torrent=engine.url + "/feed",
            online_mappings_enabled=False,
        )
        cfg["downloaders"]["aria2"]["endpoint"] = engine.url + "/jsonrpc"
        cfg["downloaders"]["qbittorrent"].update(endpoint=engine.url, enabled=True)
        assert client.put("/api/config", json=cfg).status_code == 200
        yield client, app, cfg


def sync(client, app):
    assert client.post("/api/sync").status_code == 202
    deadline = time.monotonic() + 5
    while app.state.service.running and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not app.state.service.running
    # _sync releases the sync lock before its initial progress observation.
    with app.state.service.poll_lock:
        pass
    return client.get("/api/tasks").json()["data"]
