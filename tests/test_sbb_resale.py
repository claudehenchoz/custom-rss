from __future__ import annotations

import io
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image

from custom_rss import cli
from custom_rss.config import load_config
from custom_rss.http import FetchError
from custom_rss.jobs import sbb_resale
from custom_rss.models import JobContext
from custom_rss.rss import MEDIA_NS

NOW = datetime(2026, 11, 25, 6, 17, tzinfo=timezone.utc)
API = "https://api.example.test/offers"
SELF = "https://example.test/feeds/sbb-resale/feed.xml"


def picture(
    width: int, height: int, *, fmt: str = "JPEG", mode: str = "RGB", exif: Image.Exif | None = None
) -> bytes:
    out = io.BytesIO()
    color = (200, 30, 30, 0) if mode == "RGBA" else (200, 30, 30)
    kwargs = {"exif": exif} if exif is not None else {}
    Image.new(mode, (width, height), color).save(out, fmt, **kwargs)
    return out.getvalue()


def write_config(tmp_path: Path, *, job: str = "", cache: bool = True) -> Path:
    lines = [
        'title = "SBB Resale"',
        'description = "Test description."',
        'link = "https://sbbresale.ch/offers"',
        f"self_url = {json.dumps(SELF)}",
        'language = "en"',
        "ttl = 60",
        "max_items = 100",
        "",
        "[schedule]",
        'every = "hour"',
        "",
        *(["[cache]", "max_items = 100", ""] if cache else []),
        "[job]",
        f"api_url = {json.dumps(API)}",
        job,
    ]
    path = tmp_path / "feeds" / "sbb-resale" / "feed.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def offer(offer_id: str = "aaa111", **overrides: object) -> dict:
    base: dict = {
        "__t": "Offer",
        "id": offer_id,
        "title": {"de": "Drehbank", "fr": "", "it": "", "en": ""},
        "description": {"de": "<p>Gut erhalten.</p>", "en": ""},
        "price": 3000,
        "currency": "CHF",
        "startBidPrice": 0,
        "publishDate": "2026-11-16T09:06:59.772Z",
        "createdAt": "2026-11-16T09:00:00Z",
        "thumbnailId": f"thumb-{offer_id}",
        "quantity": 1,
        "quantityUnit": "Piece",
        "condition": "Used",
        "zipCode": "5200",
        "city": "Brugg",
        "manufacturer": "Schaublin",
        "constructionYear": 1980,
        "visibility": True,
    }
    base.update(overrides)
    return base


class FakeHttp:
    def __init__(self, offers: list[dict], *, image: bytes | None = None, fail_image: bool = False) -> None:
        self.offers = offers
        self.image = image if image is not None else picture(1024, 768)
        self.fail_image = fail_image
        self.calls: list[str] = []

    def get_json(self, url: str) -> object:
        self.calls.append(url)
        assert url.startswith(API + "?")
        return {"offers": self.offers, "count": len(self.offers)}

    def get_bytes(self, url: str, *, accept: str) -> bytes:
        self.calls.append(url)
        if url.endswith("/thumbnail"):
            offer_id = url.split("/")[-2]
            return f"https://blob.example.test/offers/{offer_id}/img?sig=secret".encode()
        if url.startswith("https://blob.example.test/"):
            if self.fail_image:
                raise FetchError("boom")
            return self.image
        raise AssertionError(f"unexpected URL {url}")


def context(tmp_path: Path, http: FakeHttp) -> JobContext:
    return JobContext(config=load_config("sbb-resale", tmp_path), http=http, now=NOW)  # type: ignore[arg-type]


def test_builds_item_with_hosted_photo_price_and_facts(tmp_path: Path) -> None:
    write_config(tmp_path)
    http = FakeHttp([offer(quantity=75, price=300, title={"de": "Sitz", "en": "Seat"})])

    (item,) = sbb_resale.build(context(tmp_path, http))

    image_url = "https://example.test/feeds/sbb-resale/images/thumb-aaa111.jpg"
    assert item.title == "Seat · CHF 300 per piece"
    assert item.guid == "sbb-resale:aaa111"
    assert item.link == "https://sbbresale.ch/offers/aaa111?language=en"
    assert item.published == datetime(2026, 11, 16, 9, 6, 59, 772000, tzinfo=timezone.utc)
    assert item.thumbnail is not None and item.thumbnail.url == image_url
    assert (item.thumbnail.width, item.thumbnail.height) == (1000, 750)
    assert item.media_content[0].medium == "image"
    assert f'<img src="{image_url}"' in item.description
    assert "<h2>CHF 300 per piece</h2>" in item.description
    assert "<td>75 pieces</td>" in item.description
    assert "<td>5200 Brugg</td>" in item.description
    assert "<p>Gut erhalten.</p>" in item.description
    assert "sig=secret" not in item.description
    stored = tmp_path / "feeds" / "sbb-resale" / "images" / "thumb-aaa111.jpg"
    with Image.open(stored) as image:
        assert (image.format, image.size) == ("JPEG", (1000, 750))


def test_large_photo_is_downscaled_rotated_and_stripped() -> None:
    exif = Image.Exif()
    exif[0x0112] = 6            # Orientation: rotate 90° clockwise
    exif[0x010F] = "PhoneMaker"
    original = picture(4000, 3000, exif=exif)

    payload, width, height = sbb_resale.normalize_image(original)

    assert (width, height) == (750, 1000)     # portrait after rotation, longest edge 1000
    assert len(payload) < len(original)
    with Image.open(io.BytesIO(payload)) as image:
        assert image.size == (750, 1000)
        assert not image.getexif()
    assert sbb_resale.normalize_image(original) == (payload, width, height)   # deterministic


def test_transparent_png_becomes_jpeg_and_garbage_is_rejected() -> None:
    payload, width, height = sbb_resale.normalize_image(picture(300, 200, fmt="PNG", mode="RGBA"))
    with Image.open(io.BytesIO(payload)) as image:
        assert (image.format, image.mode, image.size) == ("JPEG", "RGB", (300, 200))
        assert image.getpixel((10, 10)) == (255, 255, 255)
    with pytest.raises(ValueError):
        sbb_resale.normalize_image(b"<html>nope</html>")


def test_unreadable_image_fails_the_run(tmp_path: Path) -> None:
    write_config(tmp_path)
    with pytest.raises(FetchError, match="unusable"):
        sbb_resale.build(context(tmp_path, FakeHttp([offer()], image=b"<html>expired</html>")))


def test_existing_image_is_not_downloaded_again(tmp_path: Path) -> None:
    write_config(tmp_path)
    sbb_resale.build(context(tmp_path, FakeHttp([offer()])))
    http = FakeHttp([offer()])

    (item,) = sbb_resale.build(context(tmp_path, http))

    assert len(http.calls) == 1   # only the listing
    assert item.thumbnail is not None and item.thumbnail.width == 1000


def test_auction_and_free_prices() -> None:
    auction = offer(
        __t="Auction",
        price=None,
        startBidPrice=6000,
        minimumBidIncrement=100,
        endDate="2026-12-22T10:00:00Z",
    )
    assert sbb_resale.price_summary(auction) == (
        "Auction from CHF 6'000",
        "Auction, starting bid CHF 6'000",
    )
    assert sbb_resale.price_summary(offer(price=None)) == ("Free", "Free")
    assert sbb_resale.price_summary(offer(price=12.5)) == ("CHF 12.50", "CHF 12.50")
    description = sbb_resale.render_description(
        auction, title="X", link="https://sbbresale.ch/offers/x", body_html="", image_url=None
    )
    assert "22 Dec 2026, 11:00 (Swiss time)" in description
    assert "<td>CHF 100</td>" in description


def test_swiss_time_switches_between_cet_and_cest() -> None:
    winter = sbb_resale.to_swiss_time(datetime(2026, 1, 1, 12, tzinfo=timezone.utc))
    summer = sbb_resale.to_swiss_time(datetime(2026, 7, 1, 12, tzinfo=timezone.utc))
    assert (winter.hour, summer.hour) == (13, 14)
    # 2026: CEST from 29 Mar 01:00 UTC to 25 Oct 01:00 UTC.
    assert sbb_resale.to_swiss_time(datetime(2026, 3, 29, 0, 59, tzinfo=timezone.utc)).hour == 1
    assert sbb_resale.to_swiss_time(datetime(2026, 3, 29, 1, 0, tzinfo=timezone.utc)).hour == 3
    assert sbb_resale.to_swiss_time(datetime(2026, 10, 25, 1, 0, tzinfo=timezone.utc)).hour == 2


def test_failed_image_fetch_writes_nothing(tmp_path: Path) -> None:
    write_config(tmp_path)
    with pytest.raises(FetchError):
        sbb_resale.build(context(tmp_path, FakeHttp([offer("a1"), offer("b2")], fail_image=True)))
    assert not (tmp_path / "feeds" / "sbb-resale" / "images").exists()


def test_offer_without_photo_and_hidden_offer(tmp_path: Path) -> None:
    write_config(tmp_path)
    http = FakeHttp([offer("a1", thumbnailId=None), offer("b2", visibility=False)])

    (item,) = sbb_resale.build(context(tmp_path, http))

    assert item.guid == "sbb-resale:a1"
    assert item.thumbnail is None and "<img" not in item.description


def test_end_to_end_run_is_deterministic_and_prunes_only_unreferenced_images(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_config(tmp_path)
    feed_dir = tmp_path / "feeds" / "sbb-resale"
    http = FakeHttp([offer("a1"), offer("b2")])
    monkeypatch.setattr(cli, "JOBS", {"sbb-resale": sbb_resale.build})
    monkeypatch.setattr(cli, "HttpClient", lambda *args, **kwargs: http)
    (feed_dir / "images").mkdir(parents=True)
    (feed_dir / "images" / "stale.jpg").write_bytes(b"x")

    assert cli.main(["--root", str(tmp_path), "run", "sbb-resale"]) == 0
    first = (feed_dir / "feed.xml").read_bytes()
    assert cli.main(["--root", str(tmp_path), "run", "sbb-resale"]) == 0
    assert (feed_dir / "feed.xml").read_bytes() == first
    assert sorted(path.name for path in (feed_dir / "images").iterdir()) == ["thumb-a1.jpg", "thumb-b2.jpg"]

    # a1 leaves the listing: the cache keeps the item, so its photo stays.
    http.offers = [offer("b2")]
    assert cli.main(["--root", str(tmp_path), "run", "sbb-resale"]) == 0
    root = ET.parse(feed_dir / "feed.xml").getroot()
    assert len(root.findall("./channel/item")) == 2
    assert (feed_dir / "images" / "thumb-a1.jpg").exists()
    assert root.find(f"./channel/item/{{{MEDIA_NS}}}content") is not None


@pytest.mark.parametrize(
    ("job", "message"),
    [
        ("limit = 0", "limit"),
        ('languages = ["en", "xx"]', "languages"),
        ('api_url = "http://insecure.test"', "api_url"),
        ("bogus = 1", "unknown"),
    ],
)
def test_invalid_settings(tmp_path: Path, job: str, message: str) -> None:
    path = write_config(tmp_path)
    text = path.read_text(encoding="utf-8")
    if job.startswith("api_url"):
        text = text.replace(f"api_url = {json.dumps(API)}\n", "")
    path.write_text(text + job + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        sbb_resale.load_settings(load_config("sbb-resale", tmp_path))
