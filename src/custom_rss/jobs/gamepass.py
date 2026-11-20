"""RSS feed of newly added games in the Xbox Game Pass catalog.

Once per UTC day (or when forced), this job:
  1. Fetches the product-ID list for one tier + platform + market from
     catalog.gamepass.com.
  2. Diffs it against the JSONL state committed under ``feeds/gamepass/state``.
  3. Fetches full store metadata only for product IDs never seen before.
  4. Lets the shared renderer publish one item per announced addition.

Design rules preserved from the original single-file generator:
  * At most one successful catalog fetch per UTC day; --force bypasses.
  * A failed fetch never touches the feed. Last-good state is kept.
  * The first ever run seeds the state silently; bootstrap can be replayed.
  * A product ID that merely replaces a recent listing of the same game
    (edition swap, re-published SKU) is NOT announced.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum, StrEnum
from html import escape as html_escape
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode

from ..files import write_text_atomic
from ..http import FetchError, HttpClient
from ..models import Deferred, FeedConfig, FeedItem, JobContext, Media

log = logging.getLogger("custom_rss.jobs.gamepass")

# ─── Microsoft endpoints and identifiers ──────────────────────────────────────
# Tier GUIDs and contexts as observed by github.com/nanhopper/catabox after the
# 2025 tier restructure. If Microsoft renames tiers again, these are what change.

SIGLS_ENDPOINT = "https://catalog.gamepass.com/sigls/v3"
DISPLAY_CATALOG_ENDPOINT = "https://displaycatalog.mp.microsoft.com/v7.0/products"
DISPLAY_CATALOG_MS_CV = "DGU1mcuYo0WMMp+F.1"   # Correlation vector the store client sends; harmless but expected

TIERS = {
    "ultimate":  {"sigl_id": "97c6c862-d28a-4907-a3d5-c401f2296a53", "subscription_context": "cfq7ttc0khs0"},
    "premium":   {"sigl_id": "09a72c0d-c466-426a-9580-b78955d8173a", "subscription_context": "cfq7ttc0p85b"},
    "essential": {"sigl_id": "34031711-5a70-4196-bab7-45757dc2294e", "subscription_context": "cfq7ttc0k5dj"},
}
PLATFORMS = {
    "pc": "pc",
    "console": "ConsoleGen8;ConsoleGen9",
}

# DisplayCatalog accepts many IDs per call; 50 keeps the URL well under proxy limits.
DISPLAY_CATALOG_BATCH_SIZE = 50
MAX_SCREENSHOTS = 6

PRODUCTS_FILE = "products.jsonl"
RUNS_FILE = "runs.jsonl"
CATALOG_FILE = "catalog.json"
RAW_DIR = "raw"

_JOB_KEYS = {
    "tier",
    "platform",
    "market",
    "bootstrap_announce",
    "max_fetches_per_day",
    "failed_retry_cooldown_hours",
    "max_shrink_ratio",
    "relisting_window_days",
}


@dataclass(frozen=True)
class GamePassSettings:
    tier: str
    platform: str
    market: str
    language: str
    bootstrap_announce: int
    max_fetches_per_day: int
    failed_retry_cooldown_hours: int
    max_shrink_ratio: float
    relisting_window_days: int


def _require_int(raw: dict[str, Any], key: str, default: int, minimum: int, where: Path) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{where}: setting {key!r} must be an integer >= {minimum}")
    return value


def load_settings(config: FeedConfig) -> GamePassSettings:
    where = config.directory / "feed.toml"
    raw: dict[str, Any] = dict(config.settings)

    unknown = sorted(set(raw) - _JOB_KEYS)
    if unknown:
        raise ValueError(f"{where}: unknown [job] setting(s): {', '.join(unknown)}")

    tier = raw.get("tier")
    if tier not in TIERS:
        raise ValueError(f"{where}: setting 'tier' must be one of {sorted(TIERS)}")
    platform = raw.get("platform")
    if platform not in PLATFORMS:
        raise ValueError(f"{where}: setting 'platform' must be one of {sorted(PLATFORMS)}")
    market = raw.get("market")
    if not isinstance(market, str) or len(market) != 2 or not market.isalpha() or not market.isupper():
        raise ValueError(f"{where}: setting 'market' must be an uppercase two-letter market code")

    shrink = raw.get("max_shrink_ratio", 0.10)
    if isinstance(shrink, bool) or not isinstance(shrink, (int, float)) or not 0 <= shrink < 1:
        raise ValueError(f"{where}: setting 'max_shrink_ratio' must be a number in [0, 1)")

    return GamePassSettings(
        tier=tier,
        platform=platform,
        market=market,
        language=config.language,
        bootstrap_announce=_require_int(raw, "bootstrap_announce", 100, 0, where),
        max_fetches_per_day=_require_int(raw, "max_fetches_per_day", 1, 1, where),
        failed_retry_cooldown_hours=_require_int(raw, "failed_retry_cooldown_hours", 6, 1, where),
        max_shrink_ratio=float(shrink),
        relisting_window_days=_require_int(raw, "relisting_window_days", 30, 1, where),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Catalog — talking to Microsoft
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class CatalogListing:
    """The raw product-ID list for one tier/platform/market, plus its source JSON."""
    product_ids: tuple[str, ...]
    raw_json: bytes
    source_title: str


def fetch_catalog_listing(settings: GamePassSettings, http: HttpClient) -> CatalogListing:
    """One request: which product IDs are currently in the configured Game Pass list."""
    tier = TIERS[settings.tier]
    params = {
        "id": tier["sigl_id"],
        "language": settings.language,
        "market": settings.market,
        "platformContext": PLATFORMS[settings.platform],
        "subscriptionContext": tier["subscription_context"],
    }
    url = f"{SIGLS_ENDPOINT}?{urlencode(params)}"
    log.info("Fetching catalog list: %s", url)
    payload = http.get_json(url)

    # Shape: [ {header...}, {"id": "9XXXXXXXXXXX"}, {"id": ...}, ... ]
    if not isinstance(payload, list) or not payload:
        raise FetchError("SIGLS response was not a non-empty JSON array")
    header, *entries = payload
    if not isinstance(header, dict):
        raise FetchError("SIGLS response header missing")
    if header.get("siglId") and header["siglId"].lower() != tier["sigl_id"]:
        # Same check catabox does: make sure Microsoft answered for the list we asked about.
        log.warning("SIGLS header siglId %s != requested %s", header.get("siglId"), tier["sigl_id"])

    ids = _unique_sorted(
        entry["id"] for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry["id"].strip()
    )
    return CatalogListing(
        product_ids=tuple(ids),
        raw_json=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        source_title=str(header.get("title") or ""),
    )


def fetch_products(
    product_ids: Iterable[str], settings: GamePassSettings, http: HttpClient
) -> dict[str, dict]:
    """Full store metadata for the given product IDs, batched. Returns {product_id: raw product}."""
    ids = _unique_sorted(product_ids)
    products: dict[str, dict] = {}
    for start in range(0, len(ids), DISPLAY_CATALOG_BATCH_SIZE):
        batch = ids[start:start + DISPLAY_CATALOG_BATCH_SIZE]
        params = {
            "bigIds": ",".join(batch),
            "market": settings.market,
            "languages": settings.language,
            "MS-CV": DISPLAY_CATALOG_MS_CV,
        }
        url = f"{DISPLAY_CATALOG_ENDPOINT}?{urlencode(params)}"
        log.info("Fetching metadata for %d products", len(batch))
        payload = http.get_json(url)
        for product in (payload or {}).get("Products") or []:
            product_id = str(product.get("ProductId") or "").upper()
            if product_id:
                products[product_id] = product
    missing = [pid for pid in ids if pid not in products]
    if missing:
        log.warning("DisplayCatalog returned no data for %d product(s): %s", len(missing), ", ".join(missing))
    return products


def _unique_sorted(values: Iterable[str]) -> list[str]:
    return sorted({value.strip().upper() for value in values if value and value.strip()})


# ═══════════════════════════════════════════════════════════════════════════════
# Metadata — turning a raw DisplayCatalog product into what we show in the feed
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class GameInfo:
    product_id: str
    title: str
    short_description: str = ""
    description: str = ""
    publisher: str = ""
    developer: str = ""
    genres: list[str] = field(default_factory=list)
    features: list[str] = field(default_factory=list)   # Raw capability attributes: XPA, 4K, HDR, ...
    player_modes: list[str] = field(default_factory=list)
    content_rating: str = ""
    release_date: str = ""                                # ISO date
    poster_url: str = ""
    hero_url: str = ""
    screenshot_urls: list[str] = field(default_factory=list)
    store_url: str = ""


def extract_game_info(settings: GamePassSettings, product_id: str, product: dict) -> GameInfo:
    localized = (product.get("LocalizedProperties") or [{}])[0]
    props = product.get("Properties") or {}
    images = localized.get("Images") or []

    title = _clean(localized.get("ProductTitle")) or product_id
    return GameInfo(
        product_id=product_id,
        title=title,
        short_description=_clean(localized.get("ShortDescription")),
        description=_clean(localized.get("ProductDescription")),
        publisher=_clean(localized.get("PublisherName")),
        developer=_clean(localized.get("DeveloperName")),
        genres=_genres(props),
        features=_feature_attributes(props),
        player_modes=_player_modes(product),
        content_rating=_content_rating(product),
        release_date=_release_date(product, settings),
        poster_url=_pick_image(images, ("Poster", "BoxArt", "BrandedKeyArt", "Tile")),
        hero_url=_pick_image(images, ("SuperHeroArt", "TitledHeroArt", "HeroArt", "BrandedKeyArt")),
        screenshot_urls=_screenshots(images),
        store_url=store_url(settings, product_id, title),
    )


def store_url(settings: GamePassSettings, product_id: str, title: str) -> str:
    slug = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", slug.lower()).strip("-") or "game"
    return f"https://www.xbox.com/{settings.language}/games/store/{slug}/{product_id}"


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", value).strip() if isinstance(value, str) else ""


def _genres(props: dict) -> list[str]:
    raw = list(props.get("Categories") or [])
    if props.get("Category"):
        raw.append(props["Category"])
    return sorted({_clean(genre) for genre in raw if _clean(genre)})


def _feature_attributes(props: dict) -> list[str]:
    names = [_clean(attribute.get("Name")) for attribute in props.get("Attributes") or [] if isinstance(attribute, dict)]
    return sorted({name for name in names if name})


_PLAYER_MODE_PATTERNS = (
    ("Single player",     re.compile(r"single.?player", re.I)),
    ("Online multiplayer", re.compile(r"online.?multi.?player|cross.?platform.?multi.?player", re.I)),
    ("Local multiplayer", re.compile(r"local.?multi.?player|shared.?split.?screen", re.I)),
    ("Online co-op",      re.compile(r"online.?co.?op|coop.?support.?online|cross.?platform.?coop", re.I)),
    ("Local co-op",       re.compile(r"local.?co.?op|couch.?co.?op|split.?screen.?co.?op", re.I)),
)


def _player_modes(product: dict) -> list[str]:
    """Derive human-readable modes from capability attributes, with player counts when given."""
    modes: dict[str, str] = {}
    for attribute in (product.get("Properties") or {}).get("Attributes") or []:
        name = _clean(attribute.get("Name")) if isinstance(attribute, dict) else ""
        for label, pattern in _PLAYER_MODE_PATTERNS:
            if label not in modes and pattern.search(name):
                lo, hi = attribute.get("Minimum"), attribute.get("Maximum")
                count = f" ({lo})" if lo and lo == hi else f" ({lo}-{hi})" if lo and hi else ""
                modes[label] = f"{label}{count}"
    return list(modes.values())


def _content_rating(product: dict) -> str:
    """Prefer PEGI (European markets); fall back to whatever rating system is listed first."""
    ratings: list[dict] = []
    for market_props in product.get("MarketProperties") or []:
        ratings.extend(market_props.get("ContentRatings") or [])
    ratings.extend((product.get("Properties") or {}).get("ContentRatings") or [])
    if not ratings:
        return ""
    rating = next((r for r in ratings if "PEGI" in str(r.get("RatingSystem", "")).upper()), ratings[0])
    system = _clean(rating.get("RatingSystem"))
    value = _clean(rating.get("RatingId") or rating.get("Rating") or rating.get("RatingValue"))
    value = re.sub(r"^[A-Za-z]+:", "", value)   # "PEGI:12" -> "12"
    return f"{system} {value}".strip()


def _release_date(product: dict, settings: GamePassSettings) -> str:
    candidates: list[Any] = []
    for market_props in product.get("MarketProperties") or []:
        if str(market_props.get("Market", "")).upper() == settings.market:
            candidates.append(market_props.get("OriginalReleaseDate"))
    for market_props in product.get("MarketProperties") or []:
        candidates.append(market_props.get("OriginalReleaseDate"))
    candidates.append((product.get("Properties") or {}).get("OriginalReleaseDate"))
    return _first_plausible_date(candidates)


def _first_plausible_date(candidates: Iterable[Any]) -> str:
    """First value that looks like a real ISO date (Microsoft uses 1601/1753 as 'unset')."""
    for value in candidates:
        if isinstance(value, str) and re.match(r"^(19[5-9]\d|20\d{2})-\d{2}-\d{2}", value):
            return value[:10]
    return ""


def infer_added_date(product: dict, settings: GamePassSettings) -> tuple[str, str]:
    """Best guess at when this product joined the configured Game Pass tier.

    Returns (iso_date, source). Sources, in order of trust:
      'subscription' — StartDate of the store availability that is fulfilled by
                       the tier's subscription product (the same BigId we send as
                       subscriptionContext). This is the real answer when present.
      'modified'     — the product's LastModifiedDate. Any store edit bumps it,
                       so it's a loose upper bound.
      'release'      — original release date. Wrong for old games added recently.
      ''             — nothing usable.
    """
    subscription_id = TIERS[settings.tier]["subscription_context"].upper()
    subscription_dates: list[str] = []
    for sku in product.get("DisplaySkuAvailabilities") or []:
        for availability in sku.get("Availabilities") or []:
            remediations = availability.get("Remediations") or []
            fulfilled_by_tier = any(
                isinstance(r, dict) and str(r.get("BigId", "")).upper() == subscription_id for r in remediations
            )
            if fulfilled_by_tier:
                subscription_dates.append(_first_plausible_date([(availability.get("Conditions") or {}).get("StartDate")]))
    subscription_dates = [d for d in subscription_dates if d]
    if subscription_dates:
        return max(subscription_dates), "subscription"       # Latest = most recent (re)join
    modified = _first_plausible_date([product.get("LastModifiedDate")])
    if modified:
        return modified, "modified"
    released = _release_date(product, settings)
    return (released, "release") if released else ("", "")


def _image_url(image: dict) -> str:
    uri = image.get("Uri") if isinstance(image, dict) else None
    if not isinstance(uri, str) or not uri:
        return ""
    if uri.startswith("//"):
        return "https:" + uri
    return uri.replace("http://", "https://", 1)


def _pick_image(images: list, purposes: tuple[str, ...]) -> str:
    """Largest image whose ImagePurpose matches, checking purposes in preference order."""
    for purpose in purposes:
        matches = [img for img in images if isinstance(img, dict) and img.get("ImagePurpose") == purpose and img.get("Uri")]
        if matches:
            best = max(matches, key=lambda img: (img.get("Width") or 0) * (img.get("Height") or 0))
            return _image_url(best)
    return ""


def _screenshots(images: list) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for image in images:
        if not isinstance(image, dict) or str(image.get("ImagePurpose", "")).lower() != "screenshot":
            continue
        url = _image_url(image)
        content_hash = image.get("UnscaledImageSHA256Hash")
        if not url or url in seen or (content_hash and content_hash in seen):
            continue
        seen.update(filter(None, (url, content_hash)))   # Same image can appear under two URLs or two hashes
        if "store-images.s-microsoft.com" in url:
            # Microsoft's image CDN resizes on request; ask for a modest thumbnail.
            url += ("&" if "?" in url else "?") + "w=640&h=360&q=80&format=jpg"
        urls.append(url)
        if len(urls) == MAX_SCREENSHOTS:
            break
    return urls


# ─── Title families: recognising the same game under a different product ID ──

_PLATFORM_QUALIFIERS = (
    re.compile(r"\s*[\[(]\s*(?:win(?:dows)?(?:\s+(?:10|11|pc))?|pc|xbox(?:\s+(?:one|series\s*x\s*\|?\s*s))?)(?:\s+version)?\s*[\])]\s*$", re.I),
    re.compile(r"\s*(?:[-:–—]\s*)?(?:for\s+)?windows(?:\s+(?:10|11))?(?:\s*(?:\+|and)\s*launcher)?(?:\s+version)?\s*$", re.I),
    re.compile(r"\s*(?:[-:–—]\s*)?(?:for\s+)?xbox(?:\s+(?:one|series\s*x\s*\|?\s*s))?(?:\s+version)?\s*$", re.I),
    re.compile(r"\s*(?:[-:–—]\s*)?pc(?:\s+version)?\s*$", re.I),
    re.compile(r"\s*(?:[-:–—]\s*)?(?:standard|deluxe|ultimate|gold|premium|digital|special|complete|definitive|anniversary|(?:game\s+of\s+the\s+year|goty)|(?:19|20)\d{2})\s+edition\s*$", re.I),
)
_ROMAN = {"ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8", "ix": "9", "x": "10"}


def family_key(title: str) -> str:
    """Normalise a title so 'Forza Horizon 5 – Windows', 'FORZA HORIZON V' and
    'Forza Horizon 5 Deluxe Edition' all collapse to 'forza horizon 5'.

    Ported from catabox's game-families logic. Deliberately does NOT strip years
    or numbers: 'FC 26' and 'FC 27' are different games.
    """
    text = _clean(title)
    previous = None
    while text != previous:                       # Strip qualifiers repeatedly: "X (PC) - Windows"
        previous = text
        for pattern in _PLATFORM_QUALIFIERS:
            text = pattern.sub("", text).strip()
    text = text or _clean(title)
    text = re.sub(r"[©®℠™]", "", text)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"([a-z])(\d)", r"\1 \2", text, flags=re.I)
    text = re.sub(r"(\d)([a-z])", r"\1 \2", text, flags=re.I)
    text = text.lower().replace("&", " and ")
    text = re.sub(r"['’`´]", "", text)
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    return " ".join(_ROMAN.get(token, token) for token in text.split())


# ═══════════════════════════════════════════════════════════════════════════════
# Storage — plain text files under the feed's state directory
# ═══════════════════════════════════════════════════════════════════════════════
#
# Text rather than SQLite because the state directory is committed to git by the
# daily GitHub Action. These files change by a handful of lines per run.
#
#   products.jsonl     one line per product ID, sorted — the diffable core state
#   runs.jsonl         append-only audit log, one line per run
#   catalog.json       latest raw SIGLS list response (git history is the audit trail)
#   raw/<ID>.json      raw DisplayCatalog product, written once, never modified
#
# products.jsonl fields: product_id, title, family_key, first_seen (ISO UTC),
# removed_at (ISO UTC or null; cleared if it returns), announced (bool),
# suppressed_reason (see Admission), info (GameInfo).
#
# There is deliberately no per-product "last_seen": updating it would rewrite
# every line every day. Presence is "removed_at is null"; see family_last_seen.
#
# Every state machine below is spelled out in README.md ("State machines") and
# printed live by `custom-rss status gamepass`.


class RunStatus(StrEnum):
    """``runs.jsonl`` status. A run the fetch gate skips records nothing."""

    OK = "ok"
    FAILED = "failed"


class Admission(Enum):
    """How a product ID entered the store, persisted as ``suppressed_reason``.

    Decided once, when the ID is first seen, and never changed. It fixes which
    values ``announced`` may hold:

      FRESH      null          announced is always true
      RELISTING  "relisting"   announced is always false (a product of the same
                               title family was listed within relisting_window_days)
      BOOTSTRAP  "bootstrap"   seeded silently on the first run; ``backfill``
                               sets announced for the newest N by added date

    ``announced=true`` with ``"bootstrap"`` is therefore valid: a seeded game
    that backfill chose to publish. Rows are validated against this on load.
    """

    FRESH = None
    RELISTING = "relisting"
    BOOTSTRAP = "bootstrap"


_ALLOWED_ANNOUNCED: dict[Admission, frozenset[bool]] = {
    Admission.FRESH: frozenset({True}),
    Admission.RELISTING: frozenset({False}),
    Admission.BOOTSTRAP: frozenset({True, False}),
}


def _validate_product(record: Any, where: str) -> None:
    if not isinstance(record, dict) or not isinstance(record.get("product_id"), str):
        raise ValueError(f"{where}: product row must be a JSON object with a string product_id")
    product_id = record["product_id"]
    reason = record.get("suppressed_reason")
    try:
        admission = Admission(reason)
    except ValueError:
        raise ValueError(
            f"{where}: product {product_id}: suppressed_reason {reason!r} must be null, "
            f"'relisting' or 'bootstrap'"
        ) from None
    announced = record.get("announced")
    if not isinstance(announced, bool) or announced not in _ALLOWED_ANNOUNCED[admission]:
        allowed = " or ".join(str(value).lower() for value in sorted(_ALLOWED_ANNOUNCED[admission]))
        raise ValueError(
            f"{where}: product {product_id}: announced must be {allowed} when "
            f"suppressed_reason is {reason!r}, got {announced!r}"
        )
    for key in ("first_seen", "removed_at"):
        value = record.get(key)
        if value is None and key == "removed_at":
            continue
        try:
            datetime.fromisoformat(value)
        except (TypeError, ValueError):
            raise ValueError(f"{where}: product {product_id}: {key} must be an ISO datetime, got {value!r}") from None


def _validate_run(run: Any, where: str) -> None:
    if not isinstance(run, dict):
        raise ValueError(f"{where}: run row must be a JSON object")
    if run.get("status") not in tuple(RunStatus):
        raise ValueError(f"{where}: run status {run.get('status')!r} must be one of {[s.value for s in RunStatus]}")
    try:
        datetime.fromisoformat(run.get("started_at"))
    except (TypeError, ValueError):
        raise ValueError(f"{where}: run started_at must be an ISO datetime, got {run.get('started_at')!r}") from None


class Store:
    """In-memory state loaded from the state directory; nothing touches disk until commit().

    Transaction states: LOADED (matches disk) -> DIRTY (after any mutation) ->
    commit() writes only the files whose content changed, or rollback() reloads.
    """

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self._load()

    def _load(self) -> None:
        self.products: dict[str, dict] = {}
        for where, record in _read_jsonl(self.data_dir / PRODUCTS_FILE):
            _validate_product(record, where)
            self.products[record["product_id"]] = record
        self.runs: list[dict] = []
        for where, run in _read_jsonl(self.data_dir / RUNS_FILE):
            _validate_run(run, where)
            self.runs.append(run)
        self._pending_raw: dict[str, dict] = {}
        self._pending_listing: bytes | None = None
        self._committed_products = _render_products(self.products)
        self._committed_runs = _render_runs(self.runs)

    # ── runs ──────────────────────────────────────────────────────────────────

    def last_run(self, status: RunStatus | None = None) -> dict | None:
        """Most recent run, optionally only of one status."""
        matching = [run for run in self.runs if status is None or run["status"] == status]
        return max(matching, key=lambda run: datetime.fromisoformat(run["started_at"])) if matching else None

    def successful_runs_on(self, utc_date: str) -> int:
        return sum(1 for run in self.runs if run["status"] == RunStatus.OK and _utc_date(run["started_at"]) == utc_date)

    def record_run(self, started_at: datetime, status: RunStatus, raw_listing: bytes | None = None,
                   **fields: Any) -> None:
        run = {"started_at": started_at.isoformat(), "status": status.value, "product_count": None, "added": None,
               "announced": None, "removed": None, "error": None}
        run.update(fields)
        self.runs.append(run)
        if raw_listing is not None:
            self._pending_listing = raw_listing

    # ── products ──────────────────────────────────────────────────────────────
    #
    # Per product, two independent axes:
    #   presence:      (new) --admit--> LISTED <--mark_removed / mark_present--> DELISTED
    #   announcement:  fixed by Admission at admit(); only BOOTSTRAP rows may
    #                  change it later, via set_bootstrap_announced (backfill).

    def known_product_ids(self) -> set[str]:
        return set(self.products)

    def is_empty(self) -> bool:
        return not self.products

    def family_last_seen(self, key: str, now: datetime) -> datetime | None:
        """When was a product of this title family last in the catalog?

        A member that is still listed counts as seen *now* (sync_catalog marks
        presence before deciding announcements). Otherwise the latest removed_at
        stands in for the old last_seen — at most one run later than the true
        last sighting, which is noise against relisting_window_days.
        """
        members = [record for record in self.products.values() if record["family_key"] == key]
        if not members:
            return None
        if any(record["removed_at"] is None for record in members):
            return now
        return max(datetime.fromisoformat(record["removed_at"]) for record in members)

    def admit(self, info: GameInfo, raw_product: dict | None, seen_at: datetime, admission: Admission) -> bool:
        """new -> LISTED. Returns whether the product is announced."""
        announced = admission is Admission.FRESH
        self.products[info.product_id] = {
            "product_id": info.product_id,
            "title": info.title,
            "family_key": family_key(info.title),
            "first_seen": seen_at.isoformat(),
            "removed_at": None,
            "announced": announced,
            "suppressed_reason": admission.value,
            "info": info.__dict__.copy(),
        }
        if raw_product:
            self._pending_raw[info.product_id] = raw_product
        return announced

    def mark_present(self, product_ids: Iterable[str]) -> int:
        """DELISTED -> LISTED for IDs that are back. Returns how many came back.

        A returning ID keeps its admission and announcement: it is never
        re-announced, and its GUID was already delivered to readers.
        """
        count = 0
        for product_id in product_ids:
            record = self.products[product_id]
            if record["removed_at"] is not None:
                record["removed_at"] = None
                count += 1
        return count

    def mark_removed(self, product_ids: Iterable[str], seen_at: datetime) -> int:
        """LISTED -> DELISTED. Returns how many were listed before."""
        count = 0
        for product_id in product_ids:
            record = self.products[product_id]
            if record["removed_at"] is None:
                record["removed_at"] = seen_at.isoformat()
                count += 1
        return count

    def announced_products(self) -> list[tuple[GameInfo, datetime]]:
        records = sorted((r for r in self.products.values() if r["announced"]), key=lambda r: r["title"])
        records.sort(key=lambda r: r["first_seen"], reverse=True)     # Stable: newest first, then by title
        return [(GameInfo(**r["info"]), datetime.fromisoformat(r["first_seen"])) for r in records]

    def bootstrap_products(self) -> list[tuple[str, str, dict | None]]:
        """(product_id, title, raw product or None) for every row seeded at bootstrap."""
        return [
            (record["product_id"], record["title"], self.raw_product(record["product_id"]))
            for record in self.products.values() if record["suppressed_reason"] == Admission.BOOTSTRAP.value
        ]

    def raw_product(self, product_id: str) -> dict | None:
        product_id = product_id.upper()
        if product_id in self._pending_raw:
            return self._pending_raw[product_id]
        path = self.data_dir / RAW_DIR / f"{product_id}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def _bootstrap_record(self, product_id: str) -> dict:
        record = self.products[product_id]
        if record["suppressed_reason"] != Admission.BOOTSTRAP.value:
            raise ValueError(f"product {product_id} was not seeded at bootstrap; backfill may not change it")
        return record

    def set_first_seen(self, product_id: str, first_seen: datetime) -> None:
        self._bootstrap_record(product_id)["first_seen"] = first_seen.isoformat()

    def set_bootstrap_announced(self, product_ids: Iterable[str], announced: bool) -> None:
        for product_id in product_ids:
            self._bootstrap_record(product_id)["announced"] = announced

    # ── persistence ───────────────────────────────────────────────────────────

    def commit(self) -> None:
        """Write only what changed, so a failed run touches runs.jsonl alone."""
        # Raw files first: products.jsonl must never reference a product whose
        # raw file failed to write.
        if self._pending_raw:
            raw_dir = self.data_dir / RAW_DIR
            raw_dir.mkdir(parents=True, exist_ok=True)
            for product_id, raw in self._pending_raw.items():
                write_text_atomic(raw_dir / f"{product_id}.json", _dumps(raw) + "\n")
        if self._pending_listing is not None:
            # Indented so each product ID sits on its own line: the git diff of
            # this file is then exactly the day's additions and removals.
            listing = json.loads(self._pending_listing)
            write_text_atomic(
                self.data_dir / CATALOG_FILE,
                json.dumps(listing, ensure_ascii=False, indent=1) + "\n",
            )
        products = _render_products(self.products)
        if products != self._committed_products:
            write_text_atomic(self.data_dir / PRODUCTS_FILE, products)
            self._committed_products = products
        runs = _render_runs(self.runs)
        if runs != self._committed_runs:
            write_text_atomic(self.data_dir / RUNS_FILE, runs)
            self._committed_runs = runs
        self._pending_raw.clear()
        self._pending_listing = None

    def rollback(self) -> None:
        """Discard everything since the last commit by reloading from disk."""
        self._load()


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _render_products(products: dict[str, dict]) -> str:
    return "".join(_dumps(products[pid]) + "\n" for pid in sorted(products))


def _render_runs(runs: list[dict]) -> str:
    return "".join(_dumps(run) + "\n" for run in runs)


def _read_jsonl(path: Path) -> list[tuple[str, Any]]:
    """(``path:line`` for error messages, parsed row) for every non-blank line."""
    if not path.exists():
        return []
    rows: list[tuple[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if line.strip():
                where = f"{path}:{number}"
                try:
                    rows.append((where, json.loads(line)))
                except json.JSONDecodeError as error:
                    raise ValueError(f"{where}: invalid JSON: {error}") from None
    return rows


def _utc_date(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone(timezone.utc).date().isoformat()


# ═══════════════════════════════════════════════════════════════════════════════
# Sync — the daily job
# ═══════════════════════════════════════════════════════════════════════════════


class Gate(StrEnum):
    """Whether this run may hit Microsoft, derived from runs.jsonl history."""

    OPEN = "open"                # Fetch.
    FORCED = "forced"            # --force: fetch regardless of history.
    DAILY_LIMIT = "daily-limit"  # max_fetches_per_day successful runs already this UTC day.
    COOLDOWN = "cooldown"        # Latest run failed less than failed_retry_cooldown_hours ago.


@dataclass(frozen=True)
class FetchGate:
    state: Gate
    reason: str

    @property
    def allows_fetch(self) -> bool:
        return self.state in (Gate.OPEN, Gate.FORCED)


def fetch_gate(store: Store, now: datetime, force: bool, settings: GamePassSettings) -> FetchGate:
    """The single decision point for the once-per-day rule and the post-failure cooldown."""
    if force:
        return FetchGate(Gate.FORCED, "--force bypasses the daily limit and failure cooldown")
    today = _utc_date(now.isoformat())
    done = store.successful_runs_on(today)
    limit = settings.max_fetches_per_day
    if done >= limit:
        return FetchGate(Gate.DAILY_LIMIT, f"{done} of {limit} successful fetch(es) already used on {today} UTC")
    latest = store.last_run()
    if latest and latest["status"] == RunStatus.FAILED:
        failed_at = datetime.fromisoformat(latest["started_at"]).astimezone(timezone.utc)
        retry_at = failed_at + timedelta(hours=settings.failed_retry_cooldown_hours)
        if now < retry_at:
            return FetchGate(
                Gate.COOLDOWN,
                f"last fetch failed at {failed_at:%Y-%m-%d %H:%M} UTC; next attempt after {retry_at:%Y-%m-%d %H:%M} UTC",
            )
    return FetchGate(Gate.OPEN, f"{done} of {limit} successful fetch(es) used on {today} UTC")


def validate_listing(listing: CatalogListing, store: Store, settings: GamePassSettings) -> None:
    """Refuse to believe an implausible response. Raises FetchError."""
    if not listing.product_ids:
        raise FetchError("catalog list came back empty")
    last_ok = store.last_run(RunStatus.OK)
    if last_ok and last_ok["product_count"]:
        floor = int(last_ok["product_count"] * (1 - settings.max_shrink_ratio))
        if len(listing.product_ids) < floor:
            raise FetchError(
                f"catalog shrank from {last_ok['product_count']} to {len(listing.product_ids)} products "
                f"(below {floor}); treating as an API glitch"
            )


@dataclass
class SyncResult:
    product_count: int
    added: int
    announced: int
    removed: int


def sync_catalog(
    store: Store, now: datetime, settings: GamePassSettings, http: HttpClient
) -> SyncResult:
    """Fetch the list, diff it, fetch metadata for newcomers, persist. Raises FetchError on failure."""
    listing = fetch_catalog_listing(settings, http)
    validate_listing(listing, store, settings)

    is_bootstrap = store.is_empty()     # Catalog phase: BOOTSTRAP (no rows yet) or STEADY.
    current = set(listing.product_ids)
    known = store.known_product_ids()
    new_ids = sorted(current - known)
    gone_ids = sorted(known - current)
    log.info("Catalog has %d products: %d new, %d no longer listed", len(current), len(new_ids), len(gone_ids))

    products = fetch_products(new_ids, settings, http) if new_ids else {}

    # Update presence first, so relisting detection below sees accurate presence
    # for everything *except* the newcomers we're about to insert.
    returned = store.mark_present(current & known)
    removed = store.mark_removed(gone_ids, now)
    if returned:
        log.info("%d product(s) back under a known ID; not re-announced", returned)

    announced_count = 0
    for product_id in new_ids:
        raw = products.get(product_id)
        if raw is None:
            # Listed in Game Pass but the store has no page for it. Announce anyway
            # with a bare title so it isn't silently lost; store a stub so we don't
            # re-request it every day.
            info = GameInfo(
                product_id=product_id,
                title=product_id,
                store_url=store_url(settings, product_id, product_id),
            )
        else:
            info = extract_game_info(settings, product_id, raw)

        admission = decide_admission(store, info, now, is_bootstrap, settings)
        announced = store.admit(info, raw, now, admission)
        announced_count += int(announced)
        log.info("%-12s %s%s", "NEW" if announced else "SUPPRESSED", info.title,
                 f"  [{admission.value}]" if admission.value else "")

    store.record_run(now, RunStatus.OK, product_count=len(current), added=len(new_ids), announced=announced_count,
                     removed=removed, raw_listing=listing.raw_json)
    if is_bootstrap and settings.bootstrap_announce > 0:
        announced_count = backfill_bootstrap(store, settings, settings.bootstrap_announce)
    store.commit()
    return SyncResult(len(current), len(new_ids), announced_count, removed)


def backfill_bootstrap(store: Store, settings: GamePassSettings, announce_newest: int) -> int:
    """Give bootstrap-seeded products a real 'added' date and announce the newest N.

    Idempotent: only touches rows admitted as BOOTSTRAP. Rows whose date can't
    be inferred keep their original first_seen and stay silent.
    """
    dated: list[tuple[datetime, str, str]] = []       # (added, product_id, title)
    sources: dict[str, int] = {}
    for product_id, title, raw in store.bootstrap_products():
        if raw is None:
            sources["no raw product"] = sources.get("no raw product", 0) + 1
            continue
        iso_date, source = infer_added_date(raw, settings)
        sources[source or "none"] = sources.get(source or "none", 0) + 1
        if not iso_date:
            continue
        added = datetime.fromisoformat(iso_date).replace(tzinfo=timezone.utc)
        store.set_first_seen(product_id, added)
        dated.append((added, product_id, title))

    dated.sort(reverse=True)
    chosen = dated[:announce_newest]
    store.set_bootstrap_announced([pid for _, pid, _ in dated], False)      # Reset, so re-running with a smaller N works
    store.set_bootstrap_announced([pid for _, pid, _ in chosen], True)

    log.info("Backfill: dated %d bootstrap products (sources: %s); announcing newest %d",
             len(dated), ", ".join(f"{k}={v}" for k, v in sorted(sources.items())), len(chosen))
    for added, _, title in chosen[:10]:
        log.info("  %s  %s", added.date(), title)
    if len(chosen) > 10:
        log.info("  ... and %d more", len(chosen) - 10)
    return len(chosen)


def decide_admission(
    store: Store, info: GameInfo, now: datetime, is_bootstrap: bool, settings: GamePassSettings
) -> Admission:
    """How does this newly seen product ID enter the store?

    * First run ever: BOOTSTRAP — we're just learning what's already there.
    * Same title family listed recently under another ID: RELISTING.
    * Otherwise: FRESH, which is announced.
    """
    if is_bootstrap:
        return Admission.BOOTSTRAP
    seen = store.family_last_seen(family_key(info.title), now)
    if seen and now - seen < timedelta(days=settings.relisting_window_days):
        return Admission.RELISTING
    return Admission.FRESH


# ═══════════════════════════════════════════════════════════════════════════════
# Feed items and job entry points
# ═══════════════════════════════════════════════════════════════════════════════


def render_description_html(info: GameInfo) -> str:
    """The body of the feed item: cover art, blurb, and every useful fact MS gave us."""
    h = html_escape
    parts: list[str] = []
    if info.poster_url:
        parts.append(f'<p><a href="{h(info.store_url)}"><img src="{h(info.poster_url)}" alt="{h(info.title)}" style="max-width:300px"></a></p>')
    if info.short_description:
        parts.append(f"<p><strong>{h(info.short_description)}</strong></p>")

    facts = [
        ("Developer", info.developer),
        ("Publisher", info.publisher),
        ("Genre", ", ".join(info.genres)),
        ("Players", ", ".join(info.player_modes)),
        ("Rating", info.content_rating),
        ("Released", info.release_date),
        ("Features", ", ".join(info.features)),
        ("Product ID", info.product_id),
    ]
    rows = "".join(f"<li><b>{h(label)}:</b> {h(value)}</li>" for label, value in facts if value)
    if rows:
        parts.append(f"<ul>{rows}</ul>")

    if info.description and info.description != info.short_description:
        paragraphs = [p.strip() for p in re.split(r"\n{2,}|\r\n\r\n", info.description) if p.strip()]
        parts.extend(f"<p>{h(p)}</p>" for p in (paragraphs or [info.description]))

    if info.screenshot_urls:
        shots = "".join(f'<a href="{h(u)}"><img src="{h(u)}" style="max-width:100%;margin:2px 0"></a>' for u in info.screenshot_urls)
        parts.append(f"<p>{shots}</p>")
    if info.hero_url:
        parts.append(f'<p><img src="{h(info.hero_url)}" style="max-width:100%"></p>')

    parts.append(f'<p><a href="{h(info.store_url)}">Open in the Xbox store</a></p>')
    return "\n".join(parts)


def announced_items(store: Store) -> tuple[FeedItem, ...]:
    items: list[FeedItem] = []
    for info, first_seen in store.announced_products():
        items.append(
            FeedItem(
                title=info.title,
                link=info.store_url,
                guid=info.product_id,
                guid_is_permalink=False,
                description=render_description_html(info),
                published=first_seen,
                categories=tuple(info.genres),
                thumbnail=Media(url=info.poster_url) if info.poster_url else None,
                media_content=tuple(Media(url=url, medium="image") for url in info.screenshot_urls),
            )
        )
    return tuple(items)


def build(context: JobContext) -> tuple[FeedItem, ...]:
    settings = load_settings(context.config)
    store = Store(context.config.state_dir)
    gate = fetch_gate(store, context.now, context.force, settings)
    if gate.state is Gate.COOLDOWN:
        raise Deferred(gate.reason)     # Not done today yet: the next hourly tick asks again.
    if not gate.allows_fetch:
        log.info("Not fetching (%s): %s", gate.state, gate.reason)
        return announced_items(store)
    log.info("Fetching (%s): %s", gate.state, gate.reason)

    try:
        result = sync_catalog(store, context.now, settings, context.http)
    except FetchError as error:
        # Nothing has been committed; last-good state is intact. Only the
        # failed audit row is written (commit() skips unchanged files).
        store.rollback()
        store.record_run(context.now, RunStatus.FAILED, error=str(error))
        store.commit()
        log.error("Fetch failed, feed left unchanged: %s", error)
        raise
    log.info(
        "Sync OK: %d in catalog, %d new (%d announced), %d removed",
        result.product_count,
        result.added,
        result.announced,
        result.removed,
    )
    return announced_items(store)


def status(context: JobContext) -> dict[str, Any]:
    """Read-only snapshot of every state machine; no network, no writes.

    ``products`` counts rows by ``presence/admission/announcement``, e.g.
    ``listed/fresh/announced`` or ``delisted/bootstrap/silent``.
    """
    settings = load_settings(context.config)
    store = Store(context.config.state_dir)
    gate = fetch_gate(store, context.now, context.force, settings)
    counts: Counter[str] = Counter()
    for record in store.products.values():
        presence = "listed" if record["removed_at"] is None else "delisted"
        admission = Admission(record["suppressed_reason"]).name.lower()
        announcement = "announced" if record["announced"] else "silent"
        counts[f"{presence}/{admission}/{announcement}"] += 1
    return {
        "phase": "bootstrap" if store.is_empty() else "steady",
        "fetch_gate": {"state": gate.state.value, "allows_fetch": gate.allows_fetch, "reason": gate.reason},
        "last_run": store.last_run(),
        "products": dict(sorted(counts.items())),
    }


def render_offline(context: JobContext) -> tuple[FeedItem, ...]:
    return announced_items(Store(context.config.state_dir))


def backfill(context: JobContext, count: int) -> tuple[FeedItem, ...]:
    settings = load_settings(context.config)
    store = Store(context.config.state_dir)
    backfill_bootstrap(store, settings, count)
    store.commit()
    return announced_items(store)


def dump(context: JobContext, product_id: str) -> dict | None:
    return Store(context.config.state_dir).raw_product(product_id)
