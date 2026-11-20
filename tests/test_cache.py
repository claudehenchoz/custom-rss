from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from custom_rss.cache import CACHE_FILE, item_from_dict, item_to_dict, load_cache, merge, save_cache
from custom_rss.models import FeedItem, Media, Source

NOW = datetime(2026, 11, 21, 3, 0, tzinfo=timezone.utc)


def item(guid: str, hours_ago: float | None = 0, title: str | None = None) -> FeedItem:
    return FeedItem(
        title=title or f"Story {guid}",
        link=f"https://example.test/{guid}",
        guid=f"https://example.test/{guid}",
        description=f"Summary {guid}.",
        published=None if hours_ago is None else NOW - timedelta(hours=hours_ago),
        guid_is_permalink=True,
    )


def guids(items: tuple[FeedItem, ...] | list[FeedItem]) -> list[str]:
    return [entry.guid.rsplit("/", 1)[1] for entry in items]


def test_round_trip_keeps_every_field() -> None:
    full = FeedItem(
        title="Full",
        link="https://example.test/full",
        guid="full-guid",
        description="<p>Body</p>",
        published=NOW,
        guid_is_permalink=False,
        categories=("A", "B"),
        source=Source(name="NZZ", url="https://www.nzz.ch/wirtschaft.rss"),
        thumbnail=Media(url="https://img.test/t.jpg", width=640, height=360),
        media_content=(Media(url="https://img.test/1.jpg", medium="image"),),
    )
    assert item_from_dict(item_to_dict(full)) == full
    assert item_from_dict(item_to_dict(item("undated", None))) == item("undated", None)


def test_merge_keeps_vanished_items_and_prefers_fresh_versions() -> None:
    cached = [item("a", 5), item("b", 4, title="Old title")]
    fresh = [item("b", 4, title="New title"), item("c", 1)]

    merged = merge(cached, fresh, 10)

    assert guids(merged) == ["c", "b", "a"]
    assert merged[1].title == "New title"


def test_merge_trims_oldest_and_puts_undated_last() -> None:
    merged = merge([item("old", 50), item("undated", None)], [item("new", 1), item("mid", 10)], 3)
    assert guids(merged) == ["new", "mid", "old"]

    merged = merge([item("undated", None)], [item("new", 1)], 5)
    assert guids(merged) == ["new", "undated"]


def test_save_and_load_are_stable(tmp_path: Path) -> None:
    items = merge([], [item("a", 2), item("b", 1)], 10)
    save_cache(tmp_path, items)
    first = (tmp_path / CACHE_FILE).read_bytes()

    assert tuple(load_cache(tmp_path)) == items
    save_cache(tmp_path, merge(load_cache(tmp_path), items, 10))
    assert (tmp_path / CACHE_FILE).read_bytes() == first
    assert len(first.splitlines()) == 2


def test_missing_cache_is_empty(tmp_path: Path) -> None:
    assert load_cache(tmp_path) == []


@pytest.mark.parametrize(
    "line",
    [
        "not json",
        '{"title": "No guid", "link": "https://example.test/x"}',
        '{"guid": "g", "title": "Bad link", "link": "ftp://example.test/x"}',
        '{"guid": "g", "title": "Naive", "link": "https://example.test/x", "published": "2026-11-21T03:00:00"}',
    ],
)
def test_corrupt_cache_names_file_and_line(tmp_path: Path, line: str) -> None:
    save_cache(tmp_path, [item("ok")])
    with (tmp_path / CACHE_FILE).open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")

    with pytest.raises(ValueError, match=r"cache\.jsonl:2"):
        load_cache(tmp_path)
