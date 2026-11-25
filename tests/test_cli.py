from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import custom_rss.cli as cli
from custom_rss.models import Deferred, FeedItem

NOW = datetime(2026, 11, 21, 3, 0, tzinfo=timezone.utc)


def write_config(
    tmp_path: Path,
    slug: str,
    *,
    title: str = "Test Feed",
    link: str = "https://example.test/feed",
    every: str = "hour",
    at: str | None = None,
    cache: int | None = None,
) -> Path:
    lines = [
        f"title = {json.dumps(title)}",
        f"description = {json.dumps('Test description.')}",
        f"link = {json.dumps(link)}",
        f"self_url = {json.dumps(f'https://example.test/feeds/{slug}/feed.xml')}",
        f"language = {json.dumps('en-us')}",
        "ttl = 60",
        "max_items = 100",
        "",
        "[schedule]",
        f"every = {json.dumps(every)}",
        *([f"at = {json.dumps(at)}"] if at else []),
        "",
        *(["[cache]", f"max_items = {cache}", ""] if cache is not None else []),
        "[job]",
        'tier = "ultimate"',
    ]
    path = tmp_path / "feeds" / slug / "feed.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def make_item(title: str = "CLI Story", guid: str = "cli-story") -> FeedItem:
    return FeedItem(
        title=title,
        link=f"https://example.test/{guid}",
        guid=guid,
        description="Body.",
        published=NOW,
        guid_is_permalink=True,
    )


def test_list_prints_registered_slugs(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["list"]) == 0
    assert capsys.readouterr().out.split() == ["zurich-finance", "gamepass", "sbb-resale"]


def test_run_executes_only_the_selected_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_config(tmp_path, "zurich-finance")
    write_config(tmp_path, "gamepass")
    calls: list[str] = []

    def fake_zurich(context: object) -> tuple[FeedItem, ...]:
        calls.append("zurich-finance")
        return (make_item(),)

    def fake_gamepass(context: object) -> tuple[FeedItem, ...]:
        calls.append("gamepass")
        raise AssertionError("the other job must not run")

    monkeypatch.setattr(
        cli, "JOBS", {"zurich-finance": fake_zurich, "gamepass": fake_gamepass}
    )

    assert cli.main(["--root", str(tmp_path), "run", "zurich-finance"]) == 0

    output = tmp_path / "feeds" / "zurich-finance" / "feed.xml"
    assert calls == ["zurich-finance"]
    assert output.exists()
    root = ET.parse(output).getroot()
    assert root.findtext("./channel/item/title") == "CLI Story"
    assert (tmp_path / "feeds" / "gamepass" / "feed.xml").exists() is False


def test_second_identical_run_writes_identical_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_config(tmp_path, "zurich-finance")
    monkeypatch.setattr(
        cli, "JOBS", {"zurich-finance": lambda context: (make_item(),), "gamepass": lambda context: ()}
    )
    output = tmp_path / "feeds" / "zurich-finance" / "feed.xml"

    assert cli.main(["--root", str(tmp_path), "run", "zurich-finance"]) == 0
    first = output.read_bytes()
    assert cli.main(["--root", str(tmp_path), "run", "zurich-finance"]) == 0

    assert output.read_bytes() == first
    assert b"lastBuildDate" not in first


def test_job_failure_leaves_previous_feed_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_config(tmp_path, "zurich-finance")

    def failing(context: object) -> tuple[FeedItem, ...]:
        raise RuntimeError("source exploded")

    monkeypatch.setattr(cli, "JOBS", {"zurich-finance": failing, "gamepass": lambda context: ()})
    output = tmp_path / "feeds" / "zurich-finance" / "feed.xml"
    sentinel = b"previous feed bytes\n"
    output.write_bytes(sentinel)

    assert cli.main(["--root", str(tmp_path), "run", "zurich-finance"]) == 1
    assert output.read_bytes() == sentinel


def test_missing_config_is_a_config_error(tmp_path: Path) -> None:
    assert cli.main(["--root", str(tmp_path), "run", "zurich-finance"]) == 2


def test_invalid_job_settings_exit_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_config(tmp_path, "zurich-finance")

    def invalid(context: object) -> tuple[FeedItem, ...]:
        raise ValueError("bad [job] setting")

    monkeypatch.setattr(cli, "JOBS", {"zurich-finance": invalid, "gamepass": lambda context: ()})
    assert cli.main(["--root", str(tmp_path), "run", "zurich-finance"]) == 2


def test_gamepass_render_dump_and_backfill_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_config(tmp_path, "gamepass", link="https://www.xbox.com/en-us/xbox-game-pass")
    rendered: list[tuple[FeedItem, ...]] = []
    backfills: list[int] = []

    def render_offline(context: object) -> tuple[FeedItem, ...]:
        rendered.append((make_item("Rendered", "rendered"),))
        return rendered[-1]

    def backfill(context: object, count: int) -> tuple[FeedItem, ...]:
        backfills.append(count)
        return (make_item("Backfilled", "backfilled"),)

    def dump(context: object, product_id: str) -> dict | None:
        return {"ProductId": product_id, "Nested": {"ok": True}} if product_id == "KNOWN" else None

    monkeypatch.setattr(
        cli,
        "gamepass",
        SimpleNamespace(
            render_offline=render_offline,
            backfill=backfill,
            dump=dump,
            load_settings=lambda config: SimpleNamespace(bootstrap_announce=7),
        ),
    )
    output = tmp_path / "feeds" / "gamepass" / "feed.xml"

    assert cli.main(["--root", str(tmp_path), "render", "gamepass"]) == 0
    assert len(rendered) == 1
    assert output.exists()
    assert ET.parse(output).getroot().findtext("./channel/item/title") == "Rendered"

    output.unlink()
    assert cli.main(["--root", str(tmp_path), "dump", "gamepass", "KNOWN"]) == 0
    printed = capsys.readouterr().out
    assert '"ProductId": "KNOWN"' in printed
    assert not output.exists()

    assert cli.main(["--root", str(tmp_path), "dump", "gamepass", "MISSING"]) == 1
    assert not output.exists()

    assert cli.main(["--root", str(tmp_path), "backfill", "gamepass", "-1"]) == 2
    assert backfills == []

    assert cli.main(["--root", str(tmp_path), "backfill", "gamepass", "0"]) == 0
    assert backfills == [0]

    assert cli.main(["--root", str(tmp_path), "backfill", "gamepass"]) == 0
    assert backfills == [0, 7]


# ─── Scheduling ──────────────────────────────────────────────────────────────


def _at(hour: int, minute: int = 17) -> datetime:
    return datetime(2026, 11, 21, hour, minute, tzinfo=timezone.utc)


def test_tick_runs_only_due_feeds_and_records_daily_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_config(tmp_path, "hourly")
    write_config(tmp_path, "daily", every="day", at="03:00")
    calls: list[str] = []

    def job(slug: str):
        def run(context: object) -> tuple[FeedItem, ...]:
            calls.append(slug)
            return (make_item(),)
        return run

    monkeypatch.setattr(cli, "JOBS", {"hourly": job("hourly"), "daily": job("daily")})

    assert cli.tick(tmp_path, now=_at(3)) == 0
    assert calls == ["hourly", "daily"]
    assert not (tmp_path / "feeds" / "hourly" / "last_run.json").exists()
    record = json.loads((tmp_path / "feeds" / "daily" / "last_run.json").read_text())
    assert record == {"last_success": "2026-11-21T03:17:00+00:00"}

    calls.clear()
    assert cli.tick(tmp_path, now=_at(4)) == 0
    assert calls == ["hourly"]

    calls.clear()
    assert cli.tick(tmp_path, now=_at(5), force=True) == 0
    assert calls == ["hourly", "daily"]


def test_tick_isolates_failures_and_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_config(tmp_path, "broken", every="day", at="03:00")
    write_config(tmp_path, "healthy")

    def broken(context: object) -> tuple[FeedItem, ...]:
        raise RuntimeError("upstream down")

    monkeypatch.setattr(cli, "JOBS", {"broken": broken, "healthy": lambda context: (make_item(),)})

    assert cli.tick(tmp_path, now=NOW) == 1
    assert (tmp_path / "feeds" / "healthy" / "feed.xml").exists()
    assert not (tmp_path / "feeds" / "broken" / "last_run.json").exists()  # retried next tick
    assert cli.main(["--root", str(tmp_path), "tick"]) == 1


def test_deferred_job_writes_nothing_and_stays_due(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_config(tmp_path, "daily", every="day", at="03:00")

    def deferred(context: object) -> tuple[FeedItem, ...]:
        raise Deferred("cooling down")

    monkeypatch.setattr(cli, "JOBS", {"daily": deferred})

    assert cli.main(["--root", str(tmp_path), "run", "daily"]) == 0
    assert not (tmp_path / "feeds" / "daily" / "feed.xml").exists()
    assert not (tmp_path / "feeds" / "daily" / "last_run.json").exists()


def test_schedule_command_prints_json_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_config(tmp_path, "hourly")
    write_config(tmp_path, "daily", every="day", at="03:00")
    (tmp_path / "feeds" / "daily" / "last_run.json").write_text(
        json.dumps({"last_success": "2026-11-21T03:17:00+00:00"})
    )
    monkeypatch.setattr(cli, "JOBS", {"hourly": lambda c: (), "daily": lambda c: ()})
    before = sorted(tmp_path.rglob("*"))

    assert cli.schedule_report(tmp_path, now=_at(9, 0)) == 0

    assert json.loads(capsys.readouterr().out) == [
        {"feed": "hourly", "schedule": "every hour", "last_success": None, "due": True, "next_run": "next tick"},
        {
            "feed": "daily",
            "schedule": "every day at 03:00 UTC",
            "last_success": "2026-11-21T03:17:00+00:00",
            "due": False,
            "next_run": "2026-11-22T03:00:00+00:00",
        },
    ]
    assert sorted(tmp_path.rglob("*")) == before


# ─── Item cache ──────────────────────────────────────────────────────────────


def _dated(guid: str, hour: int) -> FeedItem:
    item = make_item(f"Story {guid}", guid)
    return FeedItem(**{**item.__dict__, "published": NOW.replace(hour=hour)})


def _feed_guids(path: Path) -> list[str]:
    return [element.text or "" for element in ET.parse(path).getroot().iter("guid")]


def test_cache_keeps_items_the_source_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_config(tmp_path, "news", cache=2)
    batches = [(_dated("a", 1), _dated("b", 2)), (_dated("c", 3),)]
    monkeypatch.setattr(cli, "JOBS", {"news": lambda context: batches.pop(0)})
    directory = tmp_path / "feeds" / "news"

    assert cli.main(["--root", str(tmp_path), "run", "news"]) == 0
    assert cli.main(["--root", str(tmp_path), "run", "news"]) == 0

    # "a" is the oldest and falls past the per-feed limit of 2; "b" survives its source.
    assert _feed_guids(directory / "feed.xml") == ["c", "b"]
    rows = [json.loads(line) for line in (directory / "cache.jsonl").read_text().splitlines()]
    assert [row["guid"] for row in rows] == ["c", "b"]


def test_failed_run_leaves_cache_and_feed_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_config(tmp_path, "news", cache=10)
    outcomes: list[object] = [(_dated("a", 1),), RuntimeError("source down")]

    def job(context: object) -> tuple[FeedItem, ...]:
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome  # type: ignore[return-value]

    monkeypatch.setattr(cli, "JOBS", {"news": job})
    directory = tmp_path / "feeds" / "news"
    assert cli.main(["--root", str(tmp_path), "run", "news"]) == 0
    before = {name: (directory / name).read_bytes() for name in ("feed.xml", "cache.jsonl")}

    assert cli.main(["--root", str(tmp_path), "run", "news"]) == 1
    assert {name: (directory / name).read_bytes() for name in before} == before


def test_feeds_without_cache_table_write_no_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_config(tmp_path, "news")
    monkeypatch.setattr(cli, "JOBS", {"news": lambda context: (_dated("a", 1),)})

    assert cli.main(["--root", str(tmp_path), "run", "news"]) == 0
    assert not (tmp_path / "feeds" / "news" / "cache.jsonl").exists()
