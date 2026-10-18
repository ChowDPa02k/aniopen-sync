"""Bounded upstream reads; retries apply only to idempotent reads."""

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def upstream_session(proxy=""):
    session = requests.Session()
    session.headers["User-Agent"] = "ANiOpen-Sync/0.2"
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    retry = Retry(
        total=2,
        backoff_factor=0.5,
        status_forcelist=[429, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
        respect_retry_after_header=False,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.mount("http://", HTTPAdapter(max_retries=retry))
    return session


def fetch_bytes(session, url, timeout, limit):
    with session.get(url, timeout=timeout, stream=True) as response:
        response.raise_for_status()
        if int(response.headers.get("Content-Length", 0)) > limit:
            raise ValueError("上游响应超过大小限制")
        content = bytearray()
        for chunk in response.iter_content(65536):
            content.extend(chunk)
            if len(content) > limit:
                raise ValueError("上游响应超过大小限制")
        return bytes(content)
