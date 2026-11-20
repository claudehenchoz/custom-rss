from __future__ import annotations

from pathlib import Path

import pytest

from custom_rss.config import load_config

BASE = """\
title = "Test Feed"
description = "A test feed."
link = "https://example.test/feed"
self_url = "https://example.test/feeds/demo/feed.xml"
language = "en-us"
ttl = 60
max_items = 100

[schedule]
every = "hour"

[job]
tier = "ultimate"
"""


def write_config(root: Path, text: str = BASE, slug: str = "demo") -> Path:
    directory = root / "feeds" / slug
    directory.mkdir(parents=True)
    path = directory / "feed.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_common_fields_and_job_settings(tmp_path: Path) -> None:
    write_config(tmp_path)

    config = load_config("demo", tmp_path)

    assert config.slug == "demo"
    assert config.title == "Test Feed"
    assert config.description == "A test feed."
    assert config.link == "https://example.test/feed"
    assert config.self_url == "https://example.test/feeds/demo/feed.xml"
    assert config.language == "en-us"
    assert config.ttl == 60
    assert config.max_items == 100
    assert config.directory == tmp_path / "feeds" / "demo"
    assert config.output_path == tmp_path / "feeds" / "demo" / "feed.xml"
    assert config.state_dir == tmp_path / "feeds" / "demo" / "state"
    assert config.settings == {"tier": "ultimate"}


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ('title = "Test Feed"', 'title = ""', "title"),
        ('description = "A test feed."', 'description = "   "', "description"),
        ('link = "https://example.test/feed"', 'link = "/feed"', "link"),
        ('link = "https://example.test/feed"', 'link = "ftp://example.test/feed"', "link"),
        ('self_url = "https://example.test/feeds/demo/feed.xml"', 'self_url = "feed.xml"', "self_url"),
        ('language = "en-us"', 'language = ""', "language"),
        ("ttl = 60", "ttl = 0", "ttl"),
        ("ttl = 60", 'ttl = "60"', "ttl"),
        ("ttl = 60", "ttl = true", "ttl"),
        ("max_items = 100", "max_items = -1", "max_items"),
        ("max_items = 100", "max_items = 1.5", "max_items"),
    ],
)
def test_rejects_invalid_common_fields_before_writing(
    tmp_path: Path, old: str, new: str, message: str
) -> None:
    write_config(tmp_path, BASE.replace(old, new))

    with pytest.raises(ValueError, match=message):
        load_config("demo", tmp_path)

    assert list((tmp_path / "feeds" / "demo").glob("*.xml")) == []
    assert not (tmp_path / "feeds" / "demo" / "state").exists()


def test_requires_job_table(tmp_path: Path) -> None:
    write_config(tmp_path, BASE.split("[job]")[0])

    with pytest.raises(ValueError, match=r"\[job\] table"):
        load_config("demo", tmp_path)


def test_missing_file_names_the_path(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_config("absent", tmp_path)



def test_requires_schedule_table(tmp_path: Path) -> None:
    write_config(tmp_path, BASE.replace('[schedule]\nevery = "hour"\n\n', ""))

    with pytest.raises(ValueError, match=r"\[schedule\] table is required"):
        load_config("demo", tmp_path)


def test_parses_schedule(tmp_path: Path) -> None:
    write_config(tmp_path, BASE.replace('every = "hour"', 'every = "day"\nat = "05:00"'))

    assert load_config("demo", tmp_path).schedule.describe() == "every day at 05:00 UTC"


def test_cache_table_is_optional_and_validated(tmp_path: Path) -> None:
    write_config(tmp_path)
    assert load_config("demo", tmp_path).cache_max_items is None

    cached = BASE.replace("[job]", "[cache]\nmax_items = 100\n\n[job]")
    write_config(tmp_path, cached, slug="cached")
    assert load_config("cached", tmp_path).cache_max_items == 100

    write_config(tmp_path, cached.replace("max_items = 100\n\n[job]", "max_items = 0\n\n[job]"), slug="zero")
    with pytest.raises(ValueError, match="cache.max_items"):
        load_config("zero", tmp_path)

    write_config(tmp_path, cached.replace("max_items = 100\n\n[job]", "max_items = 5\nttl = 3\n\n[job]"), slug="extra")
    with pytest.raises(ValueError, match=r"unknown \[cache\] setting"):
        load_config("extra", tmp_path)
