"""Fault injection against a disposable s6 container and local fake dependencies. No production access."""

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import requests
from check_downloaders import container, docker, wait_for


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    state = SimpleNamespace(rss_ok=True, downloader_ok=True, mutations=0)

    class Dependencies(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, body, status=200):
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.reply(b"<rss><channel/></rss>" if state.rss_ok else b"<html>maintenance</html>")

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if payload["method"] != "aria2.getGlobalStat":
                state.mutations += 1
                return self.reply(b"unexpected RPC", 500)
            self.reply(
                json.dumps({"jsonrpc": "2.0", "id": payload["id"], "result": {"numActive": "0"}}).encode()
                if state.downloader_ok
                else b"private?token=never-leak-test-secret",
                200 if state.downloader_ok else 503,
            )

    server = ThreadingHTTPServer(("0.0.0.0", 0), Dependencies)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with container(
            args.image,
            8090,
            "--read-only",
            "--user",
            "1001:1001",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--tmpfs",
            "/run:rw,exec,nosuid,nodev,size=16m,mode=0700,uid=1001,gid=1001",
            "--tmpfs",
            "/tmp:rw,nosuid,noexec,size=32m,mode=1777",
            "--tmpfs",
            "/data:rw,nosuid,noexec,size=32m,mode=0700,uid=1001,gid=1001",
            "--health-interval",
            "1s",
            "--health-start-period",
            "1s",
            "--health-retries",
            "1",
            "-e",
            "ANIOPEN_API_TOKEN=container-health-test-only",
        ) as (name, url):
            with requests.Session() as session:
                session.trust_env = False
                session.headers["Authorization"] = "Bearer container-health-test-only"
                wait_for(lambda: session.get(url + "/api/health", timeout=2).status_code == 200)
                assert docker("exec", name, "id", "-u") == "1001"
                assert docker("exec", name, "cat", "/proc/1/comm") == "s6-svscan"
                cfg = session.get(url + "/api/config", timeout=3).json()["config"]
                base = f"http://host.docker.internal:{server.server_port}"
                cfg["schedule"]["enabled"] = False
                cfg["maintenance"]["sync_on_startup"] = False
                cfg["source"].update(enabled=True, rss_link=base + "/feed", online_mappings_enabled=False)
                cfg["downloaders"]["aria2"]["endpoint"] = base + "/jsonrpc"

                def check(expected, component=None):
                    # Wake the real monitor by saving a new configuration, without waiting 60s.
                    cfg["progress_poll_seconds"] = 16 if cfg["progress_poll_seconds"] == 15 else 15
                    session.put(url + "/api/config", json=cfg, timeout=3).raise_for_status()

                    def ready():
                        response = session.get(url + "/api/health/ready", timeout=3)
                        body = response.json()
                        if body["status"] != expected:
                            return False
                        if (
                            component
                            and body["checks"].get(component, {}).get("code") != component + "_unavailable"
                        ):
                            return False
                        return body

                    wait_for(ready)
                    expected_docker = "healthy" if expected == "ok" else "unhealthy"
                    wait_for(
                        lambda: (
                            docker("inspect", "--format", "{{.State.Health.Status}}", name) == expected_docker
                        )
                    )
                    output = docker("inspect", "--format", "{{json .State.Health}}", name)
                    assert "never-leak-test-secret" not in output
                    assert session.get(url + "/api/health", timeout=3).status_code == 200
                    assert session.get(url + "/api/config", timeout=3).status_code == 200
                    if component:
                        assert component + "_unavailable" in output

                check("ok")
                state.rss_ok = False
                check("warning", "rss")
                state.rss_ok = True
                check("ok")
                state.downloader_ok = False
                check("warning", "downloader")
                state.downloader_ok = True
                check("ok")
                assert state.mutations == 0
                assert session.get(url + "/api/tasks", timeout=3).json()["total"] == 0
                docker("stop", "--time", "15", name)
                assert docker("inspect", "--format", "{{.State.ExitCode}}", name) in ("0", "143")
                docker("start", name)
                wait_for(lambda: docker("inspect", "--format", "{{.State.Health.Status}}", name) == "healthy")
                # A main-process crash must exit the container, allowing its restart policy to act.
                kill = """import os,signal
from pathlib import Path
for path in Path('/proc').glob('[0-9]*/cmdline'):
 try:
  args=path.read_bytes().split(b'\\0')
  if b'/app/.venv/bin/aniopen-sync' in args and b'--host' in args:
   os.kill(int(path.parent.name),signal.SIGKILL);break
 except (OSError,ProcessLookupError):pass
else:raise SystemExit('Main process not found')
"""
                docker("exec", name, "python", "-c", kill)
                wait_for(lambda: docker("inspect", "--format", "{{.State.Status}}", name) == "exited")
                assert docker("inspect", "--format", "{{.State.ExitCode}}", name) == "137"
                print(
                    json.dumps(
                        {
                            "s6_pid1": "passed",
                            "nonroot_uid": 1001,
                            "rss_fault_recovery": "passed",
                            "downloader_fault_recovery": "passed",
                            "graceful_stop": "passed",
                            "main_crash_exit": "passed",
                            "download_submissions": 0,
                        }
                    )
                )
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


if __name__ == "__main__":
    main()
