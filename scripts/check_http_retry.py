"""Verify HTTP 403 -> automatic retry -> completed against a disposable aria2."""

import argparse
import hashlib
import json
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from check_downloaders import PAYLOAD, build_aria, container, docker, wait_for

from aniopen_sync.config import Config, ConfigStore, Downloader
from aniopen_sync.db import Database, later
from aniopen_sync.downloaders import Aria2
from aniopen_sync.service import Service


class Content(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        if self.path == "/feed":
            body = (
                "<rss><channel><item><title>[ANi] Retry Contract - 01 "
                "[1080P][Baha][WEB-DL][AAC AVC][CHT].mp4</title>"
                f"<guid>http://host.docker.internal:{self.server.server_port}/retry.mp4</guid>"
                "</item></channel></rss>"
            ).encode()
            status = 200
        else:
            status = 403 if self.server.fail_download else 200
            body = b"" if status == 403 else PAYLOAD
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aria-image")
    args = parser.parse_args()
    server = ThreadingHTTPServer(("0.0.0.0", 0), Content)
    server.fail_download = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with container(args.aria_image or build_aria(), 6800) as (name, endpoint):
            downloader = Downloader(endpoint=endpoint + "/jsonrpc", secret="contract-test", timeout=3)
            engine = Aria2(downloader)
            try:
                wait_for(lambda: engine.rpc("getVersion"))
                with tempfile.TemporaryDirectory(prefix="aniopen-retry-") as directory:
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
                    cfg.download_retry.enabled = True
                    cfg.download_retry.delay_seconds = 5
                    config.save(cfg)
                    db = Database(data / "history.sqlite3")
                    service = Service(config, db, data)
                    try:
                        assert service.trigger()
                        wait_for(lambda: not service.running)
                        with service.poll_lock:
                            pass
                        old = db.task(1)["engine_id"]
                        wait_for(lambda: engine.rpc("tellStatus", old)["status"] == "error")
                        db.update("tasks", 1, next_observe_at=None)
                        service.poll(force=False)
                        assert db.task(1)["auto_retry_state"] == "waiting"
                        server.fail_download = False
                        db.update("tasks", 1, auto_retry_due_at=later(-1), next_observe_at=None)
                        service.poll(force=False)
                        row = db.task(1)
                        new = row["current_engine_id"]
                        assert new != old and row["auto_retry_count"] == 1, row["observation_error"]
                        wait_for(lambda: engine.observe(new)["status"] == "completed")
                        service.poll()
                        row = db.task(1)
                        assert row["status"] == "completed" and row["progress"] == 100
                        assert row["engine_id"] == old and db.tasks()["total"] == 1
                        assert engine.rpc("tellStatus", old)["status"] == "error"
                        assert len(db.attempt_ids(1)) == 2
                        old_options, new_options = engine.rpc("getOption", old), engine.rpc("getOption", new)
                        assert old_options["dir"] == new_options["dir"]
                        assert old_options["out"] == new_options["out"]
                        digest = docker("exec", name, "sha256sum", row["target_path"]).split()[0]
                        assert digest == hashlib.sha256(PAYLOAD).hexdigest()
                        print(
                            json.dumps(
                                {
                                    "engine": "disposable aria2",
                                    "http_403_retry": "passed",
                                    "new_gid_tracking": "passed",
                                    "preserved_path": "passed",
                                    "file_sha256": "passed",
                                    "original_failure_retained": "passed",
                                }
                            )
                        )
                    finally:
                        service.stop()
            finally:
                engine.close()
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


if __name__ == "__main__":
    main()
