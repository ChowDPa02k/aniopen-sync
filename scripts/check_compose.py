"""Cold Compose startup, persistent credentials, masked edits, restart and backup smoke checks.

Uses a random project and a disposable volume. --build-proxy is only for restricted build networks.
"""

import argparse
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import requests
import yaml
from check_downloaders import wait_for


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-proxy")
    parser.add_argument("--browser", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    project = "aniopen-smoke-" + uuid.uuid4().hex[:10]
    environment = {**os.environ, "ANIOPEN_PORT": "0", "ANIOPEN_BIND_ADDRESS": "127.0.0.1"}
    environment.pop("ANIOPEN_API_TOKEN", None)
    with tempfile.TemporaryDirectory(prefix="aniopen-compose-") as directory:
        command = ["docker", "compose", "-p", project, "-f", str(root / "compose.yaml")]
        if args.build_proxy:
            override = Path(directory) / "build-proxy.yaml"
            override.write_text(
                yaml.safe_dump(
                    {
                        "services": {
                            "aniopen-sync": {
                                "build": {
                                    "args": {"HTTP_PROXY": args.build_proxy, "HTTPS_PROXY": args.build_proxy}
                                }
                            }
                        }
                    }
                )
            )
            command += ["-f", str(override)]

        def compose(*arguments):
            result = subprocess.run(
                [*command, *arguments], cwd=root, env=environment, capture_output=True, text=True, timeout=600
            )
            if result.returncode:
                raise RuntimeError(f"Compose {arguments[0]} failed: {result.stderr[-1500:]}")
            return result.stdout.strip()

        try:
            compose("up", "-d", "--build")
            port = compose("port", "aniopen-sync", "8090")
            url = "http://" + port
            session = requests.Session()
            session.trust_env = False
            wait_for(lambda: session.get(url + "/api/health", timeout=2).status_code == 200)
            assert session.get(url + "/api/config", timeout=3).status_code == 401
            token = compose("exec", "-T", "aniopen-sync", "aniopen-sync", "token").splitlines()[-1]
            session.headers["Authorization"] = "Bearer " + token
            assert session.get(url + "/").status_code == 200
            cfg = session.get(url + "/api/config").json()["config"]
            assert not cfg["source"]["enabled"]
            assert cfg["schedule"]["cron"] == {"minute": "6,36", "second": "30"}
            cfg["downloaders"]["aria2"]["secret"] = "smoke-only-secret"
            assert session.put(url + "/api/config", json=cfg).status_code == 200
            body = session.get(url + "/api/config")
            assert body.headers["Cache-Control"] == "no-store" and "smoke-only-secret" not in body.text
            assert body.json()["config"]["downloaders"]["aria2"]["secret"] == "********"
            if args.browser:
                result = subprocess.run(
                    ["node", str(root / "web/test/browser.mjs")],
                    cwd=root,
                    env={**environment, "ANIOPEN_TEST_URL": url, "ANIOPEN_TEST_TOKEN": token},
                    timeout=180,
                )
                if result.returncode:
                    raise AssertionError("Browser contract checks failed")
            compose("restart", "aniopen-sync")
            # Docker may allocate another published port when a port-0 container restarts.
            url = "http://" + compose("port", "aniopen-sync", "8090")
            wait_for(lambda: session.get(url + "/api/status", timeout=3).status_code == 200)
            assert compose("exec", "-T", "aniopen-sync", "aniopen-sync", "token").splitlines()[-1] == token
            uid = compose("exec", "-T", "aniopen-sync", "id", "-u")
            assert uid == "10001"
            compose(
                "exec",
                "-T",
                "aniopen-sync",
                "aniopen-sync",
                "backup",
                "--output",
                "/data/backups/manual-smoke.tar.gz",
            )
            check = "import tarfile; p='/data/backups/manual-smoke.tar.gz'; t=tarfile.open(p); assert {'history.sqlite3','config.yaml','access-token'} <= set(t.getnames()); t.close()"
            compose("exec", "-T", "aniopen-sync", "python", "-c", check)
            assert session.get(url + "/api/tasks").json()["total"] == 0
            session.close()
            subprocess.run(
                [
                    sys.executable,
                    str(root / "scripts/check_container_health.py"),
                    "--image",
                    compose("images", "-q", "aniopen-sync"),
                ],
                cwd=root,
                check=True,
                timeout=180,
            )
            print(
                json.dumps(
                    {
                        "compose_cold_start": "passed",
                        "default_authentication": "passed",
                        "credential_masking": "passed",
                        "persistent_token_and_restart": "passed",
                        "uid": uid,
                        "online_backup": "passed",
                        "rss_submissions": 0,
                    }
                )
            )
        finally:
            with contextlib.suppress(Exception):
                compose("down", "--volumes", "--remove-orphans", "--rmi", "local")


if __name__ == "__main__":
    main()
