"""New listings on SBB Resale (sbbresale.ch), each with its photo, price and key facts.

The site is a single-page app backed by a public JSON API. Its photos are only
served through signed Azure Blob URLs that expire after ~15 minutes, so the job
copies each offer's main photo into ``feeds/<slug>/images/`` and the feed links
to that copy on GitHub Pages. Each photo is fetched and downscaled once (JPEG,
longest edge ``MAX_IMAGE_SIZE``, metadata dropped; the file name is the site's
immutable ``thumbnailId``), written only after every fetch succeeded, and pruned
once no fresh or cached item references it.
"""

from __future__ import annotations

import html
import io
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

from PIL import Image, ImageOps

from ..cache import load_cache
from ..files import write_bytes_atomic
from ..http import FetchError
from ..models import FeedConfig, FeedItem, JobContext, Media

log = logging.getLogger("custom_rss.jobs.sbb_resale")

IMAGE_DIR = "images"
SITE_URL = "https://sbbresale.ch"

_JOB_KEYS = {"api_url", "limit", "languages"}
_LANGUAGES = ("en", "de", "fr", "it")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_IMAGE_SIZE = 1000   # longest edge, px
JPEG_QUALITY = 75

_UNITS = {
    # quantityUnit -> (singular, plural)
    "Piece": ("piece", "pieces"),
    "Meter": ("m", "m"),
    "SquareMeter": ("m²", "m²"),
    "CubicMeter": ("m³", "m³"),
    "Kilogram": ("kg", "kg"),
    "MetricTons": ("t", "t"),
    "Lot": ("lot", "lots"),
}
_LENGTH_UNITS = {"Meter": "m", "CentiMeter": "cm", "Centimeter": "cm", "MilliMeter": "mm", "Millimeter": "mm"}
_WEIGHT_UNITS = {"Kilogram": "kg", "MetricTons": "t", "Gram": "g"}


@dataclass(frozen=True)
class SbbResaleSettings:
    api_url: str
    limit: int
    languages: tuple[str, ...]


@dataclass(frozen=True)
class StoredImage:
    name: str               # file name under images/
    payload: bytes | None   # None = already on disk
    width: int
    height: int


def load_settings(config: FeedConfig) -> SbbResaleSettings:
    where = config.directory / "feed.toml"
    raw: dict[str, Any] = dict(config.settings)

    unknown = sorted(set(raw) - _JOB_KEYS)
    if unknown:
        raise ValueError(f"{where}: unknown [job] setting(s): {', '.join(unknown)}")

    api_url = raw.get("api_url", "https://sbb-resale-webapi.azurewebsites.net/offers")
    if not isinstance(api_url, str) or urlsplit(api_url).scheme != "https" or not urlsplit(api_url).hostname:
        raise ValueError(f"{where}: setting 'api_url' must be an absolute HTTPS URL")

    limit = raw.get("limit", 50)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ValueError(f"{where}: setting 'limit' must be an integer between 1 and 200")

    languages = raw.get("languages", list(_LANGUAGES))
    if (
        not isinstance(languages, list)
        or not languages
        or any(language not in _LANGUAGES for language in languages)
        or len(set(languages)) != len(languages)
    ):
        raise ValueError(
            f"{where}: setting 'languages' must be a non-empty list of distinct values from "
            f"{', '.join(_LANGUAGES)}"
        )

    return SbbResaleSettings(api_url=api_url.rstrip("/"), limit=limit, languages=tuple(languages))


# --- formatting ---------------------------------------------------------------


def format_amount(value: float | int, currency: str) -> str:
    """Swiss style: ``CHF 32'000``, ``CHF 12.50``."""
    if float(value).is_integer():
        number = f"{int(value):,}".replace(",", "'")
    else:
        number = f"{value:,.2f}".replace(",", "'")
    return f"{currency} {number}"


def _number(value: object) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return int(value) if float(value).is_integer() else value


def _quantity(value: float | int, unit: str | None) -> str:
    singular, plural = _UNITS.get(unit or "", ("", ""))
    label = singular if value == 1 else plural
    text = f"{value:,}".replace(",", "'") if isinstance(value, int) else str(value)
    return f"{text} {label}".strip()


def _last_sunday(year: int, month: int) -> datetime:
    day = datetime(year, month + 1, 1, tzinfo=timezone.utc) - timedelta(days=1)
    return day - timedelta(days=(day.weekday() + 1) % 7)


def to_swiss_time(value: datetime) -> datetime:
    """UTC -> Europe/Zurich (CET/CEST) without needing tzdata on the host."""
    value = value.astimezone(timezone.utc)
    dst_start = _last_sunday(value.year, 3).replace(hour=1)
    dst_end = _last_sunday(value.year, 10).replace(hour=1)
    offset = 2 if dst_start <= value < dst_end else 1
    return value.astimezone(timezone(timedelta(hours=offset)))


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _date(value: datetime) -> str:
    return f"{value.day} {value:%b %Y}"


def _datetime(value: datetime) -> str:
    local = to_swiss_time(value)
    return f"{local.day} {local:%b %Y, %H:%M} (Swiss time)"


def _localized(value: object, languages: tuple[str, ...]) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    for language in languages:
        text = value.get(language)
        if isinstance(text, str) and text.strip():
            return text.strip()
    return ""


def _is_auction(offer: dict) -> bool:
    return offer.get("__t") == "Auction" or bool(_number(offer.get("startBidPrice")))


def price_summary(offer: dict) -> tuple[str, str]:
    """(short label for the title, full label for the description)."""
    currency = str(offer.get("currency") or "CHF")
    price = _number(offer.get("price"))
    unit = _UNITS.get(str(offer.get("quantityUnit") or ""), (None,))[0]
    per = f" per {unit}" if unit and (_number(offer.get("quantity")) or 0) > 1 else ""

    if _is_auction(offer):
        start = _number(offer.get("startBidPrice")) or 0
        short = f"Auction from {format_amount(start, currency)}"
        return short, f"Auction, starting bid {format_amount(start, currency)}"
    if not price:
        return "Free", "Free"
    amount = format_amount(price, currency)
    return f"{amount}{per}", f"{amount}{per}"


def _dimensions(offer: dict) -> str:
    parts = []
    for key, label in (("length", "L"), ("width", "W"), ("height", "H")):
        value = _number(offer.get(key))
        if value:
            unit = _LENGTH_UNITS.get(str(offer.get(f"{key}Unit") or ""), "")
            parts.append(f"{label} {value} {unit}".strip())
    return " × ".join(parts)


def _facts(offer: dict) -> list[tuple[str, str]]:
    currency = str(offer.get("currency") or "CHF")
    facts: list[tuple[str, str]] = []   # The price itself is the heading above the table.

    if _is_auction(offer):
        if price := _number(offer.get("price")):
            facts.append(("Buy now", format_amount(price, currency)))
        if increment := _number(offer.get("minimumBidIncrement")):
            facts.append(("Min. increment", format_amount(increment, currency)))
        if start := _parse_datetime(offer.get("startDate")):
            facts.append(("Auction starts", _datetime(start)))
        if end := _parse_datetime(offer.get("endDate")):
            facts.append(("Auction ends", _datetime(end)))

    quantity = _number(offer.get("quantity"))
    if quantity:
        text = _quantity(quantity, offer.get("quantityUnit"))
        minimum = _number(offer.get("minimumQuantity"))
        if minimum and minimum > 1:
            text += f" (min. {_quantity(minimum, offer.get('quantityUnit'))})"
        facts.append(("Quantity", text))

    if condition := str(offer.get("condition") or "").strip():
        facts.append(("Condition", condition))

    location = " ".join(
        part for part in (str(offer.get("zipCode") or "").strip(), str(offer.get("city") or "").strip()) if part
    )
    if location:
        facts.append(("Location", location))

    if manufacturer := str(offer.get("manufacturer") or "").strip():
        facts.append(("Manufacturer", manufacturer))
    if year := _number(offer.get("constructionYear")):
        facts.append(("Year built", str(year)))
    if dimensions := _dimensions(offer):
        facts.append(("Dimensions", dimensions))
    if weight := _number(offer.get("weight")):
        unit = _WEIGHT_UNITS.get(str(offer.get("weightUnit") or ""), "")
        facts.append(("Weight", f"{weight} {unit}".strip()))

    available_from = _parse_datetime(offer.get("availableFrom"))
    available_till = _parse_datetime(offer.get("availableTill"))
    if available_from or available_till:
        start = _date(to_swiss_time(available_from)) if available_from else "now"
        end = f" until {_date(to_swiss_time(available_till))}" if available_till else ""
        facts.append(("Available", f"from {start}{end}"))
    return facts


def render_description(
    offer: dict, *, title: str, link: str, body_html: str, image_url: str | None
) -> str:
    esc = html.escape
    _, price_label = price_summary(offer)
    parts: list[str] = []
    if image_url:
        parts.append(
            f'<p><a href="{esc(link)}"><img src="{esc(image_url)}" alt="{esc(title)}" '
            f'style="max-width:100%;height:auto"></a></p>'
        )
    parts.append(f"<h2>{esc(price_label)}</h2>")
    rows = "".join(
        f'<tr><th align="left" valign="top" style="padding-right:1em">{esc(label)}</th>'
        f"<td>{esc(value)}</td></tr>"
        for label, value in _facts(offer)
    )
    parts.append(f"<table>{rows}</table>")
    if body_html:
        parts.append("<hr>")
        parts.append(body_html)
    parts.append(f'<p><a href="{esc(link)}"><strong>View offer on SBB Resale →</strong></a></p>')
    return "\n".join(parts)


# --- images -------------------------------------------------------------------


def normalize_image(payload: bytes) -> tuple[bytes, int, int]:
    """Downscale once to a feed-friendly JPEG: EXIF rotation applied, metadata (GPS) dropped.

    Returns (jpeg bytes, width, height); raises ValueError for anything that is not an image.
    """
    try:
        with Image.open(io.BytesIO(payload)) as source:
            source.load()
            picture = ImageOps.exif_transpose(source)
    except (OSError, SyntaxError, Image.DecompressionBombError) as error:
        raise ValueError(f"not a readable image: {error}") from error
    if picture.mode not in ("RGB", "L"):
        rgba = picture.convert("RGBA")
        picture = Image.new("RGB", rgba.size, (255, 255, 255))
        picture.paste(rgba, mask=rgba.getchannel("A"))
    picture.thumbnail((MAX_IMAGE_SIZE, MAX_IMAGE_SIZE), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    picture.save(out, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)
    return out.getvalue(), picture.width, picture.height


def _existing_image(directory: Path, thumbnail_id: str) -> StoredImage | None:
    path = directory / f"{thumbnail_id}.jpg"
    if not path.is_file():
        return None
    try:
        with Image.open(path) as stored:
            width, height = stored.size
    except OSError:
        return None   # Unreadable leftover: fetch it again.
    return StoredImage(name=path.name, payload=None, width=width, height=height)


def _fetch_image(
    context: JobContext, settings: SbbResaleSettings, offer_id: str, thumbnail_id: str
) -> StoredImage:
    signed = context.http.get_bytes(f"{settings.api_url}/{offer_id}/thumbnail", accept="text/plain")
    signed_url = signed.decode("utf-8", "replace").strip().strip('"')
    if urlsplit(signed_url).scheme != "https":
        raise FetchError(f"offer {offer_id}: thumbnail endpoint returned no HTTPS URL")
    payload = context.http.get_bytes(signed_url, accept="image/*")
    try:
        jpeg, width, height = normalize_image(payload)
    except ValueError as error:
        raise FetchError(f"offer {offer_id}: thumbnail is unusable: {error}") from error
    return StoredImage(name=f"{thumbnail_id}.jpg", payload=jpeg, width=width, height=height)


def _image_base_url(config: FeedConfig) -> str:
    return config.self_url.rsplit("/", 1)[0] + f"/{IMAGE_DIR}/"


def _prune_images(directory: Path, keep: set[str]) -> None:
    if not directory.is_dir():
        return
    for path in directory.iterdir():
        if path.is_file() and path.name not in keep:
            log.info("pruning unreferenced image %s", path.name)
            path.unlink()


# --- job ----------------------------------------------------------------------


def fetch_offers(context: JobContext, settings: SbbResaleSettings) -> list[dict]:
    query = urlencode(
        {"page": 1, "limit": settings.limit, "sortField": "updatedAt", "sortType": "desc"}
    )
    payload = context.http.get_json(f"{settings.api_url}?{query}")
    offers = payload.get("offers") if isinstance(payload, dict) else None
    if not isinstance(offers, list):
        raise FetchError("SBB Resale API response has no 'offers' list")
    return [offer for offer in offers if isinstance(offer, dict)]


def build(context: JobContext) -> tuple[FeedItem, ...]:
    config = context.config
    settings = load_settings(config)
    image_dir = config.directory / IMAGE_DIR
    base_url = _image_base_url(config)

    offers = fetch_offers(context, settings)
    staged: list[tuple[dict, str, StoredImage | None]] = []
    seen: set[str] = set()
    for offer in offers:
        offer_id = str(offer.get("id") or "")
        if not _ID_RE.match(offer_id) or offer_id in seen:
            log.warning("skipping offer with missing or duplicate id: %r", offer_id)
            continue
        if offer.get("visibility") is False:
            continue
        seen.add(offer_id)
        thumbnail_id = str(offer.get("thumbnailId") or "")
        image: StoredImage | None = None
        if _ID_RE.match(thumbnail_id):
            image = _existing_image(image_dir, thumbnail_id) or _fetch_image(
                context, settings, offer_id, thumbnail_id
            )
        staged.append((offer, offer_id, image))

    items: list[FeedItem] = []
    for offer, offer_id, image in staged:
        title = _localized(offer.get("title"), settings.languages) or f"Offer {offer_id}"
        body = _localized(offer.get("description"), settings.languages)
        link = f"{SITE_URL}/offers/{offer_id}?language={settings.languages[0]}"
        short_price, _ = price_summary(offer)
        image_url = base_url + image.name if image else None
        media = (
            Media(url=image_url, medium="image", width=image.width, height=image.height)
            if image and image_url
            else None
        )
        items.append(
            FeedItem(
                title=f"{title} · {short_price}",
                link=link,
                guid=f"sbb-resale:{offer_id}",
                description=render_description(
                    offer, title=title, link=link, body_html=body, image_url=image_url
                ),
                published=_parse_datetime(offer.get("publishDate"))
                or _parse_datetime(offer.get("approvedAt"))
                or _parse_datetime(offer.get("createdAt")),
                categories=("Auction" if _is_auction(offer) else "Offer",),
                thumbnail=Media(url=media.url, width=media.width, height=media.height) if media else None,
                media_content=(media,) if media else (),
            )
        )

    # Every fetch succeeded: now touch the disk.
    for _, _, image in staged:
        if image is not None and image.payload is not None:
            write_bytes_atomic(image_dir / image.name, image.payload)

    keep = {item.thumbnail.url.rsplit("/", 1)[1] for item in items if item.thumbnail}
    if config.cache_max_items is not None:
        keep |= {
            item.thumbnail.url.rsplit("/", 1)[1]
            for item in load_cache(config.directory)
            if item.thumbnail and item.thumbnail.url.startswith(base_url)
        }
    _prune_images(image_dir, keep)

    log.info("%d offers, %d with photo", len(items), sum(1 for item in items if item.thumbnail))
    return tuple(items)
