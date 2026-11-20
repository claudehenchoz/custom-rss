# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
uv sync --frozen                                   # install (Python 3.13, uv-managed)
uv run --frozen pytest                             # full suite; no test touches the network
uv run --frozen pytest tests/test_schedule.py::test_due_rule   # single test
uv run --frozen pytest -k cache                    # by keyword

uv run --frozen custom-rss schedule                # which feeds are due, read-only JSON
uv run --frozen custom-rss tick [--force]          # run every due feed (what CI runs hourly)
uv run --frozen custom-rss run <slug> [--force]    # run one feed now, ignoring its schedule (network)
uv run --frozen custom-rss status gamepass         # Game Pass fetch gate + product state counts, read-only
uv run --frozen custom-rss render gamepass         # rewrite Game Pass feed from state, no network
```

There is no linter or type checker configured. Exit codes: `0` ok, `1` runtime/fetch failure, `2` config/argument error (any `ValueError` or `FileNotFoundError` reaching `cli.main`).

## Architecture

Each feed is a slug with `feeds/<slug>/feed.toml` (config), a job function registered in `src/custom_rss/jobs/__init__.py` (`JOBS`), and committed output `feeds/<slug>/feed.xml` served by GitHub Pages. A job's only contract is `build(JobContext) -> tuple[FeedItem, ...]` (`models.py`); it never renders or writes the feed itself.

Pipeline in `cli.run_feed`: `config.load_config` (validates common keys, `[schedule]`, optional `[cache]`; the job validates its own `[job]` table in its `load_settings`) → job → optional cache merge (`cache.py`, `feeds/<slug>/cache.jsonl`) → `rss.render_feed` → atomic write (`files.py`) → `schedule.record_success`. A job raising `models.Deferred` means "not done yet": nothing is written or recorded, and the next tick retries.

Scheduling: one workflow (`.github/workflows/feeds.yml`, cron `17 * * * *`) runs `custom-rss tick`. Each feed's `[schedule]` (`every = hour|day|week`, `at = "HH:00"` UTC, `on = mon..sun`) is evaluated by the slot-based due rule in `schedule.py`: due when there is no recorded success since the current slot started. Daily/weekly feeds record that in `feeds/<slug>/last_run.json`; hourly feeds keep no record on purpose. The workflow then commits everything under `feeds/` in one commit (`Update feeds: <slugs>`), even if a feed failed.

Jobs:
- `jobs/zurich_finance.py`: fetches the configured RSS sources concurrently (`sources.fetch_feeds`, all-or-nothing), regex-filters, and dedupes by canonicalized link (which is also the guid). Has `[cache] max_items = 100` so articles outlive their sources.
- `jobs/gamepass.py`: diffs the Xbox catalog against JSONL state in `feeds/gamepass/state/`. It has explicit state machines (`Gate`, `RunStatus`, `Admission`, `Store` transactions), documented in README "State machines". `Store` validates every row on load and `commit()` writes only the files that changed. `suppressed_reason` stores the `Admission`; `announced=true` together with `"bootstrap"` is valid (backfill chose it). Game Pass has its own once-per-UTC-day limit and a 6 h cooldown after a failure (raises `Deferred` while cooling down), on top of the schedule.

## Invariants to preserve

- **Determinism:** identical inputs must render byte-identical output and state. Never emit wall-clock values (no `lastBuildDate`, no "fetched at" fields) into `feed.xml`, `cache.jsonl`, or state files: the hourly bot commits whatever changes, so any nondeterminism creates a commit every hour.
- GUIDs never change for an existing item (Game Pass: product ID; zurich-finance: canonical link).
- A failed fetch must leave the previous feed and state untouched (Game Pass only appends a `failed` row to `runs.jsonl`).
- Config errors are `ValueError`s that name the file and setting; unknown keys in `[job]`, `[schedule]`, and `[cache]` are rejected.

## Working in this repo

- `feeds/` is written by the GitHub Actions bot every hour. Run `git pull --rebase` before pushing, and don't commit `feed.xml`, `cache.jsonl`, or state files produced by local runs unless that is the point of the change.
- On this Windows checkout `core.autocrlf=true`, so after a local `render`/`run` git may show `feed.xml` as modified even when the content is identical. Check with `git show HEAD:feeds/<slug>/feed.xml | cmp - feeds/<slug>/feed.xml`, then `git checkout -- feeds/<slug>/feed.xml`. That command is also the standard check that a refactor didn't change output (`render gamepass` needs no network).
- "pages build and deployment" runs show as cancelled when two pushes land seconds apart; Pages keeps only the newer one. That is expected.
- Tests use fake HTTP objects and `monkeypatch.setattr(cli, "JOBS", {...})`. Each test module has its own `write_config` helper that writes a `feed.toml`; keep `[schedule]` (required) in them when adding config keys.
- Adding a feed: job module + `JOBS` entry + `feeds/<slug>/feed.toml` with `[schedule]` (and `[cache]` if items should persist). No workflow changes are needed.
