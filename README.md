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
uv run --frozen custom-rss status gamepass
```

`--root` defaults to the current directory and must contain `feeds/<slug>/feed.toml`.
`run` writes only the selected feed. `render`, `backfill`, `dump`, and `status` are
Game Pass offline operations; they never touch the network. `status` writes nothing
and prints the fetch gate, last run, and product state counts as JSON. Use it to
answer "will the next run fetch, and why?" without reading the state files.

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

## State machines

All Game Pass state is derived from the files in `feeds/gamepass/state/`. Each
machine has one named type and one place in `jobs/gamepass.py` that moves it.

**Fetch gate** (`Gate`, decided only by `fetch_gate()` from `runs.jsonl`):

| State | When | Fetches |
| --- | --- | --- |
| `forced` | `--force` | yes |
| `daily-limit` | `max_fetches_per_day` `ok` runs already on this UTC day | no |
| `cooldown` | latest run is `failed` and `failed_retry_cooldown_hours` haven't passed | no |
| `open` | otherwise | yes |

**Run** (`RunStatus`, one `runs.jsonl` row): `ok` or `failed`. A gated (skipped)
run records nothing. A failed run appends its row and rewrites no other file.

**Catalog phase**: `bootstrap` while `products.jsonl` is empty (the first fetch
seeds silently, then backfill announces the newest `bootstrap_announce`), then
`steady`.

**Product**: two independent axes per `products.jsonl` row.

```text
presence:   (new) --admit--> listed  --mark_removed-->  delisted
                               ^-------mark_present-------'   (same ID back; never re-announced)

admission (suppressed_reason, fixed at admit) -> allowed `announced`
  fresh      null          true
  relisting  "relisting"   false   (same title family listed within relisting_window_days)
  bootstrap  "bootstrap"   false, or true once `backfill` picks it
```

`Store` rejects impossible combinations on load with the file and line number
(exit code `2`). Only `bootstrap` rows may have `announced` changed after they
are admitted.

**Store transaction**: loaded → mutated in memory → `commit()` writes only the
files whose content changed, or `rollback()` reloads from disk.

## Guarantees

- Identical inputs render byte-identically; no `lastBuildDate` or other wall-clock
  values are emitted, so unchanged feeds produce no content-free commits.
- GUIDs are stable and never derived from this repository's URL.
- Feed writes and state writes are atomic (sibling `.tmp` then `os.replace`).
- A failed fetch leaves the last-good feed and state untouched. Game Pass records
  one `failed` audit row so the failure is visible in history.
- Workflows commit only `feeds/<slug>/`; cross-feed pushes rebase up to three times.
