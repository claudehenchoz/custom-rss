# custom-rss

Deterministic RSS 2.0 feeds generated from publisher feeds and store catalogs.
Each feed has its own job module, configuration, schedule, generated XML, and
job-owned state; shared code owns retrieval, the feed model, serialization, atomic
writes, and the CLI.

## Feeds

| Feed | Subscribe | Schedule |
| --- | --- | --- |
| Zurich financial services news | <https://claudehenchoz.github.io/custom-rss/feeds/zurich-finance/feed.xml> | Hourly at `:17` UTC |
| New on Game Pass Ultimate (PC, CH) | <https://claudehenchoz.github.io/custom-rss/feeds/gamepass/feed.xml> | Daily at `03:00` UTC |

GitHub Pages serves the committed `feeds/<slug>/feed.xml` files. Raw
`raw.githubusercontent.com` URLs return `text/plain` and are only fallback
debugging URLs, not canonical subscriptions.

## Commands

```bash
uv sync --frozen
uv run --frozen pytest

uv run --frozen custom-rss list
uv run --frozen custom-rss run zurich-finance
uv run --frozen custom-rss run gamepass [--force]
uv run --frozen custom-rss render gamepass
uv run --frozen custom-rss backfill gamepass [N]
uv run --frozen custom-rss dump gamepass PRODUCT_ID
```

`--root` defaults to the current directory and must contain `feeds/<slug>/feed.toml`.
`run` writes only the selected feed. `render`, `backfill`, and `dump` are Game Pass
offline operations; they never touch the network.

Exit codes: `0` success, `1` fetch/runtime failure or missing dump ID, `2`
configuration or argument error.

## Layout

| Path | Purpose |
| --- | --- |
| `src/custom_rss/models.py` | `FeedConfig`, `FeedItem`, `JobContext`: the stable job boundary. |
| `src/custom_rss/config.py` | Common `feed.toml` validation before any I/O. |
| `src/custom_rss/http.py` | Retrying, rate-limit-aware `HttpClient`. |
| `src/custom_rss/sources.py` | Concurrent RSS/Atom retrieval, all-or-nothing. |
| `src/custom_rss/rss.py` | Deterministic RSS 2.0 serialization with Atom self link and Media RSS. |
| `src/custom_rss/jobs/` | One module per feed plus the explicit `JOBS` registry. |
| `feeds/<slug>/feed.toml` | Feed metadata and job-specific `[job]` settings. |
| `feeds/<slug>/feed.xml` | Generated output, committed by the scheduled workflow. |
| `feeds/gamepass/state/` | Game Pass catalog diff state, run audit log, raw products. |
| `.github/workflows/run-feed.yml` | Reusable selected-feed runner and commit/push protocol. |

## Adding a feed

1. Add `src/custom_rss/jobs/<name>.py` exposing `build(context) -> tuple[FeedItem, ...]`
   and validate its own `[job]` settings in a `load_settings` function.
2. Add it to `JOBS` in `src/custom_rss/jobs/__init__.py`.
3. Add `feeds/<slug>/feed.toml`.
4. Add a thin caller workflow with its cron schedule and `force` dispatch input.
5. Add focused job tests; no shared runner or renderer edit is needed unless the
   feed requires a new RSS field.

## Guarantees

- Identical inputs render byte-identically; no `lastBuildDate` or other wall-clock
  values are emitted, so unchanged feeds produce no content-free commits.
- GUIDs are stable and never derived from this repository's URL.
- Feed writes and state writes are atomic (sibling `.tmp` then `os.replace`).
- A failed fetch leaves the last-good feed and state untouched. Game Pass records
  one `failed` audit row so the failure is visible in history.
- Workflows commit only `feeds/<slug>/`; cross-feed pushes rebase up to three times.
