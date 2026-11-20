from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from custom_rss.schedule import (
    Cadence,
    Schedule,
    current_slot,
    is_due,
    next_slot,
    parse_schedule,
    read_last_success,
    record_success,
)

WHERE = Path("feed.toml")
HOURLY = Schedule(Cadence.HOUR)
DAILY = Schedule(Cadence.DAY, hour=3)
WEEKLY = Schedule(Cadence.WEEK, hour=3, weekday=0)  # Monday 03:00 UTC


def utc(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=timezone.utc)  # 2026-11-23 is a Monday


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"every": "hour"}, HOURLY),
        ({"every": "day", "at": "03:00"}, DAILY),
        ({"every": "week", "on": "mon", "at": "03:00"}, WEEKLY),
        ({"every": "week", "on": "sun", "at": "23:00"}, Schedule(Cadence.WEEK, 23, 6)),
    ],
)
def test_parses_valid_schedules(raw: dict, expected: Schedule) -> None:
    assert parse_schedule(raw, WHERE) == expected


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (None, r"\[schedule\] table is required"),
        ({"every": "month"}, "schedule.every"),
        ({"every": "hour", "at": "03:00"}, "schedule.at' does not apply"),
        ({"every": "day"}, "whole UTC hour"),
        ({"every": "day", "at": "03:30"}, "whole UTC hour"),
        ({"every": "day", "at": "24:00"}, "whole UTC hour"),
        ({"every": "day", "at": 3}, "whole UTC hour"),
        ({"every": "day", "at": "03:00", "on": "mon"}, 'only applies to every = "week"'),
        ({"every": "week", "at": "03:00"}, "schedule.on"),
        ({"every": "week", "at": "03:00", "on": "monday"}, "schedule.on"),
        ({"every": "hour", "cron": "* * * * *"}, r"unknown \[schedule\] setting"),
    ],
)
def test_rejects_invalid_schedules(raw: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_schedule(raw, WHERE)


def test_describe() -> None:
    assert HOURLY.describe() == "every hour"
    assert DAILY.describe() == "every day at 03:00 UTC"
    assert WEEKLY.describe() == "every week on mon at 03:00 UTC"


def test_slots() -> None:
    assert current_slot(HOURLY, utc(21, 6, 17)) == utc(21, 6)
    assert current_slot(DAILY, utc(21, 6, 17)) == utc(21, 3)
    assert current_slot(DAILY, utc(21, 2, 17)) == utc(20, 3)        # before `at`: yesterday's slot
    assert next_slot(DAILY, utc(21, 2, 17)) == utc(21, 3)
    assert current_slot(WEEKLY, utc(23, 3, 17)) == utc(23, 3)       # Monday after 03:00
    assert current_slot(WEEKLY, utc(23, 2, 17)) == utc(16, 3)       # Monday before 03:00
    assert current_slot(WEEKLY, utc(22, 23, 17)) == utc(16, 3)      # Sunday
    assert next_slot(WEEKLY, utc(22, 23, 17)) == utc(23, 3)
    zurich = timezone(timedelta(hours=1))                           # non-UTC input is normalised
    assert current_slot(DAILY, datetime(2026, 11, 21, 4, 30, tzinfo=zurich)) == utc(21, 3)


def test_due_rule() -> None:
    assert is_due(HOURLY, None, utc(21, 6, 17))
    assert is_due(DAILY, None, utc(21, 6, 17))                      # never ran: due now
    assert not is_due(DAILY, utc(21, 3, 17), utc(21, 4, 17))        # done in this slot
    assert is_due(DAILY, utc(20, 3, 17), utc(21, 6, 17))            # late tick still runs
    assert not is_due(DAILY, utc(20, 3, 17), utc(21, 2, 17))        # today's slot not open yet
    assert is_due(DAILY, utc(18, 3, 17), utc(21, 4, 17))            # dropped days: caught up once
    assert not is_due(DAILY, utc(21, 4, 17), utc(21, 5, 17))
    assert not is_due(WEEKLY, utc(23, 3, 17), utc(29, 23, 17))
    assert is_due(WEEKLY, utc(23, 3, 17), utc(30, 3, 17))


def test_record_round_trip_and_hourly_keeps_no_record(tmp_path: Path) -> None:
    assert read_last_success(tmp_path) is None
    record_success(tmp_path, HOURLY, utc(21, 3, 17))
    assert not (tmp_path / "last_run.json").exists()

    record_success(tmp_path, DAILY, utc(21, 3, 17))
    assert (tmp_path / "last_run.json").read_text() == '{\n  "last_success": "2026-11-21T03:17:00+00:00"\n}\n'
    assert read_last_success(tmp_path) == utc(21, 3, 17)


def test_corrupt_record_names_the_file(tmp_path: Path) -> None:
    (tmp_path / "last_run.json").write_text('{"when": "yesterday"}')
    with pytest.raises(ValueError, match="last_run.json"):
        read_last_success(tmp_path)
