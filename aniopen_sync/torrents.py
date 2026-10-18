"""Content identities shared by torrent submission and read-only reconciliation."""

import base64
import hashlib
import re
from urllib.parse import parse_qs, urlsplit

import bencodepy


def magnet_hashes(url):
    result = {}
    for value in parse_qs(urlsplit(url).query).get("xt", []):
        if value.startswith("urn:btih:"):
            value = value[9:]
            if len(value) == 32:
                value = base64.b32decode(value.upper()).hex()
            if not re.fullmatch("[a-fA-F0-9]{40}", value):
                raise ValueError("无效的 v1 infohash")
            result["v1"] = value.lower()
        elif value.startswith("urn:btmh:1220"):
            value = value[len("urn:btmh:1220") :]
            if not re.fullmatch("[a-fA-F0-9]{64}", value):
                raise ValueError("无效的 v2 infohash")
            result["v2"] = value.lower()
    if not result:
        raise ValueError("磁力链接缺少支持的 btih / btmh infohash")
    return result


def torrent_info(content):
    try:
        info = bencodepy.decode(content)[b"info"]
        encoded = bencodepy.encode(info)
        hashes = {}
        if b"pieces" in info:
            hashes["v1"] = hashlib.sha1(encoded).hexdigest()
        if info.get(b"meta version") == 2:
            hashes["v2"] = hashlib.sha256(encoded).hexdigest()
        if not hashes:
            raise ValueError("Missing torrent content identity")
        name = info.get(b"name.utf-8", info[b"name"]).decode("utf-8", errors="replace")
        return hashes, name
    except Exception as exc:
        raise ValueError("下载链接未返回有效的 torrent 文件") from exc


def torrent_id(hashes):
    return hashes["v2"][:40] if hashes.get("v2") else hashes["v1"]


def hash_aliases(hashes):
    return set(filter(None, [hashes.get("v1"), hashes.get("v2"), hashes.get("v2", "")[:40]]))
