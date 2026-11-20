"""When a feed is due: its ``[schedule]`` table plus the last recorded success.

One workflow ticks hourly and asks every feed whether it is due. Each cadence
divides UTC time into slots (every hour; every day at ``at``; every week ``on``
a weekday ``at``). A feed is due when it has no recorded success at or after
the start of the current slot, so a late tick still runs it, a dropped tick is
caught up once on the next one, and a failure is retried on the next tick.

Hourly feeds are due on every tick and keep no record, so an unchanged hourly
feed never produces a commit. Daily and weekly feeds record the start time of
their last successful run in ``feeds/<slug>/last_run.json``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

from .files import write_text_atomic

LAST_RUN_FILE = "last_run.json"
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_KEYS = {"every", "at", "on"}
_AT = re.compile(r"^([01]\d|2[0-3]):00$")


class Cadence(StrEnum):
    HOUR = "hour"
    DAY = "day"
    WEEK = "week"


_PERIOD = {
    Cadence.HOUR: timedelta(hours=1),
    Cadence.DAY: timedelta(days=1),
    Cadence.WEEK: timedelta(weeks=1),
}


@dataclass(frozen=True)
class Schedule:
    every: Cadence
    hour: int = 0                 # UTC hour a day/week slot starts
    weekday: int | None = None    # 0 = Monday, weekly only

    def describe(self) -> str:
        if self.every is Cadence.HOUR:
            return "every hour"
        at = f"at {self.hour:02d}:00 UTC"
        if self.every is Cadence.DAY:
            return f"every day {at}"
        return f"every week on {WEEKDAYS[self.weekday or 0]} {at}"


def parse_schedule(raw: Any, where: Path) -> Schedule:
    """Validate a ``[schedule]`` table."""
    if not isinstance(raw, dict):
        raise ValueError(
            f"{where}: a [schedule] table is required, e.g. every = \"hour\" | \"day\" | \"week\""
        )
    unknown = sorted(set(raw) - _KEYS)
    if unknown:
        raise ValueError(f"{where}: unknown [schedule] setting(s): {', '.join(unknown)}")

    every = raw.get("every")
    if every not in tuple(Cadence):
        raise ValueError(f"{where}: setting 'schedule.every' must be one of {[c.value for c in Cadence]}")
    cadence = Cadence(every)

    if cadence is Cadence.HOUR:
        for key in ("at", "on"):
            if key in raw:
                raise ValueError(f"{where}: setting 'schedule.{key}' does not apply to every = \"hour\"")
        return Schedule(cadence)

    at = raw.get("at")
    match = _AT.match(at) if isinstance(at, str) else None
    if match is None:
        raise ValueError(
            f"{where}: setting 'schedule.at' must be a whole UTC hour like \"03:00\" "
            f"(the scheduler ticks hourly)"
        )
    hour = int(match.group(1))

    if cadence is Cadence.DAY:
        if "on" in raw:
            raise ValueError(f"{where}: setting 'schedule.on' only applies to every = \"week\"")
        return Schedule(cadence, hour)

    on = raw.get("on")
    if on not in WEEKDAYS:
        raise ValueError(f"{where}: setting 'schedule.on' must be one of {list(WEEKDAYS)}")
    return Schedule(cadence, hour, WEEKDAYS.index(on))


def current_slot(schedule: Schedule, now: datetime) -> datetime:
    """Start of the slot that contains ``now`` (UTC)."""
    now = now.astimezone(timezone.utc)
    if schedule.every is Cadence.HOUR:
        return now.replace(minute=0, second=0, microsecond=0)
    start = now.replace(hour=schedule.hour, minute=0, second=0, microsecond=0)
    if schedule.every is Cadence.WEEK:
        start -= timedelta(days=(now.weekday() - (schedule.weekday or 0)) % 7)
    while start > now:
        start -= _PERIOD[schedule.every]
    return start


def next_slot(schedule: Schedule, now: datetime) -> datetime:
    return current_slot(schedule, now) + _PERIOD[schedule.every]


def is_due(schedule: Schedule, last_success: datetime | None, now: datetime) -> bool:
    return last_success is None or last_success < current_slot(schedule, now)


def read_last_success(directory: Path) -> datetime | None:
    path = directory / LAST_RUN_FILE
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))["last_success"]
        return datetime.fromisoformat(value).astimezone(timezone.utc)
    except (ValueError, KeyError, TypeError) as error:
        raise ValueError(f"{path}: expected {{\"last_success\": \"<ISO datetime>\"}}: {error}") from None


def record_success(directory: Path, schedule: Schedule, started_at: datetime) -> None:
    """Remember a successful run; hourly feeds keep no record (always due)."""
    if schedule.every is Cadence.HOUR:
        return
    payload = {"last_success": started_at.astimezone(timezone.utc).isoformat()}
    write_text_atomic(directory / LAST_RUN_FILE, json.dumps(payload, indent=2) + "\n")
