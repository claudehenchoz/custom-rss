from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import custom_rss.cli as cli
from custom_rss.models import FeedItem

NOW = datetime(2026, 11, 21, 3, 0, tzinfo=timezone.utc)


def write_config(
    tmp_path: Path,
    slug: str,
    *,
    title: str = "Test Feed",
    link: str = "https://example.test/feed",
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
    assert capsys.readouterr().out.split() == ["zurich-finance", "gamepass"]


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
