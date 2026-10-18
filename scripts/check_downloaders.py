"""Exercise real local downloader protocols using disposable containers and generated test data.

Never connects to a production downloader and never invokes task control APIs.
Run: uv run python scripts/check_downloaders.py --qb-image ghcr.io/qbittorrent/docker-qbittorrent-nox:5.0.2-1
"""

import argparse
import contextlib
import hashlib
import json
import os
import re
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import bencodepy

from aniopen_sync.config import Downloader, Source
from aniopen_sync.downloaders import Aria2, QBittorrent
from aniopen_sync.network import upstream_session
from aniopen_sync.parser import Release
from aniopen_sync.torrents import hash_aliases

PAYLOAD = b"ANiOpen Sync local protocol contract test\n" * 8192
INFO = dict(
    sorted(
        {
            b"name": b"contract.txt",
            b"length": len(PAYLOAD),
            b"piece length": 16384,
            b"pieces": b"".join(
                hashlib.sha1(PAYLOAD[index : index + 16384]).digest()
                for index in range(0, len(PAYLOAD), 16384)
            ),
        }.items()
    )
)
TORRENT = bencodepy.encode({b"info": INFO})


def docker(*arguments):
    result = subprocess.run(["docker", *arguments], capture_output=True, text=True, timeout=600)
    if result.returncode:
        raise RuntimeError(f"docker {arguments[0]} failed: {result.stderr[-1200:]}")
    return result.stdout.strip()


@contextlib.contextmanager
def container(image, port, *arguments, host_port=""):
    name = "aniopen-contract-" + uuid.uuid4().hex[:12]
    docker(
        "run",
        "-d",
        "-t",
        "--name",
        name,
        "--add-host",
        "host.docker.internal:host-gateway",
        "-p",
        f"127.0.0.1:{host_port}:{port}",
        *arguments,
        image,
    )
    try:
        mappings = json.loads(docker("inspect", name))[0]["NetworkSettings"]["Ports"][f"{port}/tcp"]
        yield name, f"http://127.0.0.1:{mappings[0]['HostPort']}"
    finally:
        docker("rm", "-f", name)


def wait_for(function, seconds=30):
    deadline = time.monotonic() + seconds
    last = None
    while time.monotonic() < deadline:
        try:
            result = function()
            if result:
                return result
        except Exception as exc:
            last = type(exc).__name__
        time.sleep(0.2)
    raise AssertionError(f"Contract check timed out ({last})")


def build_aria():
    with tempfile.TemporaryDirectory(prefix="aniopen-aria-build-") as directory:
        Path(directory, "Dockerfile").write_text("""FROM alpine:3.24
RUN apk add --no-cache aria2
RUN mkdir -p /downloads
ENTRYPOINT ["aria2c", "--enable-rpc", "--rpc-listen-all=true", "--rpc-secret=contract-test", "--dir=/downloads", "--enable-dht=false", "--bt-enable-lpd=false", "--enable-peer-exchange=false", "--console-log-level=warn"]
""")
        arguments = ["build", "-q", "-t", "aniopen-sync-contract-aria:local"]
        for key in ("HTTP_PROXY", "HTTPS_PROXY"):
            value = os.environ.get(key)
            if value:
                parsed = urlsplit(value)
                if parsed.hostname in ("127.0.0.1", "localhost"):
                    parsed = parsed._replace(netloc=f"host.docker.internal:{parsed.port}")
                arguments += ["--build-arg", key + "=" + urlunsplit(parsed)]
        docker(*arguments, directory)
    return "aniopen-sync-contract-aria:local"


class Content(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        body = TORRENT if self.path.endswith(".torrent") else PAYLOAD
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def check_aria(image, upstream, server_port):
    with container(image, 6800) as (name, endpoint):
        engine = Aria2(Downloader(endpoint=endpoint + "/jsonrpc", secret="contract-test", timeout=3))
        try:
            version = wait_for(lambda: engine.rpc("getVersion"))["version"]
            release = Release(
                "contract-http",
                f"http://host.docker.internal:{server_port}/payload.bin",
                "payload.bin",
                "payload.bin",
                "Contract",
                1,
                "01",
                "TV/ANi/Contract",
            )
            engine_id, prepared = engine.prepare(release, Source(), upstream)
            assert engine.submit(prepared) == engine_id
            status = wait_for(
                lambda: value if (value := engine.observe(engine_id))["status"] == "completed" else None
            )
            assert status["downloaded"] == len(PAYLOAD) and status["progress"] == 100
            digest = docker("exec", name, "sha256sum", "/downloads/TV/ANi/Contract/payload.bin").split()[0]
            assert digest == hashlib.sha256(PAYLOAD).hexdigest()
            print(
                json.dumps(
                    {
                        "engine": "aria2",
                        "version": version,
                        "http_download": "passed",
                        "readonly_status": "passed",
                    }
                )
            )
        finally:
            engine.close()


def check_qbit(image, upstream, server_port):
    # qB WebUI CSRF validation expects Referer port to equal its configured port.
    # Use the same random port on both sides rather than disabling that protection.
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        web_port = reservation.getsockname()[1]
    with container(
        image,
        web_port,
        "-e",
        f"QBT_WEBUI_PORT={web_port}",
        "-e",
        "QBT_LEGAL_NOTICE=confirm",
        "-e",
        "QBT_EULA=accept",
        "--tmpfs",
        "/config:mode=0777,size=64m",
        "--tmpfs",
        "/downloads:mode=0777,size=64m",
        host_port=str(web_port),
    ) as (name, endpoint):

        def connect():
            logs = docker("logs", name)
            match = re.search(
                r"(?:temporary password is provided for this session|temporary password):\s*(\S+)", logs, re.I
            )
            if not match:
                return None  # Do not consume failed-login budget while qB is still starting.
            password = match.group(1)
            return QBittorrent(
                Downloader(
                    type="qbittorrent", endpoint=endpoint, username="admin", password=password, timeout=3
                )
            )

        engine = wait_for(connect)
        try:
            version = engine.session.get(endpoint + "/api/v2/app/version", timeout=3).text
            build = engine.session.get(endpoint + "/api/v2/app/buildInfo", timeout=3).json()
            supports_v2 = str(build.get("libtorrent", "")).startswith("2.")
            source = Source(
                api_type="torrent", use_sub_category=False, download_param={"category": "", "tags": []}
            )
            urls = [
                f"http://127.0.0.1:{server_port}/file.torrent",
                "magnet:?xt=urn:btmh:1220" + hashlib.sha256(b"v2-metadata-test").hexdigest(),
                "magnet:?xt=urn:btih:"
                + hashlib.sha1(b"hybrid-metadata-test").hexdigest()
                + "&xt=urn:btmh:1220"
                + hashlib.sha256(b"hybrid-metadata-test").hexdigest(),
            ]
            for index, url in enumerate(urls):
                if index == 1 and not supports_v2:
                    continue
                release = Release(
                    f"contract-{index}",
                    url,
                    "contract.torrent",
                    "contract.txt",
                    "Contract",
                    1,
                    "01",
                    "contract",
                )
                engine_id, prepared = engine.prepare(release, source, upstream)
                aliases = hash_aliases(engine.content_hashes) | {engine_id}
                engine.submit(prepared)
                snapshots = wait_for(lambda: engine.observe_many(sorted(aliases)))
                assert aliases.intersection(snapshots)
                status = engine.snapshot(next(iter(snapshots.values())))
                assert 0 <= status["progress"] <= 100
            print(
                json.dumps(
                    {
                        "engine": "qbittorrent",
                        "version": version,
                        "v1_torrent": "passed",
                        "libtorrent": build.get("libtorrent"),
                        "v2_magnet": "passed" if supports_v2 else "unsupported by engine build",
                        "hybrid_magnet": "passed",
                        "readonly_status": "passed",
                    }
                )
            )
        finally:
            engine.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qb-image", default="ghcr.io/qbittorrent/docker-qbittorrent-nox:5.0.2-1")
    parser.add_argument("--aria-image")
    args = parser.parse_args()
    server = ThreadingHTTPServer(("0.0.0.0", 0), Content)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with upstream_session() as upstream:
            check_aria(args.aria_image or build_aria(), upstream, server.server_port)
            check_qbit(args.qb_image, upstream, server.server_port)
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


if __name__ == "__main__":
    main()
