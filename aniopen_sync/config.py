from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

import yaml
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .storage import atomic_write


class ConfigLoader(yaml.SafeLoader):
    # The browser editor emits YAML 1.2: ISO dates and on/off/yes/no are strings.
    yaml_implicit_resolvers = {
        key: [
            (tag, pattern)
            for tag, pattern in entries
            if tag
            not in (
                "tag:yaml.org,2002:timestamp",
                "tag:yaml.org,2002:bool",
                "tag:yaml.org,2002:int",
                "tag:yaml.org,2002:float",
            )
        ]
        for key, entries in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }


ConfigLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|false)$", re.I), list("tTfF")
)

ConfigLoader.add_implicit_resolver(
    "tag:yaml.org,2002:int",
    re.compile(r"^(?:[-+]?[0-9]+|[-+]?0o[0-7]+|[-+]?0x[0-9a-fA-F]+)$"),
    list("-+0123456789"),
)
ConfigLoader.add_constructor(
    "tag:yaml.org,2002:int",
    lambda loader, node: (
        int(loader.construct_scalar(node), 0)
        if re.match(r"^[-+]?0[ox]", loader.construct_scalar(node))
        else int(loader.construct_scalar(node), 10)
    ),
)
ConfigLoader.add_implicit_resolver(
    "tag:yaml.org,2002:float",
    re.compile(r"^(?:[-+]?(?:[0-9]+\.[0-9]*|\.[0-9]+)(?:[eE][-+]?[0-9]+)?|[-+]?[0-9]+[eE][-+]?[0-9]+)$"),
    list("-+0123456789."),
)


def load_yaml(text):
    if not isinstance(text, (str, bytes)):
        raise ValueError("YAML 必须是文本")
    return yaml.load(text, Loader=ConfigLoader)


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Schedule(Model):
    enabled: bool = True
    timezone: str = "Asia/Shanghai"
    trigger: Literal["cron", "interval", "date"] = "cron"
    cron: dict[str, str | int] = Field(default_factory=lambda: {"minute": "6,36", "second": "30"})
    interval: dict[str, str | int | float] = Field(default_factory=lambda: {"minutes": 30})
    run_date: str = "2030-01-01T00:00:00+08:00"
    misfire_grace_time: int = Field(300, ge=1)
    coalesce: bool = True

    @model_validator(mode="after")
    def valid_trigger(self):
        build_trigger(self)
        return self


def build_trigger(cfg: Schedule):
    try:
        timezone = ZoneInfo(cfg.timezone)
        if cfg.trigger == "cron":
            allowed = {
                "year",
                "month",
                "day",
                "week",
                "day_of_week",
                "hour",
                "minute",
                "second",
                "start_date",
                "end_date",
                "jitter",
            }
            if set(cfg.cron) - allowed:
                raise ValueError("不支持的 cron 字段")
            return CronTrigger(timezone=timezone, **cfg.cron)
        if cfg.trigger == "interval":
            allowed = {"weeks", "days", "hours", "minutes", "seconds", "start_date", "end_date", "jitter"}
            if set(cfg.interval) - allowed:
                raise ValueError("不支持的 interval 字段")
            duration = sum(
                float(cfg.interval.get(key, 0)) * factor
                for key, factor in {
                    "weeks": 604800,
                    "days": 86400,
                    "hours": 3600,
                    "minutes": 60,
                    "seconds": 1,
                }.items()
            )
            if duration <= 0:
                raise ValueError("间隔必须大于 0")
            return IntervalTrigger(timezone=timezone, **cfg.interval)
        return DateTrigger(run_date=cfg.run_date, timezone=timezone)
    except Exception as exc:
        raise ValueError(f"无效定时规则: {exc}") from exc


class SeasonRule(Model):
    season: int = Field(ge=0)
    reserve_keywords: str = ""


class DownloadParams(Model):
    category: str = "ANi"
    tags: list[str] = Field(default_factory=lambda: ["ANi"])
    use_auto_torrent_management: bool = False


class Source(Model):
    enabled: bool = False
    api_type: Literal["http", "torrent"] = "http"
    downloader: str = "aria2"
    rss_link: str = "https://api.ani.rip/ani-download.xml"
    rss_link_torrent: str = "https://api.ani.rip/ani-torrent.xml"
    save_path: str = "TV/ANi"
    classification_on_directory: bool = True
    blacklist: list[str] = Field(default_factory=list)
    custom_season_mapping: dict[str, Annotated[int, Field(ge=0)] | SeasonRule] = Field(default_factory=dict)
    season_episode_adjustment: dict[str, dict[int, int]] = Field(default_factory=dict)
    use_sub_category: bool = True
    custom_category_mapping: dict[str, str] = Field(default_factory=dict)
    download_param: DownloadParams = Field(default_factory=DownloadParams)
    online_mappings_enabled: bool = True
    online_mappings_url: str = (
        "https://cdn.jsdelivr.net/gh/ChowDPa02k/ani-tmdb-mapper@main/mappings_kubespider.json"
    )
    proxy: str = ""
    request_timeout: int = Field(30, ge=1, le=300)
    max_feed_bytes: int = Field(4 * 1024 * 1024, ge=1024, le=32 * 1024 * 1024)
    max_torrent_bytes: int = Field(8 * 1024 * 1024, ge=1024, le=32 * 1024 * 1024)
    url_rewrites: dict[str, str] = Field(
        default_factory=lambda: {"resources.ani.rip": "cloud.ani-download.workers.dev"}
    )

    @model_validator(mode="after")
    def valid_rewrites(self):
        for origin, target in self.url_rewrites.items():
            if not origin or not target or any(char in origin + target for char in "/?#@: "):
                raise ValueError("域名替换规则只接受不含端口和路径的主机名")
        return self


class Downloader(Model):
    type: Literal["aria2", "qbittorrent"] = "aria2"
    enabled: bool = True
    endpoint: str = "http://127.0.0.1:6800/jsonrpc"
    download_base_path: str = "/downloads"
    secret: str = ""
    username: str = ""
    password: str = ""
    verify_certificate: bool = True
    timeout: int = Field(15, ge=1, le=120)

    @model_validator(mode="after")
    def valid_endpoint(self):
        from urllib.parse import urlsplit

        parsed = urlsplit(self.endpoint)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("endpoint 必须是 HTTP(S) 地址，凭据请使用独立字段")
        return self


class Tracking(Model):
    external_rebuild_enabled: bool = True
    terminal_watch_hours: int = Field(24, ge=1, le=720)
    error_poll_seconds: int = Field(300, ge=5, le=86400)
    completed_poll_seconds: int = Field(300, ge=5, le=86400)
    lost_poll_seconds: int = Field(60, ge=5, le=86400)
    max_tasks_per_poll: int = Field(1000, ge=10, le=10000)
    max_engine_results: int = Field(5000, ge=100, le=50000)


class Maintenance(Model):
    history_retention_days: int = Field(180, ge=1, le=3650)
    history_max_records: int = Field(20000, ge=100, le=1000000)
    retry_base_seconds: int = Field(60, ge=1, le=3600)
    retry_max_seconds: int = Field(3600, ge=60, le=86400)
    sync_on_startup: bool = True
    automatic_backups: bool = True
    backup_keep: int = Field(7, ge=1, le=30)


class Config(Model):
    schema_version: Literal[2] = 2
    tracking: Tracking = Field(default_factory=Tracking)
    maintenance: Maintenance = Field(default_factory=Maintenance)
    source: Source = Field(default_factory=Source)
    schedule: Schedule = Field(default_factory=Schedule)
    downloaders: dict[str, Downloader] = Field(
        default_factory=lambda: {
            "aria2": Downloader(),
            "qbittorrent": Downloader(type="qbittorrent", endpoint="http://127.0.0.1:8080", enabled=False),
        }
    )
    progress_poll_seconds: int = Field(15, ge=5, le=3600)

    @model_validator(mode="after")
    def compatible_downloader(self):
        selected = self.downloaders.get(self.source.downloader)
        if not selected:
            raise ValueError("source.downloader 必须指向已配置的下载器")
        if self.source.enabled and not selected.enabled:
            raise ValueError("请先启用选择的下载器")
        if self.source.api_type == "http" and selected.type != "aria2":
            raise ValueError("HTTP 直链下载需要 aria2")
        return self


class ConfigStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        if path.exists():
            payload = load_yaml(path.read_text()) or {}
            if not isinstance(payload, dict):
                raise ValueError("配置必须是 YAML 对象")
            old_version = payload.get("schema_version", 0)
            if not isinstance(old_version, int):
                raise ValueError("schema_version 必须是整数")
            if old_version > 2:
                raise ValueError("配置来自更新版本，请升级程序；不会覆盖原文件")
            payload = {**payload, "schema_version": 2}
            self.value = Config.model_validate(payload)
            if old_version < 2:
                atomic_write(path.with_suffix(".pre-v2.yaml"), path.read_bytes())
                self.save(self.value)
        else:
            self.value = Config()
            self.save(self.value)

    def get(self) -> Config:
        with self.lock:
            return self.value.model_copy(deep=True)

    def save(self, config: Config):
        with self.lock:
            atomic_write(self.path, yaml.safe_dump(config.model_dump(), allow_unicode=True, sort_keys=False))
            self.value = config.model_copy(deep=True)
