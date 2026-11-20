"""The stable boundary between shared feed machinery and per-feed jobs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from .http import HttpClient


@dataclass(frozen=True)
class Source:
    name: str
    url: str


@dataclass(frozen=True)
class Media:
    url: str
    medium: str | None = None
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True)
class FeedItem:
    title: str
    link: str
    guid: str
    description: str
    published: datetime | None = None
    guid_is_permalink: bool = False
    categories: tuple[str, ...] = ()
    source: Source | None = None
    thumbnail: Media | None = None
    media_content: tuple[Media, ...] = ()


@dataclass(frozen=True)
class FeedConfig:
    slug: str
    title: str
    description: str
    link: str
    self_url: str
    language: str
    ttl: int
    max_items: int
    directory: Path
    settings: Mapping[str, Any]

    @property
    def output_path(self) -> Path:
        return self.directory / "feed.xml"

    @property
    def state_dir(self) -> Path:
        return self.directory / "state"


@dataclass(frozen=True)
class JobContext:
    config: FeedConfig
    http: HttpClient
    now: datetime
    force: bool = False
