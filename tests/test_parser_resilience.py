import pytest
from conftest import TITLE

from aniopen_sync.config import ConfigStore, Source, load_yaml
from aniopen_sync.parser import AniParser


def feed(title=TITLE, url="https://resources.ani.rip/episode.mp4"):
    return f"<rss><channel><item><title>{title}</title><guid>{url}</guid></item></channel></rss>".encode()


def test_cross_layer_priority_and_non_additive_offsets():
    online = {
        "custom_season_mapping": {"第二季": 2, "測試動畫": 3},
        "season_episode_adjustment": {"測試動畫": {4: 48}},
    }
    cfg = Source(custom_season_mapping={"第二季": 4}, season_episode_adjustment={"測試": {4: 0}})
    release = AniParser(cfg, online).parse(feed())[0]
    assert (release.season, release.episode) == (4, "13")
    assert "S04E13" in release.file_name


def test_specificity_is_independent_of_mapping_order():
    for values in ({"動畫": 3, "測試動畫": 4}, {"測試動畫": 4, "動畫": 3}):
        parser = AniParser(
            Source(
                custom_season_mapping=values,
                season_episode_adjustment={"動畫": {4: 20}, "測試動畫": {4: -12}},
            )
        )
        release = parser.parse(feed())[0]
        assert (release.season, release.episode) == (4, "01")


@pytest.mark.parametrize(
    "platform,resolution", [("AT-X", "1440P"), ("Netflix", "4K"), ("NewPlatform", "2160P")]
)
def test_all_accepted_platforms_have_consistent_naming(platform, resolution):
    title = TITLE.replace("Baha", platform).replace("1080P", resolution)
    assert "S02E13" in AniParser(Source()).parse(feed(title))[0].file_name


def test_url_rewrite_is_explicit_optional_and_identity_stable():
    original = AniParser(Source(url_rewrites={})).parse(feed())[0]
    rewritten = AniParser(Source(url_rewrites={"resources.ani.rip": "cdn.example"})).parse(feed())[0]
    assert original.url == "https://resources.ani.rip/episode.mp4"
    assert rewritten.url == "https://cdn.example/episode.mp4"
    assert original.identity == rewritten.identity
    assert rewritten.original_url == original.url


def test_constant_non_url_guid_cannot_collapse_distinct_releases():
    xml = (
        "<rss><channel>"
        + "".join(
            f'<item><title>{TITLE}</title><guid>constant</guid><enclosure url="https://cdn.example/{index}.mp4"/></item>'
            for index in range(2)
        )
        + "</channel></rss>"
    )
    releases = AniParser(Source()).parse(xml.encode())
    assert releases[0].identity != releases[1].identity


def test_yaml_12_decimal_and_timestamp_semantics():
    values = load_yaml("a: 012\nb: 1:30\nc: 0o12\nd: 1e3\ne: off\nf: true\ng: 2030-01-01\n")
    assert values == {"a": 12, "b": "1:30", "c": 10, "d": 1000.0, "e": "off", "f": True, "g": "2030-01-01"}


def test_config_upgrade_backup_and_future_version_rejection(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("source:\n  enabled: false\n")
    store = ConfigStore(path)
    assert store.get().schema_version == 2
    assert (tmp_path / "config.pre-v2.yaml").read_text() == "source:\n  enabled: false\n"
    path.write_text("schema_version: 99\n")
    with pytest.raises(ValueError, match="更新版本"):
        ConfigStore(path)
    assert path.read_text() == "schema_version: 99\n"


def test_no_guid_identity_is_stable_across_rewrite_changes():
    xml = (
        b"<rss><channel><item><title>"
        + TITLE.encode()
        + b'</title><enclosure url="https://resources.ani.rip/episode.mp4"/></item></channel></rss>'
    )
    original = AniParser(Source(url_rewrites={})).parse(xml)[0]
    rewritten = AniParser(Source(url_rewrites={"resources.ani.rip": "new.example"})).parse(xml)[0]
    assert original.identity == rewritten.identity == "https://resources.ani.rip/episode.mp4"


@pytest.mark.parametrize("rule", [0, {"season": 0, "reserve_keywords": "特別篇"}])
def test_user_season_zero_is_preserved_and_overrides_online(rule):
    title = TITLE.replace("第二季", "特別篇")
    source = Source(custom_season_mapping={"特別篇": rule})
    release = AniParser(source, {"custom_season_mapping": {"測試動畫": 3}}).parse(feed(title))[0]
    assert release.season == 0 and "S00E13" in release.file_name


@pytest.mark.parametrize("rule", [-1, {"season": -1}])
def test_negative_season_mapping_is_rejected(rule):
    with pytest.raises(ValueError):
        Source(custom_season_mapping={"特別篇": rule})
