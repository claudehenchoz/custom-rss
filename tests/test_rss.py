from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import pytest

from custom_rss.models import FeedConfig, FeedItem, Media, Source
from custom_rss.rss import ATOM_NS, MEDIA_NS, render_feed
from custom_rss.schedule import Cadence, Schedule


def make_config(**overrides: object) -> FeedConfig:
    values: dict[str, object] = {
        "slug": "demo",
        "title": "Demo Feed",
        "description": "Demo description.",
        "link": "https://example.test/",
        "self_url": "https://example.test/feeds/demo/feed.xml",
        "language": "en-gb",
        "ttl": 60,
        "max_items": 100,
        "directory": Path("/nonexistent"),
        "settings": {},
        "schedule": Schedule(Cadence.HOUR),
    }
    values.update(overrides)
    return FeedConfig(**values)  # type: ignore[arg-type]


def item(**overrides: object) -> FeedItem:
    values: dict[str, object] = {
        "title": "Story",
        "link": "https://example.test/story",
        "guid": "https://example.test/story",
        "description": "Body.",
        "published": datetime(2025, 6, 3, 10, 0, tzinfo=timezone.utc),
        "guid_is_permalink": True,
    }
    values.update(overrides)
    return FeedItem(**values)  # type: ignore[arg-type]


def parse(payload: bytes) -> ET.Element:
    return ET.fromstring(payload)


def test_channel_contract_and_atom_self_link() -> None:
    payload = render_feed(make_config(), [item()])
    root = parse(payload)
    channel = root.find("channel")
    assert root.tag == "rss" and root.attrib["version"] == "2.0"
    assert channel is not None
    assert channel.findtext("title") == "Demo Feed"
    assert channel.findtext("link") == "https://example.test/"
    assert channel.findtext("description") == "Demo description."
    assert channel.findtext("language") == "en-gb"
    assert channel.findtext("ttl") == "60"
    assert channel.findtext("generator") == "custom-rss/1.0"
    self_link = channel.find(f"{{{ATOM_NS}}}link")
    assert self_link is not None
    assert self_link.attrib == {
        "href": "https://example.test/feeds/demo/feed.xml",
        "rel": "self",
        "type": "application/rss+xml",
    }
    assert channel.find("lastBuildDate") is None
    assert payload.startswith(b'<?xml version="1.0" encoding="utf-8"?>\n')


def test_guid_modes_are_emitted_verbatim() -> None:
    payload = render_feed(
        make_config(),
        [
            item(title="Permalink", guid="https://example.test/a", guid_is_permalink=True),
            item(
                title="Product",
                guid="9PNJXVCVWD4K",
                guid_is_permalink=False,
                link="https://www.xbox.com/en-us/games/store/game/9PNJXVCVWD4K",
            ),
        ],
    )
    items = {element.findtext("title"): element for element in parse(payload).findall("./channel/item")}
    assert items["Permalink"].find("guid").attrib["isPermaLink"] == "true"
    assert items["Permalink"].findtext("guid") == "https://example.test/a"
    assert items["Product"].find("guid").attrib["isPermaLink"] == "false"
    assert items["Product"].findtext("guid") == "9PNJXVCVWD4K"


def test_html_is_escaped_once_and_survives_xml_round_trip() -> None:
    html = '<p>Tom & Jerry — "Zürich"</p>\n<p><img src="https://example.test/a.jpg?w=1&amp;h=2"></p>'
    payload = render_feed(make_config(), [item(description=html)])

    assert b"&lt;p&gt;Tom &amp; Jerry" in payload
    assert b"Z\xc3\xbcrich" in payload
    assert b"<p>Tom" not in payload
    assert parse(payload).findtext("./channel/item/description") == html

    parsed = feedparser.parse(payload)
    assert "Tom & Jerry" in parsed.entries[0].description
    assert "Zürich" in parsed.entries[0].description


def test_categories_source_and_media() -> None:
    payload = render_feed(
        make_config(),
        [
            item(
                categories=("Action & adventure", "Family & kids"),
                source=Source(name="NZZ Wirtschaft", url="https://www.nzz.ch/wirtschaft.rss"),
                thumbnail=Media(url="https://img.example/p.jpg?w=1&h=2", width=200, height=300),
                media_content=(
                    Media(url="https://img.example/shot1.jpg", medium="image"),
                    Media(url="https://img.example/shot2.jpg", medium="image", width=640, height=360),
                ),
            )
        ],
    )
    element = parse(payload).find("./channel/item")
    assert element is not None
    assert [category.text for category in element.findall("category")] == [
        "Action & adventure",
        "Family & kids",
    ]
    source = element.find("source")
    assert source is not None
    assert source.text == "NZZ Wirtschaft"
    assert source.attrib["url"] == "https://www.nzz.ch/wirtschaft.rss"
    thumbnail = element.find(f"{{{MEDIA_NS}}}thumbnail")
    assert thumbnail is not None
    assert thumbnail.attrib == {"url": "https://img.example/p.jpg?w=1&h=2", "width": "200", "height": "300"}
    contents = element.findall(f"{{{MEDIA_NS}}}content")
    assert [content.attrib for content in contents] == [
        {"url": "https://img.example/shot1.jpg", "medium": "image"},
        {"url": "https://img.example/shot2.jpg", "medium": "image", "width": "640", "height": "360"},
    ]
    assert b"<enclosure" not in payload
    assert b"content:encoded" not in payload


def test_aware_datetimes_are_normalised_to_utc_gmt() -> None:
    offset = timezone(timedelta(hours=2))
    payload = render_feed(make_config(), [item(published=datetime(2025, 6, 3, 12, 0, tzinfo=offset))])
    text = parse(payload).findtext("./channel/item/pubDate")
    assert text == "Tue, 03 Jun 2025 10:00:00 GMT"
    parsed = feedparser.parse(payload)
    assert parsed.entries[0].published_parsed[:6] == (2025, 6, 3, 10, 0, 0)


def test_ordering_undated_last_and_tie_breaks() -> None:
    payload = render_feed(
        make_config(),
        [
            item(title="Undated", guid="u", link="https://example.test/u", published=None),
            item(title="Zebra", guid="z", link="https://example.test/z", published=datetime(2025, 6, 1, 12, tzinfo=timezone.utc)),
            item(title="alpha", guid="b", link="https://example.test/b", published=datetime(2025, 6, 1, 12, tzinfo=timezone.utc)),
            item(title="Alpha", guid="a", link="https://example.test/a", published=datetime(2025, 6, 1, 12, tzinfo=timezone.utc)),
            item(title="Beta", guid="beta", link="https://example.test/beta", published=datetime(2025, 6, 2, 12, tzinfo=timezone.utc)),
        ],
    )
    titles = [element.findtext("title") for element in parse(payload).findall("./channel/item")]
    assert titles == ["Beta", "Alpha", "alpha", "Zebra", "Undated"]
    undated = parse(payload).findall("./channel/item")[-1]
    assert undated.find("pubDate") is None


def test_max_items_truncates_after_sorting() -> None:
    items = [
        item(title=f"Story {index}", guid=f"g{index}", link=f"https://example.test/{index}", published=datetime(2025, 6, 1, index, tzinfo=timezone.utc))
        for index in range(5)
    ]
    payload = render_feed(make_config(max_items=3), items)
    titles = [element.findtext("title") for element in parse(payload).findall("./channel/item")]
    assert titles == ["Story 4", "Story 3", "Story 2"]


def test_rendering_is_byte_identical_and_order_independent() -> None:
    first = item(title="A", guid="a", link="https://example.test/a")
    second = item(title="B", guid="b", link="https://example.test/b", published=None)
    config = make_config()

    forward = render_feed(config, [first, second])
    backward = render_feed(config, [second, first])

    assert forward == backward
    assert forward == render_feed(config, [first, second])


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"published": datetime(2025, 6, 1)}, "timezone-aware"),
        ({"title": "  "}, "title"),
        ({"link": "/relative"}, "link"),
        ({"link": "mailto:someone@example.test"}, "link"),
        ({"guid": ""}, "guid"),
        ({"thumbnail": Media(url="/relative.jpg")}, "media url"),
        ({"source": Source(name="", url="https://example.test/rss")}, "source name"),
    ],
)
def test_rejects_invalid_items_before_serializing(overrides: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        render_feed(make_config(), [item(**overrides)])


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"link": "/feed"}, "channel link"),
        ({"self_url": "feed.xml"}, "self URL"),
    ],
)
def test_rejects_invalid_channel_urls(overrides: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        render_feed(make_config(**overrides), [item()])


def test_feedparser_and_elementtree_agree_on_generated_feed() -> None:
    payload = render_feed(
        make_config(),
        [
            item(title="One", guid="https://example.test/one", link="https://example.test/one"),
            item(
                title="Two",
                guid="PRODUCT-ID",
                link="https://example.test/two",
                guid_is_permalink=False,
                published=None,
            ),
        ],
    )

    tree = parse(payload)
    parsed = feedparser.parse(payload)

    assert parsed.version == "rss20"
    assert [entry.id for entry in parsed.entries] == ["https://example.test/one", "PRODUCT-ID"]
    assert [entry.link for entry in parsed.entries] == [entry.findtext("link") for entry in tree.findall("./channel/item")]
    assert [entry.title for entry in parsed.entries] == ["One", "Two"]
