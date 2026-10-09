"""Run one source live and report what it stored.

    python scripts/smoke.py rss --limit 20
    python scripts/smoke.py --list

Writes a log to logs/smoke-<source>-<timestamp>.log for the connector's pull request.
Exits 0 when the run stored or re-found at least --min valid records (default 20), or
fetched everything available without errors; 1 otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path

from .config import load_sources
from .connectors import get_connector_class
from .models import to_utc, utcnow
from .runner import run_source
from .services import Services
from .settings import REPO_ROOT, Settings

log = logging.getLogger("smoke")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="smoke.py", description="Run one connector live.")
    p.add_argument("source", nargs="?", help="source name from sources.yaml")
    p.add_argument("--limit", type=int, default=20, help="max records per query (default 20)")
    p.add_argument("--min", type=int, default=20, dest="minimum", help="records needed to pass (default 20)")
    p.add_argument("--query", action="append", help="override the configured keywords (repeatable)")
    p.add_argument("--since", type=datetime.fromisoformat, help="ISO date/time; default: initial lookback")
    p.add_argument("--dry-run", action="store_true", help="fetch and validate but do not store")
    p.add_argument("--sources", type=Path, help="path to sources.yaml")
    p.add_argument("--list", action="store_true", help="list sources and whether each can run")
    p.add_argument("--show", type=int, default=10, help="records to print (default 10)")
    return p.parse_args(argv)


def _setup_logging(source: str | None) -> Path | None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    log_path = None
    if source:
        log_dir = REPO_ROOT / "logs"
        log_dir.mkdir(exist_ok=True)
        log_path = log_dir / f"smoke-{source.replace(':', '_')}-{utcnow():%Y%m%dT%H%M%SZ}.log"
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=handlers, force=True)
    # httpx logs full request URLs at INFO, and some APIs take keys as query parameters.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return log_path


async def _main(args: argparse.Namespace, settings: Settings) -> int:
    sources = load_sources(args.sources or settings.sources_file)

    if args.list or not args.source:
        if not sources.sources:
            print("sources.yaml has no sources yet.")
        for name, s in sorted(sources.sources.items()):
            reason = s.blocked_reason(ignore_enabled=True)
            state = "runnable" if reason is None else f"cannot run: {reason}"
            print(f"{name:32} {s.route:12} {s.cadence:8} {'enabled ' if s.enabled else 'disabled'} {state}")
        return 0

    source = sources.get(args.source)
    reason = source.blocked_reason(ignore_enabled=True)
    if reason:
        log.error("%s cannot run: %s", source.name, reason)
        return 1
    if not source.enabled:
        log.warning("%s is disabled in sources.yaml; running anyway for the smoke test", source.name)
    if not settings.has_real_contact:
        log.warning("SIGNAL_CONTACT_EMAIL is not set; the User-Agent carries a placeholder contact")

    async with Services.open(settings, sources) as services:
        try:
            connector = get_connector_class(source.connector)(source, services)
            health = await connector.healthcheck()
            log.info("healthcheck: %s", health.model_dump_json())
        except Exception:
            log.exception("healthcheck failed")

        since = to_utc(args.since) if args.since else None
        out = await run_source(services, source, limit=args.limit, since=since, keywords=args.query,
                               save=not args.dry_run, collect=True, incremental=False)
        r = out.result

        print(f"\n{source.name}: status={r.status} fetched={r.fetched} new={r.new} "
              f"duplicates={r.duplicates} invalid={r.invalid} cost=${r.cost_usd:.4f}")
        for err in r.errors:
            print(f"  error: {err}")
        for post in out.posts[: args.show]:
            title = (post.title or post.text)[:70].replace("\n", " ")
            print(f"  {post.published_at:%Y-%m-%d %H:%M}  {post.id[:10]}  {title}")
            print(f"  {'':16}  {'':10}  {post.url}")

    enough = r.fetched >= args.minimum
    # Fewer than --min passes only if the source ran dry (returned fewer than --limit) without errors.
    exhausted = 0 < r.fetched < min(args.minimum, args.limit) and r.status == "ok"
    if exhausted:
        log.warning("only %d records available (fewer than %d); accepted because the run had no errors",
                    r.fetched, args.minimum)
    return 0 if (enough or exhausted) and r.status != "failed" else 1


def main(argv: list[str] | None = None, settings: Settings | None = None) -> int:
    args = _parse_args(argv)
    log_path = _setup_logging(None if args.list else args.source)
    code = asyncio.run(_main(args, settings or Settings.from_env()))
    if log_path:
        print(f"log: {log_path}")
    return code
