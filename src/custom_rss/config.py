"""Load and validate ``feeds/<slug>/feed.toml`` before any I/O."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .models import FeedConfig
from .schedule import parse_schedule


def _require_str(raw: dict, key: str, where: Path) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where}: setting {key!r} must be a non-empty string")
    return value.strip()


def _require_url(raw: dict, key: str, where: Path) -> str:
    value = _require_str(raw, key, where)
    try:
        parts = urlsplit(value)
        parts.port  # noqa: B018 - raises ValueError for malformed ports
    except ValueError as exc:
        raise ValueError(f"{where}: setting {key!r} must be an absolute HTTP(S) URL: {exc}") from exc
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise ValueError(f"{where}: setting {key!r} must be an absolute HTTP(S) URL")
    return value


def _require_positive_int(raw: dict, key: str, where: Path) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{where}: setting {key!r} must be a positive integer")
    return value


def load_config(slug: str, root: Path) -> FeedConfig:
    """Read and validate ``root/feeds/<slug>/feed.toml``."""

    directory = root / "feeds" / slug
    path = directory / "feed.toml"
    with path.open("rb") as handle:
        raw: dict[str, Any] = tomllib.load(handle)

    title = _require_str(raw, "title", path)
    description = _require_str(raw, "description", path)
    link = _require_url(raw, "link", path)
    self_url = _require_url(raw, "self_url", path)
    language = _require_str(raw, "language", path)
    ttl = _require_positive_int(raw, "ttl", path)
    max_items = _require_positive_int(raw, "max_items", path)

    settings = raw.get("job")
    if not isinstance(settings, dict):
        raise ValueError(f"{path}: a [job] table is required")
    schedule = parse_schedule(raw.get("schedule"), path)

    return FeedConfig(
        slug=slug,
        title=title,
        description=description,
        link=link,
        self_url=self_url,
        language=language,
        ttl=ttl,
        max_items=max_items,
        directory=directory,
        settings=settings,
        schedule=schedule,
    )
