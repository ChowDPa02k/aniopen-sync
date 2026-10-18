"""ANi parsing extracted from Kubespider (Apache-2.0); see NOTICE.

User mappings override online mappings, which override the built-in season rules.
"""

import logging
import posixpath
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from defusedxml import ElementTree as ET

from .config import Source

log = logging.getLogger(__name__)

DEFAULT_SEASON_MAPPER = {
    "第二季": 2,
    "第三季": 3,
    "第四季": 4,
    "第五季": 5,
    "第六季": 6,
    "第七季": 7,
    "第八季": 8,
    "第九季": 9,
    "第十季": 10,
}

ANIME_TITLE_PATTERN = re.compile(
    r"\[ANi\] (.+?) - (\d+(?:\.5)?) \[(.+?)\]\[(.+?)\]\[(.+?)\]\[(.+?)\]\[(.+?)\]\."
)

SEASON_RENAME_PATTERN = re.compile(r"- (\d+) (\[[^\]]+\]\[[^\]]+\])")


class AnimeReleaseInfo:
    def __init__(self, title: str, episode: str) -> None:
        self.title = title
        self.episode = episode

    @property
    def is_special_episode(self) -> bool:
        return self.episode.endswith(".5")


class SeasonContext:
    def __init__(self, season: int, keyword: str | None, reserve_keywords: str) -> None:
        self.season = season
        self.keyword = keyword
        self.reserve_keywords = reserve_keywords


@dataclass
class Release:
    identity: str
    url: str
    original_name: str
    file_name: str
    show_title: str
    season: int
    episode: str
    path: str
    sub_category: str = ""
    original_url: str = ""
    torrent_hashes: dict[str, str] = field(default_factory=dict)


def safe_component(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip().rstrip(". ")
    return value if value not in ("", ".", "..") else "_"


def safe_path(value: str) -> str:
    return "/".join(safe_component(part) for part in value.split("/") if part)


class AniParser:
    def __init__(self, cfg: Source, online: dict | None = None):
        self.cfg = cfg
        online = online or {}
        self.season_layers = [
            self._normalize_custom_season_mapping(cfg.model_dump()["custom_season_mapping"]),
            self._normalize_custom_season_mapping(online.get("custom_season_mapping", {})),
            DEFAULT_SEASON_MAPPER,
        ]
        self.adjustment_layers = [
            cfg.season_episode_adjustment,
            self._normalize_season_episode_adjustment(online.get("season_episode_adjustment", {})),
        ]
        self.custom_season_mapping = {
            **self._normalize_custom_season_mapping(online.get("custom_season_mapping", {})),
            **self._normalize_custom_season_mapping(cfg.model_dump()["custom_season_mapping"]),
        }
        self.season_episode_adjustment = {
            **self._normalize_season_episode_adjustment(online.get("season_episode_adjustment", {})),
            **cfg.season_episode_adjustment,
        }
        self.custom_category_mapping = cfg.custom_category_mapping

    def parse(self, xml: bytes) -> list[Release]:
        releases = []
        root = ET.fromstring(xml)
        if root.tag != "rss" or root.find("channel") is None:
            raise ValueError("订阅地址返回的不是 RSS，请检查上游是否维护或离线")
        for item in root.findall(".//item"):
            title = item.findtext("title") or ""
            info = self.get_anime_info(title)
            if info is None or info.is_special_episode or any(word in title for word in self.cfg.blacklist):
                continue
            enclosure = item.find("enclosure")
            url = (enclosure.get("url") if enclosure is not None else None) or item.findtext("link")
            guid = item.findtext("guid")
            if not url and guid and guid.startswith(("https://", "http://", "magnet:")):
                url = guid
            if not url or not url.startswith(("https://", "http://", "magnet:")):
                continue
            original_url = url
            parsed = urlsplit(url)
            if parsed.hostname in self.cfg.url_rewrites:
                host = self.cfg.url_rewrites[parsed.hostname]
                url = urlunsplit(parsed._replace(netloc=host + (f":{parsed.port}" if parsed.port else "")))
            context = self._get_season(info.title)
            episode = self._get_adjusted_episode(title, context.season, info.episode)
            if int(episode) < 0:
                log.warning("Skipping release with negative adjusted episode: %s", title)
                continue
            # Torrent names are immutable at submission; report the actual filename.
            filename = (
                title if self.cfg.api_type == "torrent" else self._rename_season(title, context, info.episode)
            )
            folder = safe_path(self.cfg.save_path)
            if self.cfg.classification_on_directory:
                folder = posixpath.join(folder, safe_component(info.title))
            sub = self._get_subcategory(info.title, context) if self.cfg.use_sub_category else ""
            releases.append(
                Release(
                    identity=guid
                    if guid and guid.startswith(("http:", "https:", "magnet:"))
                    else original_url,
                    original_url=original_url,
                    url=url,
                    original_name=title,
                    file_name=safe_component(filename) if self.cfg.api_type == "http" else filename,
                    show_title=self._replace_keyword(info.title, context).strip(),
                    season=context.season,
                    episode=episode,
                    path=folder,
                    sub_category=safe_path(sub),
                )
            )
        return releases

    def _get_custom_season_mapping_rule(self, keyword: str) -> tuple[int, str]:
        mapping = self.custom_season_mapping.get(keyword)
        if isinstance(mapping, dict):
            season = mapping.get("season")
            reserve_keywords = mapping.get("reserve_keywords", "")
            if season is None:
                log.warning("Invalid custom_season_mapping for %s: missing season field", keyword)
                return 1, reserve_keywords
            return season, reserve_keywords
        return mapping, ""

    def _normalize_custom_season_mapping(self, mapping: Any) -> dict[str, Any]:
        if not isinstance(mapping, dict):
            return {}
        normalized = {}
        for keyword, value in mapping.items():
            if isinstance(value, dict):
                season = value.get("season")
                if season is None:
                    log.warning("Invalid custom_season_mapping for %s: missing season field", keyword)
                    continue
                try:
                    if int(season) < 0:
                        continue
                    normalized[keyword] = {
                        "season": int(season),
                        "reserve_keywords": str(value.get("reserve_keywords", "")),
                    }
                except (TypeError, ValueError):
                    log.warning("Invalid custom_season_mapping season for %s: %s", keyword, season)
                continue
            try:
                if int(value) >= 0:
                    normalized[keyword] = int(value)
            except (TypeError, ValueError):
                log.warning("Invalid custom_season_mapping season for %s: %s", keyword, value)
        return normalized

    def _normalize_season_episode_adjustment(self, mapping: Any) -> dict[str, dict[int, int]]:
        if not isinstance(mapping, dict):
            return {}
        normalized = {}
        for title, season_mapping in mapping.items():
            if not isinstance(season_mapping, dict):
                log.warning("Invalid season_episode_adjustment for %s: %s", title, season_mapping)
                continue
            normalized_title_mapping = {}
            for season, offset in season_mapping.items():
                try:
                    normalized_title_mapping[int(season)] = int(offset)
                except (TypeError, ValueError):
                    log.warning(
                        "Invalid season_episode_adjustment for %s season %s: %s", title, season, offset
                    )
            if normalized_title_mapping:
                normalized[title] = normalized_title_mapping
        return normalized

    def get_season(self, title: str) -> tuple[int, str | None, str]:
        season_context = self._get_season(title)
        return season_context.season, season_context.keyword, season_context.reserve_keywords

    @staticmethod
    def _best_rule(title, rules):
        return next(
            iter(sorted((key for key in rules if key in title), key=lambda key: (-len(key), key))), None
        )

    def _get_season(self, title: str) -> SeasonContext:
        for layer in self.season_layers:
            keyword = self._best_rule(title, layer)
            if keyword is not None:
                rule = layer[keyword]
                if isinstance(rule, dict):
                    return SeasonContext(rule["season"], keyword, rule.get("reserve_keywords", ""))
                return SeasonContext(rule, keyword, "")
        return SeasonContext(1, None, "")

    def _replace_keyword(self, title: str, season_context: SeasonContext) -> str:
        if not season_context.keyword:
            return title
        replacement = f" {season_context.reserve_keywords}" if season_context.reserve_keywords else ""
        return re.sub(r"[ \t]+", " ", title.replace(season_context.keyword, replacement.strip())).strip()

    def _get_adjusted_episode(self, title: str, season: int, episode: str) -> str:
        for layer in self.adjustment_layers:
            eligible = {key: mapping for key, mapping in layer.items() if season in mapping}
            keyword = self._best_rule(title, eligible)
            if keyword is not None:
                return str(int(episode) + eligible[keyword][season]).zfill(2)
        return str(int(episode)).zfill(2)

    def _has_episode_adjustment(self, title: str, season: int) -> bool:
        for target_title, season_mapping in self.season_episode_adjustment.items():
            if target_title in title and season in season_mapping:
                return True
        return False

    def _rename_season(self, title: str, season_context: SeasonContext, episode: str) -> str:
        season_ = str(season_context.season).zfill(2)
        normalized_title = self._replace_keyword(title, season_context)
        adjusted_episode = self._get_adjusted_episode(title, season_context.season, episode)
        return SEASON_RENAME_PATTERN.sub(rf"- S{season_}E{adjusted_episode} \2", normalized_title)

    def rename_season(
        self, title: str, season: int, keyword: str | None, episode: str, reserve_keywords: str = ""
    ) -> str:
        season_context = SeasonContext(season=season, keyword=keyword, reserve_keywords=reserve_keywords)
        return self._rename_season(title, season_context, episode)

    def _get_subcategory(self, title: str, season_context: SeasonContext) -> str:
        # Custom subcategory mapping will cover any generated data
        for mapped_keyword, category in self.custom_category_mapping.items():
            if mapped_keyword in title:
                return category
        # Avoid '/' appear in original Anime title
        # This will be misleading for qbittorrent
        sub_category = title.replace("/", "_")
        if " - " in title:
            # Drop English Title
            sub_category = sub_category.split(" - ")[-1]
        if season_context.season > 1 and season_context.keyword:
            # Add Season subcategory
            season_ = str(season_context.season).zfill(2)
            sub_category = sub_category.replace(f" {season_context.keyword}", "") + f"/Season {season_}"
        # According to qbittorrent issue 19941
        # The Windows/linux illegal symbol of path will be automatically replaced with ' '
        # But if the last char of category string is illegal symbol
        # The replaced ' ' end of a path will occur unexpected bug in explorer
        if sub_category and sub_category[-1] in '<>:"/\\|?* ':
            sub_category = sub_category[:-1] + "_"
        # Idk why there's one more space ' ' between English Name and Chinese Name
        # The regex just looks fine
        if sub_category and sub_category[0] == " ":
            sub_category = sub_category[1:]
        return sub_category

    def get_anime_info(self, title: str) -> AnimeReleaseInfo | None:
        """Extract info by only REGEX, might be wrong in extreme cases."""
        matches = ANIME_TITLE_PATTERN.match(title)
        if matches is None:
            log.warning("Error while running regex on title %s", title)
            return None
        anime_title = matches.group(1)
        episode = matches.group(2)
        return AnimeReleaseInfo(title=anime_title, episode=episode)
