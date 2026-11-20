"""Explicit job registry: the only job-discovery mechanism."""

from __future__ import annotations

from collections.abc import Callable

from ..models import FeedItem, JobContext
from . import gamepass, zurich_finance

Job = Callable[[JobContext], tuple[FeedItem, ...]]

JOBS: dict[str, Job] = {
    "zurich-finance": zurich_finance.build,
    "gamepass": gamepass.build,
}
