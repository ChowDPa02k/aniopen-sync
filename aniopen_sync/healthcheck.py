"""Docker healthcheck: print sanitized readiness JSON and exit 0 (ok) or 1 (warning)."""

import json
import os
import sys
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener


def check(url):
    try:
        opener = build_opener(ProxyHandler({}))
        try:
            response = opener.open(url, timeout=2)
        except HTTPError as exc:
            response = exc
        with response:
            payload = json.loads(response.read(16384))
            if response.code == 200 and payload.get("status") == "ok":
                return 0, payload
            if response.code == 503 and payload.get("status") == "warning":
                return 1, payload
    except Exception:
        pass
    return 1, {"status": "warning", "checks": {"application": {"status": "warning", "code": "unavailable"}}}


def main():
    code, payload = check(os.environ.get("ANIOPEN_HEALTH_URL", "http://127.0.0.1:8090/api/health/ready"))
    print(json.dumps(payload, ensure_ascii=False))
    sys.exit(code)


if __name__ == "__main__":
    main()
