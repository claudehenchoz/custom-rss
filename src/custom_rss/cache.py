"""Opt-in item cache: keep published items after their source drops them.

A feed enables it with a ``[cache]`` table in ``feed.toml``::

    [cache]
    max_items = 100

After a successful job run, the fresh items are merged with
``feeds/<slug>/cache.jsonl`` by guid (fresh wins, so edits are picked up),
ordered like the feed (newest first, undated last), and trimmed to
``max_items``. The file holds no wall-clock values, so an unchanged run
rewrites identical bytes and produces no commit.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .files import write_text_atomic
from .models import FeedItem, Media, Source
from .rss import sort_items

CACHE_FILE = "cache.jsonl"


def _media_to_dict(media: Media) -> dict[str, Any]:
    return {"url": media.url, "medium": media.medium, "width": media.width, "height": media.height}


def _media_from_dict(raw: dict[str, Any]) -> Media:
    return Media(url=raw["url"], medium=raw.get("medium"), width=raw.get("width"), height=raw.get("height"))


def item_to_dict(item: FeedItem) -> dict[str, Any]:
    return {
        "guid": item.guid,
        "title": item.title,
        "link": item.link,
        "description": item.description,
        "published": item.published.isoformat() if item.published else None,
        "guid_is_permalink": item.guid_is_permalink,
        "categories": list(item.categories),
        "source": {"name": item.source.name, "url": item.source.url} if item.source else None,
        "thumbnail": _media_to_dict(item.thumbnail) if item.thumbnail else None,
        "media_content": [_media_to_dict(media) for media in item.media_content],
    }


def item_from_dict(raw: dict[str, Any]) -> FeedItem:
    published = raw.get("published")
    source = raw.get("source")
    thumbnail = raw.get("thumbnail")
    return FeedItem(
        title=raw["title"],
        link=raw["link"],
        guid=raw["guid"],
        description=raw.get("description", ""),
        published=datetime.fromisoformat(published) if published else None,
        guid_is_permalink=bool(raw.get("guid_is_permalink", False)),
        categories=tuple(raw.get("categories") or ()),
        source=Source(name=source["name"], url=source["url"]) if source else None,
        thumbnail=_media_from_dict(thumbnail) if thumbnail else None,
        media_content=tuple(_media_from_dict(media) for media in raw.get("media_content") or ()),
    )


def load_cache(directory: Path) -> list[FeedItem]:
    path = directory / CACHE_FILE
    if not path.exists():
        return []
    items: list[FeedItem] = []
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                item = item_from_dict(json.loads(line))
                sort_items([item])          # Same validation the renderer applies.
            except (ValueError, KeyError, TypeError, AttributeError) as error:
                raise ValueError(f"{path}:{number}: invalid cached item: {error}") from None
            items.append(item)
    return items


def merge(cached: Iterable[FeedItem], fresh: Iterable[FeedItem], max_items: int) -> tuple[FeedItem, ...]:
    """Fresh items replace cached ones with the same guid; keep the newest ``max_items``."""
    by_guid = {item.guid: item for item in cached}
    by_guid.update((item.guid, item) for item in fresh)
    return tuple(sort_items(by_guid.values())[:max_items])


def save_cache(directory: Path, items: Iterable[FeedItem]) -> None:
    text = "".join(
        json.dumps(item_to_dict(item), ensure_ascii=False, separators=(",", ":")) + "\n" for item in items
    )
    write_text_atomic(directory / CACHE_FILE, text)
