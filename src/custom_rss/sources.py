"""Concurrent RSS/Atom retrieval for aggregating jobs."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor

import feedparser
from feedparser.util import FeedParserDict

from .http import HttpClient
from .models import Source

log = logging.getLogger("custom_rss.sources")

ACCEPT = (
    "application/rss+xml, application/atom+xml, application/xml;q=0.9, "
    "text/xml;q=0.8, */*;q=0.1"
)


def fetch_feed(source: Source, http: HttpClient) -> FeedParserDict:
    payload = http.get_bytes(source.url, accept=ACCEPT)
    parsed = feedparser.parse(payload)
    if not parsed.get("version"):
        raise ValueError(f"{source.name!r} ({source.url}) is not a recognised RSS or Atom feed")
    return parsed


def fetch_feeds(
    sources: Sequence[Source], http: HttpClient, max_workers: int = 5
) -> tuple[FeedParserDict, ...]:
    """Fetch every source concurrently, preserving configured order.

    Any failure aborts the whole aggregation: a partially fetched set must never
    be written over a known-good feed.
    """

    if max_workers < 1:
        raise ValueError("max_workers must be at least 1")
    sources = tuple(sources)
    if not sources:
        return ()

    with ThreadPoolExecutor(max_workers=min(max_workers, len(sources))) as pool:
        futures = [pool.submit(fetch_feed, source, http) for source in sources]
        results: list[FeedParserDict] = []
        try:
            for future, source in zip(futures, sources):
                try:
                    results.append(future.result())
                except Exception as exc:  # noqa: BLE001 - re-raised with source context
                    for pending in futures:
                        pending.cancel()
                    raise RuntimeError(f"Source {source.name!r} ({source.url}) failed: {exc}") from exc
        finally:
            for future in futures:
                future.cancel()

    return tuple(results)
