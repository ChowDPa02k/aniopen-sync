from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from conftest import TITLE
from defusedxml.common import DefusedXmlException
from pydantic import ValidationError

from aniopen_sync.config import Config, Schedule, Source, build_trigger
from aniopen_sync.parser import AniParser


def xml(title=TITLE, guid="https://resources.ani.rip/episode.mp4"):
    return f"<rss><channel><item><title>{title}</title><guid>{guid}</guid></item></channel></rss>".encode()


def test_default_cron_exact_seconds_across_restart():
    trigger = build_trigger(Schedule())
    start = datetime(2026, 10, 18, 12, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    first = trigger.get_next_fire_time(None, start)
    second = trigger.get_next_fire_time(first, first)
    third = trigger.get_next_fire_time(second, second)
    assert [(d.hour, d.minute, d.second) for d in (first, second, third)] == [
        (12, 6, 30),
        (12, 36, 30),
        (13, 6, 30),
    ]
    restarted = build_trigger(Schedule())
    assert restarted.get_next_fire_time(None, start.replace(minute=20)).minute == 36


@pytest.mark.parametrize(
    "values",
    [
        {"cron": {"minute": "60"}},
        {"timezone": "not/a/zone"},
        {"trigger": "interval", "interval": {"seconds": 0}},
        {"cron": {"unexpected": "x"}},
        {"trigger": "date", "run_date": "nonsense"},
    ],
)
def test_invalid_schedules_rejected(values):
    with pytest.raises(ValidationError):
        Schedule(**values)


def test_interval_and_date():
    trigger = build_trigger(Schedule(trigger="interval", interval={"minutes": 30}))
    assert trigger.interval.total_seconds() == 1800
    trigger = build_trigger(Schedule(trigger="date", run_date="2026-10-18T12:06:30+08:00"))
    assert trigger.run_date.second == 30


def test_builtin_custom_and_online_priority():
    online = {
        "custom_season_mapping": {"第二季": {"season": 3, "reserve_keywords": "续篇"}},
        "season_episode_adjustment": {"測試動畫": {"3": -12}},
    }
    parsed = AniParser(Source(), online).parse(xml())[0]
    assert parsed.season == 3 and parsed.episode == "01"
    assert parsed.show_title == "測試動畫 续篇"
    parsed = AniParser(
        Source(custom_season_mapping={"第二季": {"season": 4, "reserve_keywords": "新篇"}}), online
    ).parse(xml())[0]
    assert parsed.season == 4 and parsed.episode == "13"
    assert parsed.show_title == "測試動畫 新篇"


def test_whole_title_rule_and_season_one_adjustment():
    parser = AniParser(
        Source(
            custom_season_mapping={"測試動畫 第二季": {"season": 1, "reserve_keywords": "測試動畫"}},
            season_episode_adjustment={"測試動畫": {1: 48}},
        )
    )
    release = parser.parse(xml())[0]
    assert release.show_title == "測試動畫"
    assert release.file_name == "[ANi] 測試動畫 - S01E61 [1080P][Baha][WEB-DL][AAC AVC][CHT].mp4"


def test_blacklist_special_and_bad_items():
    assert not AniParser(Source(blacklist=["測試動畫"])).parse(xml())
    assert not AniParser(Source()).parse(xml(TITLE.replace(" - 13 ", " - 12.5 ")))
    assert not AniParser(Source()).parse(xml("not an ANi release"))
    assert not AniParser(Source()).parse(xml(guid="non-url-guid"))


def test_paths_never_escape_base_and_fallback_enclosure():
    parser = AniParser(Source(save_path="../../TV/ANi"))
    release = parser.parse(xml(TITLE.replace("測試動畫", "../測試動畫")))[0]
    assert ".." not in release.path.split("/")
    assert release.url == "https://cloud.ani-download.workers.dev/episode.mp4"
    feed = f'<rss><channel><item><title>{TITLE}</title><guid isPermaLink="false">key</guid><enclosure url="https://cdn.example/file.mp4"/></item></channel></rss>'
    release = parser.parse(feed.encode())[0]
    assert (
        release.url == "https://cdn.example/file.mp4" and release.identity == "https://cdn.example/file.mp4"
    )


def test_xml_entities_rejected():
    with pytest.raises(DefusedXmlException):
        AniParser(Source()).parse(
            b'<!DOCTYPE rss [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><rss>&xxe;</rss>'
        )


def test_unknown_config_and_incompatible_engine_rejected():
    with pytest.raises(ValidationError):
        Config.model_validate({"unknown": 1})
    with pytest.raises(ValidationError):
        Config.model_validate({"source": {"api_type": "http", "downloader": "qbittorrent"}})
