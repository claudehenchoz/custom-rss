"""Deterministic RSS 2.0 serialization shared by every feed."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Iterable
from datetime import datetime, timezone
from email.utils import format_datetime
from urllib.parse import urlsplit

from .models import FeedConfig, FeedItem, Media, Source

ATOM_NS = "http://www.w3.org/2005/Atom"
MEDIA_NS = "http://search.yahoo.com/mrss/"
GENERATOR = "custom-rss/1.0"

ET.register_namespace("atom", ATOM_NS)
ET.register_namespace("media", MEDIA_NS)


def _is_http_url(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parts = urlsplit(value.strip())
        parts.port  # noqa: B018 - raises ValueError for malformed ports
    except ValueError:
        return False
    return parts.scheme.lower() in ("http", "https") and bool(parts.hostname)


def _normalise_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError("item published datetime must be timezone-aware")
    return value.astimezone(timezone.utc)


def _validate_item(item: FeedItem) -> datetime | None:
    if not isinstance(item.title, str) or not item.title.strip():
        raise ValueError("item title must be a non-empty string")
    if not _is_http_url(item.link):
        raise ValueError(f"item link must be an absolute HTTP(S) URL: {item.link!r}")
    if not isinstance(item.guid, str) or not item.guid.strip():
        raise ValueError(f"item {item.title!r} has an empty guid")
    if item.published is None:
        return None
    return _normalise_datetime(item.published)


def _media_attributes(media: Media, *, with_medium: bool) -> dict[str, str]:
    if not _is_http_url(media.url):
        raise ValueError(f"media url must be an absolute HTTP(S) URL: {media.url!r}")
    attributes = {"url": media.url.strip()}
    if with_medium and media.medium:
        attributes["medium"] = media.medium
    if media.width is not None:
        attributes["width"] = str(media.width)
    if media.height is not None:
        attributes["height"] = str(media.height)
    return attributes


def _source_element(item: ET.Element, source: Source) -> None:
    if not source.name.strip():
        raise ValueError("source name must be a non-empty string")
    if not _is_http_url(source.url):
        raise ValueError(f"source url must be an absolute HTTP(S) URL: {source.url!r}")
    element = ET.SubElement(item, "source", {"url": source.url})
    element.text = source.name


def _prepare(items: Iterable[FeedItem]) -> list[tuple[datetime | None, FeedItem]]:
    prepared = [(_validate_item(item), item) for item in items]
    prepared.sort(
        key=lambda entry: (
            entry[0] is None,
            -entry[0].timestamp() if entry[0] is not None else 0.0,
            entry[1].title.casefold(),
            entry[1].guid,
        )
    )
    return prepared


def sort_items(items: Iterable[FeedItem]) -> list[FeedItem]:
    """Validate and order items exactly as the feed shows them: newest first, undated last."""
    return [item for _, item in _prepare(items)]


def render_feed(config: FeedConfig, items: Iterable[FeedItem]) -> bytes:
    """Render ``items`` as UTF-8 RSS 2.0 for ``config``.

    Newest first, undated last, capped at ``config.max_items``. Identical inputs
    always produce identical bytes: no wall-clock values are included.
    """

    if not _is_http_url(config.link):
        raise ValueError(f"channel link must be an absolute HTTP(S) URL: {config.link!r}")
    if not _is_http_url(config.self_url):
        raise ValueError(f"channel self URL must be an absolute HTTP(S) URL: {config.self_url!r}")

    prepared = _prepare(items)[: config.max_items]

    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = config.title
    ET.SubElement(channel, "link").text = config.link
    ET.SubElement(channel, "description").text = config.description
    ET.SubElement(channel, "language").text = config.language
    ET.SubElement(channel, "ttl").text = str(config.ttl)
    ET.SubElement(channel, "generator").text = GENERATOR
    ET.SubElement(
        channel,
        f"{{{ATOM_NS}}}link",
        {"rel": "self", "type": "application/rss+xml", "href": config.self_url},
    )

    for published, item in prepared:
        element = ET.SubElement(channel, "item")
        ET.SubElement(element, "title").text = item.title
        ET.SubElement(element, "link").text = item.link
        ET.SubElement(element, "description").text = item.description
        if published is not None:
            ET.SubElement(element, "pubDate").text = format_datetime(published, usegmt=True)
        guid = ET.SubElement(element, "guid", {"isPermaLink": "true" if item.guid_is_permalink else "false"})
        guid.text = item.guid
        for category in item.categories:
            ET.SubElement(element, "category").text = category
        if item.source is not None:
            _source_element(element, item.source)
        if item.thumbnail is not None:
            ET.SubElement(
                element,
                f"{{{MEDIA_NS}}}thumbnail",
                _media_attributes(item.thumbnail, with_medium=False),
            )
        for media in item.media_content:
            ET.SubElement(
                element,
                f"{{{MEDIA_NS}}}content",
                _media_attributes(media, with_medium=True),
            )

    ET.indent(rss, space="  ")
    body = ET.tostring(rss, encoding="utf-8")
    return b'<?xml version="1.0" encoding="utf-8"?>\n' + body + b"\n"
