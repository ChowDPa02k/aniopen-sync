"""Import local Kubespider YAML and dedup state without contacting any downloader."""

import argparse
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import yaml
from filelock import FileLock

from .config import Config, ConfigLoader, ConfigStore, Downloader, load_yaml
from .db import Database


def endpoint(host, port, suffix=""):
    host = str(host)
    parsed = urlsplit(host if "://" in host else "http://" + host)
    hostname = parsed.hostname or "127.0.0.1"
    if ":" in hostname:
        hostname = f"[{hostname}]"
    netloc = f"{hostname}:{parsed.port or port}"
    path = parsed.path.rstrip("/")
    if suffix and not path.endswith(suffix):
        path += suffix
    return urlunsplit((parsed.scheme or "http", netloc, path, parsed.query, ""))


def migrate(old_dir: Path, data_dir: Path, provider_name: str | None = None):
    def read(name):
        path = old_dir / name
        return load_yaml(path.read_text()) or {} if path.exists() else {}

    sources = read("source_provider.yaml")
    if provider_name:
        source = sources[provider_name]
    else:
        candidates = [(key, cfg) for key, cfg in sources.items() if cfg.get("type") == "ani_source_provider"]
        if len(candidates) != 1:
            raise ValueError("需要恰好一个 ANi provider，或使用 --provider 指定名称")
        provider_name, source = candidates[0]
    if source.get("type") != "ani_source_provider":
        raise ValueError("指定 provider 不是 ANi provider")
    legacy_downloaders = read("download_provider.yaml")
    raw_credentials = {}
    node = yaml.compose((old_dir / "download_provider.yaml").read_text(), Loader=ConfigLoader)
    if isinstance(node, yaml.MappingNode):
        for name_node, value_node in node.value:
            if isinstance(value_node, yaml.MappingNode):
                raw_credentials[name_node.value] = {
                    key.value: value.value
                    for key, value in value_node.value
                    if key.value in ("username", "password", "secret")
                    and isinstance(value, yaml.ScalarNode)
                    and value.tag != "tag:yaml.org,2002:null"
                }
    config = Config()
    config.source = config.source.model_validate(
        {
            key: value
            for key, value in source.items()
            if key in type(config.source).model_fields
            and key not in ("enabled", "downloader", "download_param", "blacklist")
            and value is not None
        }
        | {
            "blacklist": (
                [source["blacklist"]]
                if isinstance(source.get("blacklist"), str)
                else [str(word) for word in source.get("blacklist") or []]
            ),
            "enabled": False,
        }
    )
    config.downloaders = {}
    for name, cfg in legacy_downloaders.items():
        kind = cfg.get("type")
        if kind not in ("aria2_download_provider", "qbittorrent_download_provider"):
            continue
        aria = kind == "aria2_download_provider"
        config.downloaders[name] = Downloader(
            type="aria2" if aria else "qbittorrent",
            enabled=cfg.get("enable", True),
            endpoint=endpoint(
                cfg.get("rpc_endpoint_host" if aria else "http_endpoint_host", "http://127.0.0.1"),
                cfg.get("rpc_endpoint_port" if aria else "http_endpoint_port", 6800 if aria else 8080),
                "/jsonrpc" if aria else "",
            ),
            download_base_path=str(cfg.get("download_base_path") or "/downloads"),
            **{key: raw_credentials.get(name, {}).get(key, "") for key in ("secret", "username", "password")},
            verify_certificate=cfg.get("verify_webui_certificate", True),
        )
    preferred = source.get("downloader") or []
    if isinstance(preferred, str):
        preferred = [preferred]
    compatible = [
        name
        for name, cfg in config.downloaders.items()
        if cfg.enabled and (config.source.api_type == "torrent" or cfg.type == "aria2")
    ]
    compatible.sort(
        key=lambda name: (name not in preferred, int(legacy_downloaders[name].get("priority") or 99))
    )
    if not compatible:
        raise ValueError("没有兼容且已启用的 aria2 / qBittorrent 下载器")
    selected = compatible[0]
    config.source.downloader = selected
    selected_config = legacy_downloaders[selected]
    params = {
        key: selected_config[key]
        for key in ("category", "tags", "use_auto_torrent_management")
        if key in selected_config and selected_config[key] is not None
    }
    params.update(
        {
            key: value
            for key, value in (source.get("download_param") or {}).items()
            if key in ("category", "tags", "use_auto_torrent_management") and value is not None
        }
    )
    if isinstance(params.get("tags"), str):
        params["tags"] = [value.strip() for value in params["tags"].split(",") if value.strip()]
    if "category" in params:
        params["category"] = str(params["category"])
    config.source.download_param = config.source.download_param.model_validate(params)
    config.source.proxy = str(read("kubespider.yaml").get("proxy", "") or "")
    config = Config.model_validate(config.model_dump())
    state = read("state.yaml").get(provider_name) or []
    data_dir.mkdir(parents=True, exist_ok=True)
    with FileLock(str(data_dir / ".instance.lock"), timeout=0):
        if (data_dir / "config.yaml").exists() or (data_dir / "history.sqlite3").exists():
            raise ValueError("目标目录已有配置或历史，请使用新的空目录")
        ConfigStore(data_dir / "config.yaml").save(config)
        db = Database(data_dir / "history.sqlite3")
        with db.connect() as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO legacy_seen VALUES (?)", [(str(uid),) for uid in state]
            )
    return len(state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="old_dir", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--provider")
    args = parser.parse_args()
    count = migrate(args.old_dir, args.data_dir, args.provider)
    print(f"导入完成：{count} 条去重标识。订阅默认关闭，请检查 WebUI 配置后启用。")


if __name__ == "__main__":
    main()
