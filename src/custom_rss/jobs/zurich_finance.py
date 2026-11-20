"""Filter Swiss newspaper RSS feeds down to Zurich financial-services coverage."""

from __future__ import annotations

import calendar
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote_plus, urlsplit, urlunsplit

from ..models import FeedConfig, FeedItem, JobContext, Media, Source
from ..sources import fetch_feeds

log = logging.getLogger("custom_rss.jobs.zurich_finance")

_JOB_KEYS = {"sources", "patterns", "max_workers"}
_SOURCE_KEYS = {"name", "url"}


@dataclass(frozen=True)
class ZurichSettings:
    sources: tuple[Source, ...]
    patterns: tuple[str, ...]
    max_workers: int


def _is_web_link(link: str) -> bool:
    try:
        parts = urlsplit(link)
        parts.port  # noqa: B018 - raises ValueError for malformed ports
    except ValueError:
        return False
    return parts.scheme.lower() in ("http", "https") and bool(parts.hostname)


def load_settings(config: FeedConfig) -> ZurichSettings:
    where = config.directory / "feed.toml"
    raw: dict[str, Any] = dict(config.settings)

    unknown = sorted(set(raw) - _JOB_KEYS)
    if unknown:
        raise ValueError(f"{where}: unknown [job] setting(s): {', '.join(unknown)}")

    raw_sources = raw.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise ValueError(f"{where}: setting 'sources' must be a non-empty array of tables")
    sources: list[Source] = []
    names: set[str] = set()
    for index, entry in enumerate(raw_sources):
        if not isinstance(entry, dict):
            raise ValueError(f"{where}: setting 'sources[{index}]' must be a table")
        unknown_source = sorted(set(entry) - _SOURCE_KEYS)
        if unknown_source:
            raise ValueError(
                f"{where}: unknown key(s) in 'sources[{index}]': {', '.join(unknown_source)}"
            )
        name = entry.get("name")
        url = entry.get("url")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"{where}: setting 'sources[{index}].name' must be a non-empty string")
        name = name.strip()
        if name in names:
            raise ValueError(f"{where}: duplicate source name {name!r}")
        names.add(name)
        if not isinstance(url, str) or not _is_web_link(url.strip()):
            raise ValueError(f"{where}: setting 'sources[{index}].url' must be an absolute HTTP(S) URL")
        sources.append(Source(name=name, url=url.strip()))

    raw_patterns = raw.get("patterns")
    if not isinstance(raw_patterns, list) or not raw_patterns:
        raise ValueError(f"{where}: setting 'patterns' must be a non-empty array of strings")
    patterns: list[str] = []
    for index, pattern in enumerate(raw_patterns):
        if not isinstance(pattern, str) or not pattern:
            raise ValueError(f"{where}: setting 'patterns[{index}]' must be a non-empty string")
        try:
            re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(
                f"{where}: setting 'patterns[{index}]' is not a valid regular expression "
                f"({pattern!r}): {exc}"
            ) from exc
        patterns.append(pattern)

    max_workers = raw.get("max_workers", 5)
    if isinstance(max_workers, bool) or not isinstance(max_workers, int) or not 1 <= max_workers <= 16:
        raise ValueError(f"{where}: setting 'max_workers' must be an integer between 1 and 16")

    return ZurichSettings(
        sources=tuple(sources),
        patterns=tuple(patterns),
        max_workers=max_workers,
    )


def canonicalize_link(link: str) -> str:
    """Lowercase scheme/host, drop fragment and tracking params, trim trailing slash."""

    parts = urlsplit(link)
    scheme = parts.scheme.lower()
    netloc = parts.hostname.lower() if parts.hostname else ""
    if parts.port is not None:
        netloc = f"{netloc}:{parts.port}"

    query = ""
    if parts.query:
        kept = [
            segment
            for segment in parts.query.split("&")
            if segment
            and not unquote_plus(segment.split("=", 1)[0]).lower().startswith("utm_")
        ]
        query = "&".join(kept)

    path = parts.path
    if path not in ("", "/") and path.endswith("/"):
        path = path[:-1]

    return urlunsplit((scheme, netloc, path, query, ""))


def _entry_timestamp(entry: dict) -> datetime | None:
    for field in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = entry.get(field)
        if parsed:
            try:
                return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)
            except (OverflowError, OSError, ValueError, TypeError):
                continue
    return None


def _dimension(value: object) -> int | None:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _media_url(media: dict, *keys: str) -> str | None:
    for key in keys:
        value = media.get(key)
        if isinstance(value, str) and _is_web_link(value.strip()):
            return value.strip()
    return None


def _entry_thumbnail(entry: dict) -> Media | None:
    """First usable publisher image: media:thumbnail, image media:content, image enclosure."""

    for media in entry.get("media_thumbnail") or ():
        if isinstance(media, dict) and (url := _media_url(media, "url")):
            return Media(
                url=url,
                width=_dimension(media.get("width")),
                height=_dimension(media.get("height")),
            )

    for media in entry.get("media_content") or ():
        if not isinstance(media, dict):
            continue
        medium = str(media.get("medium") or "").lower()
        mime = str(media.get("type") or "").lower()
        if medium == "image" or mime.startswith("image/"):
            if url := _media_url(media, "url"):
                return Media(url=url)

    for media in entry.get("enclosures") or ():
        if not isinstance(media, dict):
            continue
        if str(media.get("type") or "").lower().startswith("image/"):
            if url := _media_url(media, "href", "url"):
                return Media(url=url)

    return None


def build(context: JobContext) -> tuple[FeedItem, ...]:
    settings = load_settings(context.config)
    patterns = tuple(re.compile(pattern, re.IGNORECASE) for pattern in settings.patterns)
    feeds = fetch_feeds(settings.sources, context.http, settings.max_workers)

    items: list[FeedItem] = []
    seen_links: set[str] = set()

    for source, parsed in zip(settings.sources, feeds):
        entries = parsed.get("entries", [])
        matched = 0
        for entry in entries:
            title = str(entry.get("title") or "").strip()
            link = str(entry.get("link") or "").strip()
            summary = str(entry.get("summary") or "").strip()

            if not title or not link or not _is_web_link(link):
                log.warning(
                    "skipping entry without title or HTTP(S) link in %r", source.name
                )
                continue

            haystack = f"{title}\n{summary}"
            if not any(pattern.search(haystack) for pattern in patterns):
                continue

            matched += 1
            canonical = canonicalize_link(link)
            if canonical in seen_links:
                continue
            seen_links.add(canonical)

            items.append(
                FeedItem(
                    title=title,
                    link=canonical,
                    guid=canonical,
                    guid_is_permalink=True,
                    description=summary,
                    published=_entry_timestamp(entry),
                    source=Source(name=source.name, url=source.url),
                    thumbnail=_entry_thumbnail(entry),
                )
            )

        log.info("%s: %d entries, %d matched", source.name, len(entries), matched)

    return tuple(items)
