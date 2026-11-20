# custom-rss

Deterministic RSS 2.0 feeds generated from publisher feeds and store catalogs.
Each feed has its own job module, configuration and schedule, generated XML, and
job-owned state; shared code owns retrieval, the feed model, serialization, atomic
writes, and the CLI.

## Feeds

| Feed | Subscribe | Schedule |
| --- | --- | --- |
| Zurich financial services news | <https://claudehenchoz.github.io/custom-rss/feeds/zurich-finance/feed.xml> | Every hour |
| New on Game Pass Ultimate (PC, CH) | <https://claudehenchoz.github.io/custom-rss/feeds/gamepass/feed.xml> | Every day from `03:00` UTC |

GitHub Pages serves the committed `feeds/<slug>/feed.xml` files. Raw
`raw.githubusercontent.com` URLs return `text/plain` and are only fallback
debugging URLs, not canonical subscriptions.

## Commands

```bash
uv sync --frozen
uv run --frozen pytest

uv run --frozen custom-rss list
uv run --frozen custom-rss schedule
uv run --frozen custom-rss tick [--force]
uv run --frozen custom-rss run zurich-finance
uv run --frozen custom-rss run gamepass [--force]
uv run --frozen custom-rss render gamepass
uv run --frozen custom-rss backfill gamepass [N]
uv run --frozen custom-rss dump gamepass PRODUCT_ID
uv run --frozen custom-rss status gamepass
```

`--root` defaults to the current directory and must contain `feeds/<slug>/feed.toml`.
`run` runs the selected feed now, ignoring its schedule, and writes only that feed.
`tick` runs every feed that is due (see [Scheduling](#scheduling)); a failing feed
never blocks the others, and `tick` exits `1` if any feed failed. `schedule` prints
each feed's schedule, last success, and next run as JSON without touching anything.
`render`, `backfill`, `dump`, and `status` are
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
| `src/custom_rss/schedule.py` | `[schedule]` parsing and the due rule. |
| `src/custom_rss/jobs/` | One module per feed plus the explicit `JOBS` registry. |
| `feeds/<slug>/feed.toml` | Feed metadata, `[schedule]`, and job-specific `[job]` settings. |
| `feeds/<slug>/last_run.json` | Start time of the last successful run (daily and weekly feeds only). |
| `feeds/<slug>/feed.xml` | Generated output, committed by the scheduled workflow. |
| `feeds/gamepass/state/` | Game Pass catalog diff state, run audit log, raw products. |
| `.github/workflows/feeds.yml` | The single hourly tick plus commit/push protocol. |

## Adding a feed

1. Add `src/custom_rss/jobs/<name>.py` exposing `build(context) -> tuple[FeedItem, ...]`
   and validate its own `[job]` settings in a `load_settings` function.
2. Add it to `JOBS` in `src/custom_rss/jobs/__init__.py`.
3. Add `feeds/<slug>/feed.toml` with a `[schedule]` table.
4. Add focused job tests; no workflow, shared runner, or renderer edit is needed
   unless the feed requires a new RSS field.

## Scheduling

`.github/workflows/feeds.yml` ticks hourly at `:17` UTC and runs `custom-rss tick`.
Each feed decides for itself, from the required `[schedule]` table in its
`feed.toml`, whether it is due:

```toml
[schedule]
every = "hour"            # due on every tick

[schedule]
every = "day"
at = "03:00"              # whole UTC hour

[schedule]
every = "week"
on = "mon"                # mon tue wed thu fri sat sun
at = "03:00"
```

Each cadence divides UTC time into slots (every hour, every day at `at`, every
week `on` a day `at`). A feed is due when it has no recorded success since the
current slot started. So a late tick still runs it, a dropped tick is caught up
once on the next tick, and a failed run is retried on the next tick. Daily and
weekly feeds record their last success in `feeds/<slug>/last_run.json`; hourly
feeds keep no record, so an unchanged hourly feed never creates a commit.

A job that isn't done yet can raise `Deferred(reason)`: nothing is written, no
success is recorded, and the next tick asks again. Game Pass does this while its
fetch gate is in `cooldown` after a failure.

Manual runs: dispatch the **Feeds** workflow with `feed` set to run one feed now,
and `force` to also bypass that feed's own rate limit.

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
- Each tick makes at most one commit, containing every changed `feeds/<slug>/`;
  pushes rebase up to three times.
