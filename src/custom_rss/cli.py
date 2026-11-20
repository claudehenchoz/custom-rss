"""One CLI for listing, running, rendering and inspecting registered feed jobs."""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from .config import load_config
from .files import write_bytes_atomic
from .http import USER_AGENT, HttpClient
from .jobs import JOBS, gamepass
from .models import Deferred, FeedItem, JobContext
from .rss import render_feed
from .schedule import is_due, next_slot, read_last_success, record_success

log = logging.getLogger("custom_rss")


def _context(
    root: Path,
    slug: str,
    *,
    force: bool = False,
    client: HttpClient | None = None,
    now: datetime | None = None,
) -> JobContext:
    config = load_config(slug, root)
    return JobContext(
        config=config,
        http=client if client is not None else HttpClient(USER_AGENT),
        now=now if now is not None else datetime.now(timezone.utc),
        force=force,
    )


def _write_feed(context: JobContext, items: Sequence[FeedItem]) -> None:
    payload = render_feed(context.config, items)
    write_bytes_atomic(context.config.output_path, payload)
    rendered = min(len(items), context.config.max_items)
    log.info("Wrote %d item(s) to %s", rendered, context.config.output_path)


def run_feed(
    root: Path,
    slug: str,
    *,
    force: bool = False,
    client: HttpClient | None = None,
    now: datetime | None = None,
) -> int:
    """Run one feed now, ignoring its schedule, and record the success."""
    context = _context(root, slug, force=force, client=client, now=now)
    try:
        items = JOBS[slug](context)
    except Deferred as why:
        log.info("%s deferred: %s", slug, why)
        return 0
    _write_feed(context, items)
    record_success(context.config.directory, context.config.schedule, context.now)
    return 0


def _schedule_entry(root: Path, slug: str, now: datetime) -> dict:
    config = load_config(slug, root)
    last = read_last_success(config.directory)
    due = is_due(config.schedule, last, now)
    return {
        "feed": slug,
        "schedule": config.schedule.describe(),
        "last_success": last.isoformat() if last else None,
        "due": due,
        "next_run": "next tick" if due else next_slot(config.schedule, now).isoformat(),
    }


def tick(
    root: Path,
    *,
    force: bool = False,
    client: HttpClient | None = None,
    now: datetime | None = None,
) -> int:
    """Run every due feed (every feed with ``force``); one failure never blocks the others."""
    now = now if now is not None else datetime.now(timezone.utc)
    failed: list[str] = []
    for slug in JOBS:
        try:
            entry = _schedule_entry(root, slug, now)
            if not force and not entry["due"]:
                log.info("%s: not due (%s); next run after %s", slug, entry["schedule"], entry["next_run"])
                continue
            log.info("%s: %s (%s, last success %s)", slug, "forced" if force else "due",
                     entry["schedule"], entry["last_success"] or "never recorded")
            run_feed(root, slug, force=force, client=client, now=now)
        except Exception as error:  # noqa: BLE001 - isolate feeds from each other
            log.error("%s failed: %s", slug, error)
            failed.append(slug)
    if failed:
        log.error("Failed feed(s): %s", ", ".join(failed))
        return 1
    return 0


def schedule_report(root: Path, *, now: datetime | None = None) -> int:
    now = now if now is not None else datetime.now(timezone.utc)
    print(json.dumps([_schedule_entry(root, slug, now) for slug in JOBS], indent=2))
    return 0


def render_gamepass(
    root: Path, *, client: HttpClient | None = None, now: datetime | None = None
) -> int:
    context = _context(root, "gamepass", client=client, now=now)
    _write_feed(context, gamepass.render_offline(context))
    return 0


def backfill_gamepass(
    root: Path,
    count: int | None,
    *,
    client: HttpClient | None = None,
    now: datetime | None = None,
) -> int:
    context = _context(root, "gamepass", client=client, now=now)
    if count is None:
        count = gamepass.load_settings(context.config).bootstrap_announce
    if count < 0:
        raise ValueError("backfill count must be >= 0")
    _write_feed(context, gamepass.backfill(context, count))
    return 0


def dump_gamepass(
    root: Path,
    product_id: str,
    *,
    client: HttpClient | None = None,
    now: datetime | None = None,
) -> int:
    context = _context(root, "gamepass", client=client, now=now)
    product = gamepass.dump(context, product_id)
    if product is None:
        log.error("No stored product %s", product_id)
        return 1
    print(json.dumps(product, indent=2, ensure_ascii=False))
    return 0


def status_gamepass(
    root: Path,
    *,
    client: HttpClient | None = None,
    now: datetime | None = None,
) -> int:
    context = _context(root, "gamepass", client=client, now=now)
    print(json.dumps(gamepass.status(context), indent=2, ensure_ascii=False))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="custom-rss", description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."), help="repository root (default: .)")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list", help="list registered feed slugs")

    run = subparsers.add_parser("run", help="run one feed job and write its feed")
    run.add_argument("feed", choices=sorted(JOBS))
    run.add_argument("--force", action="store_true", help="bypass the feed's own rate limit")

    tick_parser = subparsers.add_parser(
        "tick", help="run every feed whose [schedule] says it is due (the hourly workflow)"
    )
    tick_parser.add_argument(
        "--force", action="store_true", help="run every feed and bypass their own rate limits"
    )

    subparsers.add_parser(
        "schedule", help="print each feed's schedule, last success and next run as JSON; no network"
    )

    render = subparsers.add_parser("render", help="rewrite a feed from stored state; no network")
    render.add_argument("feed", choices=["gamepass"])

    backfill = subparsers.add_parser("backfill", help="re-announce bootstrap-seeded Game Pass games")
    backfill.add_argument("feed", choices=["gamepass"])
    backfill.add_argument("count", nargs="?", type=int, default=None, metavar="N")

    dump = subparsers.add_parser("dump", help="print stored raw store JSON for one product")
    dump.add_argument("feed", choices=["gamepass"])
    dump.add_argument("product_id", metavar="PRODUCT_ID")

    status = subparsers.add_parser(
        "status", help="print the fetch gate and product state counts as JSON; no network"
    )
    status.add_argument("feed", choices=["gamepass"])

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = Path(args.root)

    try:
        if args.command == "list":
            for slug in JOBS:
                print(slug)
            return 0
        if args.command == "run":
            return run_feed(root, args.feed, force=args.force)
        if args.command == "tick":
            return tick(root, force=args.force)
        if args.command == "schedule":
            return schedule_report(root)
        if args.command == "render":
            return render_gamepass(root)
        if args.command == "backfill":
            return backfill_gamepass(root, args.count)
        if args.command == "dump":
            return dump_gamepass(root, args.product_id)
        if args.command == "status":
            return status_gamepass(root)
    except (ValueError, FileNotFoundError) as error:
        log.error("%s", error)
        return 2
    except Exception as error:  # noqa: BLE001 - CLI boundary: report and exit non-zero
        log.error("%s", error)
        return 1
    return 2
