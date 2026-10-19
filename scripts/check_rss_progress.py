"""Verify RSS-based progress against a disposable aria2 downloading without Content-Length."""

import argparse
import hashlib
import json
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from check_downloaders import build_aria, container, docker, wait_for

from aniopen_sync.config import Config, ConfigStore, Downloader
from aniopen_sync.db import Database
from aniopen_sync.downloaders import Aria2
from aniopen_sync.service import Service

PAYLOAD = b"x" * (4 * 1024**2 + 12345)


class Content(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        if self.path == "/feed":
            body = (
                '<rss xmlns:anime="https://resources.ani.rip"><channel><item>'
                "<title>[ANi] Progress Contract - 01 [1080P][Baha][WEB-DL][AAC AVC][CHT].mp4</title>"
                f"<guid>http://host.docker.internal:{self.server.server_port}/payload.mp4</guid>"
                "<anime:size>4.0 MB</anime:size></item></channel></rss>"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(200)
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(PAYLOAD[: 3 * 1024**2])
            self.wfile.flush()
            self.server.release.wait(45)
            self.wfile.write(PAYLOAD[3 * 1024**2 :])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aria-image")
    args = parser.parse_args()
    server = ThreadingHTTPServer(("0.0.0.0", 0), Content)
    server.release = threading.Event()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with container(args.aria_image or build_aria(), 6800) as (name, endpoint):
            downloader = Downloader(endpoint=endpoint + "/jsonrpc", secret="contract-test", timeout=3)
            engine = Aria2(downloader)
            try:
                wait_for(lambda: engine.rpc("getVersion"))
                with tempfile.TemporaryDirectory(prefix="aniopen-progress-") as directory:
                    data = Path(directory)
                    config = ConfigStore(data / "config.yaml")
                    cfg = Config()
                    cfg.schedule.enabled = False
                    cfg.maintenance.sync_on_startup = False
                    cfg.maintenance.automatic_backups = False
                    cfg.source.enabled = True
                    cfg.source.rss_link = f"http://127.0.0.1:{server.server_port}/feed"
                    cfg.source.online_mappings_enabled = False
                    cfg.downloaders = {"aria2": downloader}
                    cfg.tracking.external_rebuild_enabled = False
                    config.save(cfg)
                    db = Database(data / "history.sqlite3")
                    service = Service(config, db, data)
                    try:
                        assert service.trigger()
                        wait_for(lambda: not service.running)

                        def estimated():
                            service.poll()
                            row = db.task(1)
                            return row if row["progress_estimated"] and row["downloaded"] > 0 else None

                        row = wait_for(estimated)
                        assert row["status"] == "downloading" and row["total"] == 0
                        assert 0 < row["progress"] < 100
                        assert row["progress"] == min(99.9, row["downloaded"] * 100 / (4 * 1024**2))
                        server.release.set()
                        wait_for(lambda: engine.observe(row["engine_id"])["status"] == "completed")
                        service.poll()
                        row = db.task(1)
                        assert row["progress"] == 100 and not row["progress_estimated"]
                        assert row["total"] == row["downloaded"] == len(PAYLOAD)
                        digest = docker("exec", name, "sha256sum", row["target_path"]).split()[0]
                        assert digest == hashlib.sha256(PAYLOAD).hexdigest()
                        print(
                            json.dumps(
                                {
                                    "unknown_http_length": "passed",
                                    "rss_progress_estimate": "passed",
                                    "completed_size_and_checksum": "passed",
                                }
                            )
                        )
                    finally:
                        server.release.set()
                        service.stop()
            finally:
                engine.close()
    finally:
        server.release.set()
        server.shutdown()
        thread.join()
        server.server_close()


if __name__ == "__main__":
    main()
