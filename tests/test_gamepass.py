from __future__ import annotations

import json
import shutil
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import feedparser
import pytest

from custom_rss.config import load_config
from custom_rss.http import FetchError
from custom_rss.jobs import gamepass
from custom_rss.jobs.gamepass import GameInfo, GamePassSettings
from custom_rss.models import JobContext
from custom_rss.rss import MEDIA_NS, render_feed

NOW = datetime(2026, 11, 21, 3, 0, tzinfo=timezone.utc)
SETTINGS = GamePassSettings(
    tier="ultimate",
    platform="pc",
    market="CH",
    language="en-us",
    bootstrap_announce=100,
    max_fetches_per_day=1,
    failed_retry_cooldown_hours=6,
    max_shrink_ratio=0.10,
    relisting_window_days=30,
)
HEADER = {"siglId": "97c6c862-d28a-4907-a3d5-c401f2296a53", "title": "All games"}


def write_config(tmp_path: Path, **overrides: object) -> Path:
    values: dict[str, object] = {
        "tier": "ultimate",
        "platform": "pc",
        "market": "CH",
        "bootstrap_announce": 100,
        "max_fetches_per_day": 1,
        "failed_retry_cooldown_hours": 6,
        "max_shrink_ratio": 0.10,
        "relisting_window_days": 30,
    }
    values.update(overrides)
    lines = [
        f"title = {json.dumps('New on Game Pass Ultimate (PC, CH)')}",
        f"description = {json.dumps('Games newly added to the Xbox Game Pass catalog.')}",
        f"link = {json.dumps('https://www.xbox.com/en-us/xbox-game-pass')}",
        f"self_url = {json.dumps('https://example.test/feeds/gamepass/feed.xml')}",
        f"language = {json.dumps('en-us')}",
        "ttl = 1440",
        "max_items = 100",
        "",
        "[job]",
    ]
    for key, value in values.items():
        lines.append(f"{key} = {json.dumps(value)}")
    path = tmp_path / "feeds" / "gamepass" / "feed.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def make_info(
    product_id: str,
    title: str,
    *,
    poster: str = "",
    screenshots: tuple[str, ...] = (),
    genres: tuple[str, ...] = ("Action & adventure",),
    first_seen: datetime = NOW,
) -> GameInfo:
    return GameInfo(
        product_id=product_id,
        title=title,
        short_description=f"{title} short description.",
        description=f"{title} long description.\n\nSecond paragraph.",
        publisher="Publisher",
        developer="Developer",
        genres=list(genres),
        features=["XPA"],
        player_modes=["Single player (1)"],
        content_rating="PEGI 12",
        release_date="2025-01-01",
        poster_url=poster,
        hero_url="",
        screenshot_urls=list(screenshots),
        store_url=gamepass.store_url(SETTINGS, product_id, title),
    )


def record(
    info: GameInfo,
    *,
    first_seen: datetime = NOW,
    announced: bool = True,
    suppressed_reason: str | None = None,
    removed_at: datetime | None = None,
) -> dict:
    return {
        "product_id": info.product_id,
        "title": info.title,
        "family_key": gamepass.family_key(info.title),
        "first_seen": first_seen.isoformat(),
        "removed_at": removed_at.isoformat() if removed_at else None,
        "announced": announced,
        "suppressed_reason": suppressed_reason,
        "info": info.__dict__.copy(),
    }


def run_row(started_at: datetime, status: str = "ok", *, product_count: int | None = None) -> dict:
    return {
        "started_at": started_at.isoformat(),
        "status": status,
        "product_count": product_count,
        "added": 0,
        "announced": 0,
        "removed": 0,
        "error": None,
    }


def write_state(
    tmp_path: Path,
    records: list[dict],
    *,
    runs: list[dict] | None = None,
    raw_products: dict[str, dict] | None = None,
    catalog: bytes = b"[]\n",
) -> Path:
    state = tmp_path / "feeds" / "gamepass" / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "products.jsonl").write_text(
        "".join(json.dumps(item, separators=(",", ":")) + "\n" for item in sorted(records, key=lambda r: r["product_id"])),
        encoding="utf-8",
    )
    (state / "runs.jsonl").write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in (runs or [])),
        encoding="utf-8",
    )
    (state / "catalog.json").write_bytes(catalog)
    raw_dir = state / "raw"
    raw_dir.mkdir(exist_ok=True)
    for product_id, product in (raw_products or {}).items():
        (raw_dir / f"{product_id}.json").write_text(json.dumps(product), encoding="utf-8")
    return state


def raw_product(
    product_id: str,
    title: str,
    *,
    subscription_start: str = "2026-11-01T00:00:00.0000000Z",
    release: str = "2025-02-18T18:00:00.0000000Z",
    screenshots: int = 2,
    poster: bool = True,
) -> dict:
    images: list[dict] = []
    if poster:
        images.append(
            {
                "ImagePurpose": "Poster",
                "Uri": f"//store-images.s-microsoft.com/image/poster-{product_id}",
                "Width": 1440,
                "Height": 2160,
            }
        )
    for index in range(screenshots):
        images.append(
            {
                "ImagePurpose": "Screenshot",
                "Uri": f"//store-images.s-microsoft.com/image/shot-{index}-{product_id}",
                "Width": 1920,
                "Height": 1080,
                "UnscaledImageSHA256Hash": f"hash-{index}-{product_id}",
            }
        )
    return {
        "ProductId": product_id,
        "LastModifiedDate": "2026-11-01T00:00:00.0000000Z",
        "LocalizedProperties": [
            {
                "ProductTitle": title,
                "ShortDescription": f"{title} short.",
                "ProductDescription": f"{title} long.\n\nMore.",
                "PublisherName": "Publisher",
                "DeveloperName": "Developer",
                "Images": images,
            }
        ],
        "MarketProperties": [
            {
                "Market": "CH",
                "OriginalReleaseDate": release,
                "ContentRatings": [{"RatingSystem": "PEGI", "RatingId": "PEGI:12"}],
            }
        ],
        "Properties": {
            "Category": "Action & adventure",
            "Categories": ["Family & kids", "Action & adventure"],
            "Attributes": [
                {"Name": "XPA", "Minimum": None, "Maximum": None},
                {"Name": "Single player", "Minimum": 1, "Maximum": 1},
            ],
        },
        "DisplaySkuAvailabilities": [
            {
                "Availabilities": [
                    {
                        "Conditions": {"StartDate": subscription_start},
                        "Remediations": [{"BigId": "cfq7ttc0khs0"}],
                    }
                ]
            }
        ],
    }


class FakeJsonHttp:
    def __init__(self, listing: list | Exception, products: dict[str, dict] | None = None) -> None:
        self.listing = listing
        self.products = products or {}
        self.calls: list[str] = []

    def get_json(self, url: str):
        self.calls.append(url)
        if "catalog.gamepass.com" in url:
            if isinstance(self.listing, Exception):
                raise self.listing
            return self.listing
        query = parse_qs(urlsplit(url).query)
        ids = query["bigIds"][0].split(",")
        return {"Products": [self.products[pid] for pid in ids if pid in self.products]}

    def display_calls(self) -> list[str]:
        return [url for url in self.calls if "displaycatalog" in url]


def make_context(tmp_path: Path, http: object, *, force: bool = True, now: datetime = NOW) -> JobContext:
    return JobContext(
        config=load_config("gamepass", tmp_path),
        http=http,  # type: ignore[arg-type]
        now=now,
        force=force,
    )


def test_new_product_fetches_only_unseen_metadata_and_keeps_guid(tmp_path: Path) -> None:
    write_config(tmp_path)
    known_info = make_info("AAAA1", "Known Game")
    write_state(
        tmp_path,
        [record(known_info, first_seen=NOW - timedelta(days=40))],
        raw_products={"AAAA1": raw_product("AAAA1", "Known Game")},
    )
    http = FakeJsonHttp(
        [HEADER, {"id": "AAAA1"}, {"id": "NEW01"}],
        {"NEW01": raw_product("NEW01", "Fresh Game")},
    )
    context = make_context(tmp_path, http)

    items = gamepass.build(context)

    assert len(http.display_calls()) == 1
    assert "NEW01" in http.display_calls()[0]
    assert "AAAA1" not in http.display_calls()[0]
    assert {item.guid for item in items} == {"AAAA1", "NEW01"}
    fresh = next(item for item in items if item.guid == "NEW01")
    assert fresh.link == gamepass.store_url(SETTINGS, "NEW01", "Fresh Game")
    assert fresh.published == NOW
    assert fresh.categories == ("Action & adventure", "Family & kids")

    payload = render_feed(context.config, items)
    parsed = feedparser.parse(payload)
    assert parsed.version == "rss20"
    entry = next(entry for entry in parsed.entries if entry.id == "NEW01")
    assert entry.link == fresh.link
    assert entry.title == "Fresh Game"
    assert entry.published_parsed[:6] == (2026, 11, 21, 3, 0, 0)
    root = ET.fromstring(payload)
    xml_item = next(
        element for element in root.findall("./channel/item") if element.findtext("guid") == "NEW01"
    )
    assert xml_item.find("guid").attrib["isPermaLink"] == "false"
    assert xml_item.find("enclosure") is None
    assert xml_item.find(f"{{{MEDIA_NS}}}thumbnail") is not None
    assert len(xml_item.findall(f"{{{MEDIA_NS}}}content")) == 2

    state = tmp_path / "feeds" / "gamepass" / "state"
    rows = [json.loads(line) for line in (state / "products.jsonl").read_text().splitlines()]
    fresh_row = next(row for row in rows if row["product_id"] == "NEW01")
    assert fresh_row["announced"] is True
    assert (state / "raw" / "NEW01.json").exists()
    last_run = json.loads((state / "runs.jsonl").read_text().splitlines()[-1])
    assert last_run["status"] == "ok"
    assert last_run["added"] == 1
    assert last_run["announced"] == 1


def test_recent_relisting_is_suppressed(tmp_path: Path) -> None:
    write_config(tmp_path)
    old_info = make_info("OLD01", "Forza Horizon 5")
    write_state(
        tmp_path,
        [
            record(
                old_info,
                announced=False,
                suppressed_reason="relisting",
                removed_at=NOW - timedelta(days=5),
            )
        ],
    )
    http = FakeJsonHttp(
        [HEADER, {"id": "NEW02"}],
        {"NEW02": raw_product("NEW02", "Forza Horizon 5 Deluxe Edition")},
    )
    context = make_context(tmp_path, http)

    items = gamepass.build(context)

    assert items == ()
    state = tmp_path / "feeds" / "gamepass" / "state"
    rows = [json.loads(line) for line in (state / "products.jsonl").read_text().splitlines()]
    new_row = next(row for row in rows if row["product_id"] == "NEW02")
    assert new_row["announced"] is False
    assert new_row["suppressed_reason"] == "relisting"
    assert json.loads((state / "runs.jsonl").read_text().splitlines()[-1])["announced"] == 0


def test_empty_and_shrinking_listings_leave_last_good_state(tmp_path: Path) -> None:
    write_config(tmp_path)
    known = make_info("AAAA1", "Known Game")
    state = write_state(
        tmp_path,
        [record(known)],
        runs=[run_row(NOW - timedelta(days=2), product_count=100)],
        catalog=b'[{"all": true}]\n',
    )
    context = make_context(tmp_path, FakeJsonHttp([]))
    context.config.output_path.write_bytes(b"sentinel feed\n")
    product_bytes = (state / "products.jsonl").read_bytes()
    catalog_bytes = (state / "catalog.json").read_bytes()
    run_count = len((state / "runs.jsonl").read_text().splitlines())

    with pytest.raises(FetchError, match="empty"):
        gamepass.build(context)

    assert (state / "products.jsonl").read_bytes() == product_bytes
    assert (state / "catalog.json").read_bytes() == catalog_bytes
    assert context.config.output_path.read_bytes() == b"sentinel feed\n"
    runs = [json.loads(line) for line in (state / "runs.jsonl").read_text().splitlines()]
    assert len(runs) == run_count + 1
    assert runs[-1]["status"] == "failed"
    assert "empty" in runs[-1]["error"]

    shrink = [HEADER] + [{"id": f"S{index:04d}"} for index in range(50)]
    context = make_context(tmp_path, FakeJsonHttp(shrink))
    with pytest.raises(FetchError, match="shrank"):
        gamepass.build(context)
    assert (state / "products.jsonl").read_bytes() == product_bytes
    assert context.config.output_path.read_bytes() == b"sentinel feed\n"


def test_bootstrap_backfill_is_idempotent(tmp_path: Path) -> None:
    write_config(tmp_path)
    products = {
        "AAA": raw_product("AAA", "Oldest", subscription_start="2026-01-01T00:00:00.0000000Z"),
        "BBB": raw_product("BBB", "Middle", subscription_start="2026-06-01T00:00:00.0000000Z"),
        "CCC": raw_product("CCC", "Newest", subscription_start="2026-10-01T00:00:00.0000000Z"),
    }
    write_state(
        tmp_path,
        [
            record(make_info(pid, products[pid]["LocalizedProperties"][0]["ProductTitle"]), announced=False, suppressed_reason="bootstrap")
            for pid in products
        ],
        raw_products=products,
    )
    context = make_context(tmp_path, object())

    first = gamepass.backfill(context, 2)
    first_bytes = render_feed(context.config, first)
    state = tmp_path / "feeds" / "gamepass" / "state"
    first_rows = (state / "products.jsonl").read_bytes()
    assert [item.guid for item in first] == ["CCC", "BBB"]
    assert [item.published for item in first] == [
        datetime(2026, 10, 1, tzinfo=timezone.utc),
        datetime(2026, 6, 1, tzinfo=timezone.utc),
    ]

    second = gamepass.backfill(context, 2)
    assert render_feed(context.config, second) == first_bytes
    assert (state / "products.jsonl").read_bytes() == first_rows

    smaller = gamepass.backfill(context, 1)
    assert [item.guid for item in smaller] == ["CCC"]


def test_skip_and_offline_render_do_no_http(tmp_path: Path) -> None:
    write_config(tmp_path)
    now = datetime.now(timezone.utc)
    info = make_info("AAAA1", "Known Game", first_seen=now)
    write_state(
        tmp_path,
        [record(info, first_seen=now)],
        runs=[run_row(now, product_count=10)],
    )

    class ForbiddenHttp:
        def get_json(self, url: str):
            raise AssertionError(f"unexpected HTTP request: {url}")

    context = make_context(tmp_path, ForbiddenHttp(), force=False, now=now)
    build_items = gamepass.build(context)
    offline_items = gamepass.render_offline(context)

    assert [item.guid for item in build_items] == ["AAAA1"]
    assert [item.guid for item in offline_items] == ["AAAA1"]


def test_fetch_failure_appends_failed_run_and_preserves_feed(tmp_path: Path) -> None:
    write_config(tmp_path)
    info = make_info("AAAA1", "Known Game")
    state = write_state(tmp_path, [record(info)], catalog=b'[{"all": true}]\n')
    context = make_context(tmp_path, FakeJsonHttp(FetchError("GET catalog failed: boom")))
    context.config.output_path.write_bytes(b"sentinel feed\n")
    products_before = (state / "products.jsonl").read_bytes()
    catalog_before = (state / "catalog.json").read_bytes()

    with pytest.raises(FetchError, match="boom"):
        gamepass.build(context)

    assert context.config.output_path.read_bytes() == b"sentinel feed\n"
    assert (state / "products.jsonl").read_bytes() == products_before
    assert (state / "catalog.json").read_bytes() == catalog_before
    runs = [json.loads(line) for line in (state / "runs.jsonl").read_text().splitlines()]
    assert runs[-1]["status"] == "failed"
    assert "boom" in runs[-1]["error"]


def test_migration_fixture_renders_expected_identity(tmp_path: Path) -> None:
    write_config(tmp_path)
    state = tmp_path / "feeds" / "gamepass" / "state"
    fixture = tmp_path / "fixture"
    (fixture / "raw").mkdir(parents=True)
    product = raw_product("9PNJXVCVWD4K", "Migrated Game")
    info = gamepass.extract_game_info(SETTINGS, "9PNJXVCVWD4K", product)
    first_seen = datetime(2026, 9, 4, tzinfo=timezone.utc)
    row = record(info, first_seen=first_seen)
    (fixture / "products.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    (fixture / "raw" / "9PNJXVCVWD4K.json").write_text(json.dumps(product), encoding="utf-8")

    state.mkdir(parents=True)
    shutil.copy2(fixture / "products.jsonl", state / "products.jsonl")
    (state / "raw").mkdir()
    shutil.copy2(fixture / "raw" / "9PNJXVCVWD4K.json", state / "raw" / "9PNJXVCVWD4K.json")
    (state / "runs.jsonl").write_text("", encoding="utf-8")
    context = make_context(tmp_path, object())

    items = gamepass.render_offline(context)

    assert [(item.guid, item.link, item.title, item.published) for item in items] == [
        (
            "9PNJXVCVWD4K",
            gamepass.store_url(SETTINGS, "9PNJXVCVWD4K", "Migrated Game"),
            "Migrated Game",
            first_seen,
        )
    ]
    payload = render_feed(context.config, items)
    parsed = feedparser.parse(payload)
    assert parsed.entries[0].id == "9PNJXVCVWD4K"
    assert parsed.entries[0].published_parsed[:6] == (2026, 9, 4, 0, 0, 0)
    assert parsed.feed.title == "New on Game Pass Ultimate (PC, CH)"


def test_missing_store_metadata_produces_stub(tmp_path: Path) -> None:
    write_config(tmp_path)
    write_state(tmp_path, [record(make_info("AAAA1", "Known Game"))])
    http = FakeJsonHttp([HEADER, {"id": "STUB1"}], {})
    context = make_context(tmp_path, http)

    items = gamepass.build(context)

    stub = next(item for item in items if item.guid == "STUB1")
    assert stub.title == "STUB1"
    assert stub.link == "https://www.xbox.com/en-us/games/store/stub1/STUB1"
    assert stub.thumbnail is None


def test_extract_game_info_preserves_store_heuristics() -> None:
    product = raw_product("9PNJXVCVWD4K", "Zürich Game", screenshots=8)
    product["LocalizedProperties"][0]["Images"][0]["Uri"] = (
        "//store-images.s-microsoft.com/image/apps.63195.poster?w=1"
    )

    info = gamepass.extract_game_info(SETTINGS, "9PNJXVCVWD4K", product)

    assert info.title == "Zürich Game"
    assert info.poster_url == "https://store-images.s-microsoft.com/image/apps.63195.poster?w=1"
    assert len(info.screenshot_urls) == 6
    assert all("w=640&h=360&q=80&format=jpg" in url for url in info.screenshot_urls)
    assert info.genres == ["Action & adventure", "Family & kids"]
    assert info.content_rating == "PEGI 12"
    assert info.release_date == "2025-02-18"
    assert info.player_modes == ["Single player (1)"]
    assert info.store_url == "https://www.xbox.com/en-us/games/store/zurich-game/9PNJXVCVWD4K"
    assert gamepass.infer_added_date(product, SETTINGS) == ("2026-11-01", "subscription")


def test_family_key_normalisation() -> None:
    assert gamepass.family_key("Forza Horizon 5 – Windows") == "forza horizon 5"
    assert gamepass.family_key("FORZA HORIZON V") == "forza horizon 5"
    assert gamepass.family_key("Forza Horizon 5 Deluxe Edition") == "forza horizon 5"
    assert gamepass.family_key("Avowed Standard Edition") == "avowed"
    assert gamepass.family_key("FC 26") != gamepass.family_key("FC 27")


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"tier": "platinum"}, "tier"),
        ({"platform": "mobile"}, "platform"),
        ({"market": "ch"}, "market"),
        ({"market": "CHE"}, "market"),
        ({"bootstrap_announce": -1}, "bootstrap_announce"),
        ({"max_fetches_per_day": 0}, "max_fetches_per_day"),
        ({"failed_retry_cooldown_hours": 0}, "failed_retry_cooldown_hours"),
        ({"max_shrink_ratio": 1.0}, "max_shrink_ratio"),
        ({"max_shrink_ratio": -0.1}, "max_shrink_ratio"),
        ({"max_shrink_ratio": "x"}, "max_shrink_ratio"),
        ({"relisting_window_days": 0}, "relisting_window_days"),
        ({"mystery": 1}, "unknown"),
    ],
)
def test_settings_validation(tmp_path: Path, overrides: dict[str, object], message: str) -> None:
    write_config(tmp_path, **overrides)
    config = load_config("gamepass", tmp_path)

    with pytest.raises(ValueError, match=message):
        gamepass.load_settings(config)


def test_settings_use_common_language(tmp_path: Path) -> None:
    write_config(tmp_path)
    settings = gamepass.load_settings(load_config("gamepass", tmp_path))
    assert settings.language == "en-us"
    assert settings.tier == "ultimate"
    assert settings.bootstrap_announce == 100
