from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from xml.sax.saxutils import escape

import pytest

from custom_rss.config import load_config
from custom_rss.jobs import zurich_finance
from custom_rss.models import JobContext
from custom_rss.rss import MEDIA_NS, render_feed

UBS_PATTERN = r"\bUBS\b"
NOW = datetime(2025, 6, 4, 12, 0, tzinfo=timezone.utc)


def write_feed_config(
    tmp_path: Path,
    *,
    sources: list[tuple[str, str]],
    patterns: list[str],
    max_items: int = 100,
    max_workers: int = 5,
    link: str = "https://example.test/feed",
    extra_job: str = "",
) -> Path:
    lines = [
        f"title = {json.dumps('Test Feed')}",
        f"description = {json.dumps('Test description.')}",
        f"link = {json.dumps(link)}",
        f"self_url = {json.dumps('https://example.test/feeds/zurich-finance/feed.xml')}",
        f"language = {json.dumps('de-ch')}",
        "ttl = 60",
        f"max_items = {max_items}",
        "",
        "[schedule]",
        'every = "hour"',
        "",
        "[job]",
        f"max_workers = {str(max_workers).lower() if isinstance(max_workers, bool) else max_workers}",
        "patterns = [",
    ]
    lines += [f"  {json.dumps(pattern)}," for pattern in patterns]
    lines.append("]")
    if extra_job:
        lines.append(extra_job)
    for name, url in sources:
        lines += ["", "[[job.sources]]", f"name = {json.dumps(name)}", f"url = {json.dumps(url)}"]
    path = tmp_path / "feeds" / "zurich-finance" / "feed.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def rss(items: list[dict[str, str]]) -> bytes:
    parts = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<rss version="2.0"><channel>',
        "<title>Publisher</title>",
        "<link>https://publisher.example/</link>",
        "<description>Publisher feed</description>",
    ]
    for item in items:
        parts.append("<item>")
        parts.append(f"<title>{escape(item['title'])}</title>")
        parts.append(f"<link>{escape(item['link'])}</link>")
        if item.get("description") is not None:
            parts.append(f"<description>{escape(item['description'])}</description>")
        if item.get("pubDate"):
            parts.append(f"<pubDate>{item['pubDate']}</pubDate>")
        if item.get("extra"):
            parts.append(item["extra"])
        parts.append("</item>")
    parts.append("</channel></rss>")
    return "".join(parts).encode("utf-8")


class FakeHttp:
    def __init__(self, mapping: dict[str, bytes | Exception]) -> None:
        self.mapping = mapping
        self.calls: list[str] = []

    def get_bytes(self, url: str, *, accept: str) -> bytes:
        self.calls.append(url)
        value = self.mapping[url]
        if isinstance(value, Exception):
            raise value
        return value


def make_context(
    tmp_path: Path,
    http: FakeHttp,
    *,
    force: bool = False,
) -> JobContext:
    return JobContext(
        config=load_config("zurich-finance", tmp_path),
        http=http,  # type: ignore[arg-type]
        now=NOW,
        force=force,
    )


def test_end_to_end_generation_and_determinism(tmp_path: Path) -> None:
    feed_one = "https://publisher-one.example/rss"
    feed_two = "https://publisher-two.example/rss"
    http = FakeHttp(
        {
            feed_one: rss(
                [
                    {
                        "title": "UBS raises outlook",
                        "link": "https://News.Example.com/story/1/?utm_source=feed&utm_medium=rss&id=42#top",
                        "description": "The bank lifted guidance.",
                        "pubDate": "Tue, 03 Jun 2025 10:00:00 GMT",
                        "extra": (
                            '<media:thumbnail xmlns:media="http://search.yahoo.com/mrss/"'
                            ' width="200" height="200"'
                            ' url="https://img-msp-prod.nzz.ch/story/1.jpg?width=200&amp;height=200'
                            '&amp;auto=webp&amp;quality=80&amp;fit=crop"/>'
                        ),
                    },
                    {
                        "title": "UBS cuts jobs",
                        "link": "https://news.example.com/story/1?id=42&utm_source=other",
                        "description": "Duplicate of the same story via another channel.",
                        "pubDate": "Tue, 03 Jun 2025 09:00:00 GMT",
                        "extra": (
                            '<media:thumbnail xmlns:media="http://search.yahoo.com/mrss/"'
                            ' width="200" height="200"'
                            ' url="https://img-msp-prod.nzz.ch/duplicate/dropped.jpg?width=200"/>'
                        ),
                    },
                    {
                        "title": "Local weather",
                        "link": "https://news.example.com/weather",
                        "description": "Sunny all week.",
                    },
                ]
            ),
            feed_two: rss(
                [
                    {
                        "title": "Julius Bär names new chief executive",
                        "link": "https://publisher-two.example/bank/ceo",
                        "description": "Zurich private bank announcement.",
                        "pubDate": "Mon, 02 Jun 2025 08:30:00 GMT",
                    }
                ]
            ),
        }
    )
    write_feed_config(
        tmp_path,
        sources=[("News One", feed_one), ("News Two", feed_two)],
        patterns=[UBS_PATTERN, r"\bJulius[ -]B(?:ä|ae)r\b"],
    )
    context = make_context(tmp_path, http)

    items = zurich_finance.build(context)
    payload = render_feed(context.config, items)
    root = ET.fromstring(payload)

    assert http.calls == [feed_one, feed_two]
    channel = root.find("channel")
    assert channel is not None
    assert channel.findtext("link") == "https://example.test/feed"

    xml_items = channel.findall("item")
    assert len(xml_items) == 2
    titles = [element.findtext("title") for element in xml_items]
    assert sum("UBS" in title for title in titles) == 1
    assert "Local weather" not in titles

    ubs = next(element for element in xml_items if "UBS" in (element.findtext("title") or ""))
    assert ubs.findtext("link") == "https://news.example.com/story/1?id=42"
    assert ubs.findtext("guid") == ubs.findtext("link")
    assert ubs.find("guid").attrib["isPermaLink"] == "true"
    assert ubs.findtext("description") == "The bank lifted guidance."
    assert ubs.findtext("source") == "News One"
    assert ubs.find("source").attrib["url"] == feed_one
    assert parsedate_to_datetime(ubs.findtext("pubDate")) == datetime(
        2025, 6, 3, 10, 0, tzinfo=timezone.utc
    )

    for element in xml_items:
        link = element.findtext("link", "")
        assert "utm_" not in link
        assert "#" not in link

    other = next(element for element in xml_items if element is not ubs)
    assert other.findtext("source") == "News Two"
    assert other.find("source").attrib["url"] == feed_two

    thumbnail = ubs.find(f"{{{MEDIA_NS}}}thumbnail")
    assert thumbnail is not None
    assert thumbnail.attrib["url"] == (
        "https://img-msp-prod.nzz.ch/story/1.jpg?width=200&height=200"
        "&auto=webp&quality=80&fit=crop"
    )
    assert thumbnail.attrib["width"] == "200"
    assert thumbnail.attrib["height"] == "200"
    assert other.find(f"{{{MEDIA_NS}}}thumbnail") is None
    assert [len(element.findall(f"{{{MEDIA_NS}}}thumbnail")) for element in xml_items] == [1, 0]

    assert b"dropped.jpg" not in payload
    assert b'xmlns:media="http://search.yahoo.com/mrss/"' in payload
    assert b"&amp;auto=webp" in payload
    assert channel.find("lastBuildDate") is None
    assert b"lastBuildDate" not in payload

    assert render_feed(context.config, items) == payload


def test_or_semantics_over_title_and_summary(tmp_path: Path) -> None:
    feed = "https://publisher.example/rss"
    http = FakeHttp(
        {
            feed: rss(
                [
                    {
                        "title": "UBS outlook improves",
                        "link": "https://publisher.example/title-match",
                        "description": "Unrelated body text.",
                    },
                    {
                        "title": "Markets",
                        "link": "https://publisher.example/summary-match",
                        "description": "Analysts say ubs shares are cheap.",
                    },
                    {
                        "title": "Weather",
                        "link": "https://publisher.example/no-match",
                        "description": "Sunny all week.",
                    },
                ]
            )
        }
    )
    write_feed_config(tmp_path, sources=[("Publisher", feed)], patterns=[UBS_PATTERN])

    items = zurich_finance.build(make_context(tmp_path, http))

    assert sorted(item.title for item in items) == ["Markets", "UBS outlook improves"]


def test_thumbnail_source_shapes_and_precedence(tmp_path: Path) -> None:
    feed = "https://publisher.example/rss"
    content_image = (
        '<media:content xmlns:media="http://search.yahoo.com/mrss/"'
        ' type="image/jpeg" medium="image" url="https://cdn.unitycms.io/images/content.jpg"/>'
    )
    image_enclosure = (
        '<enclosure type="image/jpeg" url="https://partner-feeds.20min.ch/image.jpg"'
        ' length="12345"/>'
    )
    audio_enclosure = (
        '<enclosure type="audio/mpeg" url="https://cdn.example/audio.mp3" length="999"/>'
    )
    thumbnail = (
        '<media:thumbnail xmlns:media="http://search.yahoo.com/mrss/"'
        ' width="300" height="200" url="https://img.example/winner.jpg"/>'
    )

    def item(title: str, slug: str, extra: str = "") -> dict[str, str]:
        return {
            "title": title,
            "link": f"https://publisher.example/{slug}",
            "description": "match",
            "extra": extra,
        }

    http = FakeHttp(
        {
            feed: rss(
                [
                    item("Content match", "content", content_image),
                    item("Enclosure match", "enclosure", image_enclosure),
                    item("Audio match", "audio", audio_enclosure),
                    item("Plain match", "plain"),
                    item("All match", "all", content_image + image_enclosure + thumbnail),
                ]
            )
        }
    )
    write_feed_config(tmp_path, sources=[("Publisher", feed)], patterns=[r"\bmatch\b"])
    context = make_context(tmp_path, http)

    items = zurich_finance.build(context)
    payload = render_feed(context.config, items)
    by_title = {item.title: item for item in items}

    assert by_title["Content match"].thumbnail.url == "https://cdn.unitycms.io/images/content.jpg"
    assert by_title["Content match"].thumbnail.width is None
    assert by_title["Enclosure match"].thumbnail.url == "https://partner-feeds.20min.ch/image.jpg"
    assert by_title["All match"].thumbnail.url == "https://img.example/winner.jpg"
    assert by_title["All match"].thumbnail.width == 300
    assert by_title["All match"].thumbnail.height == 200
    assert by_title["Audio match"].thumbnail is None
    assert by_title["Plain match"].thumbnail is None
    assert all(item.media_content == () for item in items)
    assert b"cdn.example/audio.mp3" not in payload
    assert b"<enclosure" not in payload


def test_ordering_undated_last_and_cap(tmp_path: Path) -> None:
    feed = "https://publisher.example/rss"

    def dated(title: str, link: str, when: str | None) -> dict[str, str]:
        return {"title": title, "link": link, "description": "match", "pubDate": when}

    http = FakeHttp(
        {
            feed: rss(
                [
                    dated("Zebra", "https://publisher.example/zebra", "Sun, 01 Jun 2025 12:00:00 GMT"),
                    dated("Alpha", "https://publisher.example/alpha", "Sun, 01 Jun 2025 12:00:00 GMT"),
                    dated("Beta", "https://publisher.example/beta", "Mon, 02 Jun 2025 12:00:00 GMT"),
                    dated(
                        "Dup",
                        "https://publisher.example/beta?utm_source=again",
                        "Mon, 02 Jun 2025 12:00:00 GMT",
                    ),
                    dated("Undated", "https://publisher.example/undated", None),
                    dated("Same", "https://publisher.example/same-b", "Sun, 01 Jun 2025 12:00:00 GMT"),
                    dated("Same", "https://publisher.example/same-a", "Sun, 01 Jun 2025 12:00:00 GMT"),
                ]
            )
        }
    )
    write_feed_config(
        tmp_path, sources=[("Publisher", feed)], patterns=[r"\bmatch\b"], max_items=100
    )
    context = make_context(tmp_path, http)

    items = zurich_finance.build(context)
    root = ET.fromstring(render_feed(context.config, items))
    xml_items = root.findall("./channel/item")
    assert [element.findtext("title") for element in xml_items] == [
        "Beta",
        "Alpha",
        "Same",
        "Same",
        "Zebra",
        "Undated",
    ]
    assert [element.findtext("link") for element in xml_items[2:4]] == [
        "https://publisher.example/same-a",
        "https://publisher.example/same-b",
    ]
    assert xml_items[-1].find("pubDate") is None
    assert all(element.find("pubDate") is not None for element in xml_items[:-1])

    write_feed_config(
        tmp_path, sources=[("Publisher", feed)], patterns=[r"\bmatch\b"], max_items=3
    )
    capped = ET.fromstring(render_feed(load_config("zurich-finance", tmp_path), items))
    assert [element.findtext("title") for element in capped.findall("./channel/item")] == [
        "Beta",
        "Alpha",
        "Same",
    ]


def test_timestamp_precedence() -> None:
    entry = {
        "created_parsed": (2020, 1, 1, 0, 0, 0, 0, 1, 0),
        "updated_parsed": (2021, 1, 1, 0, 0, 0, 0, 1, 0),
        "published_parsed": (2022, 1, 1, 0, 0, 0, 0, 1, 0),
    }
    assert zurich_finance._entry_timestamp(entry) == datetime(2022, 1, 1, tzinfo=timezone.utc)
    assert zurich_finance._entry_timestamp({"created_parsed": (2020, 1, 1, 0, 0, 0, 0, 1, 0)}) == datetime(
        2020, 1, 1, tzinfo=timezone.utc
    )
    assert zurich_finance._entry_timestamp({}) is None


def test_valid_empty_result(tmp_path: Path) -> None:
    feed = "https://publisher.example/rss"
    http = FakeHttp(
        {
            feed: rss(
                [
                    {
                        "title": "Weather",
                        "link": "https://publisher.example/weather",
                        "description": "Sunny.",
                    }
                ]
            )
        }
    )
    write_feed_config(tmp_path, sources=[("Publisher", feed)], patterns=[UBS_PATTERN])
    context = make_context(tmp_path, http)

    items = zurich_finance.build(context)
    payload = render_feed(context.config, items)

    assert http.calls == [feed]
    assert items == ()
    root = ET.fromstring(payload)
    assert root.findtext("./channel/title") == "Test Feed"
    assert root.findall("./channel/item") == []
    assert b"xmlns:media" not in payload
    assert b"thumbnail" not in payload


def test_source_failure_preserves_previous_output(tmp_path: Path) -> None:
    good = "https://publisher.example/good"
    bad = "https://publisher.example/bad"
    http = FakeHttp(
        {
            good: rss(
                [
                    {
                        "title": "UBS story",
                        "link": "https://publisher.example/story",
                        "description": "match",
                    }
                ]
            ),
            bad: OSError("connection refused"),
        }
    )
    write_feed_config(
        tmp_path,
        sources=[("Good Source", good), ("Failing Source", bad)],
        patterns=[UBS_PATTERN],
    )
    context = make_context(tmp_path, http)
    sentinel = b"previous feed bytes\n"
    context.config.output_path.write_bytes(sentinel)

    with pytest.raises(RuntimeError, match="Failing Source"):
        zurich_finance.build(context)

    assert context.config.output_path.read_bytes() == sentinel


def test_configuration_validation_happens_before_fetching(tmp_path: Path) -> None:
    source = [("Publisher", "https://publisher.example/rss")]

    def forbidden(*args: object, **kwargs: object) -> bytes:
        raise AssertionError("fetch must not happen for invalid configuration")

    class ForbiddenHttp:
        get_bytes = staticmethod(forbidden)

    cases = [
        (dict(sources=source, patterns=["(unclosed"]), "patterns"),
        (dict(sources=source, patterns=[]), "patterns"),
        (dict(sources=[], patterns=[UBS_PATTERN]), "sources"),
        (dict(sources=source, patterns=[UBS_PATTERN], max_workers=0), "max_workers"),
        (dict(sources=source, patterns=[UBS_PATTERN], max_workers=17), "max_workers"),
        (dict(sources=source, patterns=[UBS_PATTERN], max_workers=True), "max_workers"),
        (
            dict(
                sources=[("Same", "https://a.example/rss"), ("Same", "https://b.example/rss")],
                patterns=[UBS_PATTERN],
            ),
            "duplicate source name",
        ),
        (dict(sources=[("Bad", "ftp://example.com/rss")], patterns=[UBS_PATTERN]), "absolute HTTP"),
        (
            dict(
                sources=source,
                patterns=[UBS_PATTERN],
                extra_job='unknown_setting = "x"',
            ),
            "unknown",
        ),
        (
            dict(
                sources=source,
                patterns=[UBS_PATTERN],
                extra_job='mystery = "x"',
            ),
            "unknown",
        ),
    ]
    for kwargs, message in cases:
        write_feed_config(tmp_path, **kwargs)  # type: ignore[arg-type]
        context = JobContext(
            config=load_config("zurich-finance", tmp_path),
            http=ForbiddenHttp(),  # type: ignore[arg-type]
            now=NOW,
        )
        with pytest.raises(ValueError, match=message):
            zurich_finance.build(context)


def test_logs_each_source_in_configured_order(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    one = "https://one.example/rss"
    two = "https://two.example/rss"
    http = FakeHttp(
        {
            one: rss([{"title": "UBS one", "link": "https://one.example/a", "description": "match"}]),
            two: rss(
                [
                    {"title": "Weather", "link": "https://two.example/w", "description": "sunny"},
                    {"title": "UBS two", "link": "https://two.example/b", "description": "match"},
                ]
            ),
        }
    )
    write_feed_config(
        tmp_path, sources=[("News One", one), ("News Two", two)], patterns=[UBS_PATTERN]
    )

    with caplog.at_level("INFO", logger="custom_rss.jobs.zurich_finance"):
        zurich_finance.build(make_context(tmp_path, http))

    messages = [record.getMessage() for record in caplog.records]
    assert messages.index("News One: 1 entries, 1 matched") < messages.index(
        "News Two: 2 entries, 1 matched"
    )
