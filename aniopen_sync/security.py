"""Single-administrator access, secure bootstrap and masked configuration editing."""

import ipaddress
import logging
import os
import secrets
from urllib.parse import urlsplit

from .storage import atomic_write

REDACTED = "********"
log = logging.getLogger(__name__)


def access_token(data_dir):
    configured = os.environ.get("ANIOPEN_API_TOKEN", "")
    path = data_dir / "access-token"
    if configured:
        token = configured
    elif path.exists():
        token = path.read_text().strip()
    else:
        token = secrets.token_urlsafe(32)
        atomic_write(path, token + "\n")
        log.warning("首次启动访问令牌: %s", token)
    if not token or not token.isascii() or any(char.isspace() for char in token):
        raise ValueError(
            "访问令牌必须是非空、不含空白的 ASCII 文本；请检查 ANIOPEN_API_TOKEN 或 access-token 文件"
        )
    return token


def allowed_host(host):
    try:
        name = urlsplit("//" + host).hostname or ""
    except ValueError:
        return False
    configured = os.environ.get("ANIOPEN_ALLOWED_HOSTS", "").lower().split(",")
    if name.lower() in {"localhost", "aniopen-sync", *[v.strip() for v in configured if v.strip()]}:
        return True
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


def mask_config(config):
    payload = config.model_dump()
    for downloader in payload["downloaders"].values():
        for key in ("secret", "password"):
            if downloader[key]:
                downloader[key] = REDACTED
    return payload


def preserve_secrets(payload, current):
    if not isinstance(payload, dict):
        raise ValueError("配置必须是对象")
    payload = {**payload}
    if isinstance(payload.get("downloaders"), dict):
        downloaders = {}
        for name, value in payload["downloaders"].items():
            if not isinstance(value, dict):
                downloaders[name] = value
                continue
            value = {**value}
            for key in ("secret", "password"):
                if value.get(key) == REDACTED:
                    if name not in current.downloaders:
                        raise ValueError("新下载器连接需要填写真实密钥，不能使用脱敏占位符")
                    value[key] = getattr(current.downloaders[name], key)
            downloaders[name] = value
        payload["downloaders"] = downloaders
    return payload
