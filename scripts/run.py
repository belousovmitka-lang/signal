"""Run due connectors on schedule.

    python scripts/run.py --once            # run everything due, then exit (use from cron)
    python scripts/run.py --loop            # check every 5 minutes until stopped
    python scripts/run.py --dry-run         # show what is due and why others are skipped
    python scripts/run.py --once --only hackernews,rss

Logs go to logs/run-<date>.log.
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from signal_connectors.models import utcnow  # noqa: E402
from signal_connectors.scheduler import plan, run_due  # noqa: E402
from signal_connectors.services import Services  # noqa: E402
from signal_connectors.settings import REPO_ROOT  # noqa: E402

LOOP_SECONDS = 300


def setup_logging() -> None:
    log_dir = REPO_ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", force=True,
                        handlers=[logging.StreamHandler(sys.stderr),
                                  logging.FileHandler(log_dir / f"run-{utcnow():%Y-%m-%d}.log", encoding="utf-8")])
    logging.getLogger("httpx").setLevel(logging.WARNING)   # request URLs can carry keys


def report(results, checks) -> None:
    for r in sorted(results, key=lambda r: r.source):
        print(f"  {r.source:20} {r.status:8} new={r.new:<5} dup={r.duplicates:<5} invalid={r.invalid:<3} "
              f"${r.cost_usd:.4f}" + (f"  {r.errors[0][:80]}" if r.errors else ""))
    for c in checks:
        print(f"  {c.source:20} {c.status:8} purged={c.duplicates}" + (f"  {c.errors[0][:80]}" if c.errors else ""))
    if results:
        print(f"  total new: {sum(r.new for r in results)}, failed: {sum(r.status == 'failed' for r in results)}")


async def main(args: argparse.Namespace) -> int:
    only = set(args.only.split(",")) if args.only else None
    async with Services.open() as services:
        if args.dry_run:
            p = plan(services, only=only)
            print("due:", ", ".join(s.name for s in p.runs) or "nothing")
            print("re-checks due:", ", ".join(s.name for s in p.rechecks) or "none")
            for name, why in sorted(p.skipped.items()):
                print(f"  skip {name:20} {why}")
            return 0
        while True:
            results, checks, _ = await run_due(services, only=only, concurrency=args.concurrency)
            print(f"{utcnow():%Y-%m-%d %H:%M} ran {len(results)} sources, {len(checks)} re-checks")
            report(results, checks)
            if not args.loop:
                return 1 if any(r.status == "failed" for r in results) else 0
            await asyncio.sleep(LOOP_SECONDS)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--loop", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    p.add_argument("--only", help="comma-separated source names")
    p.add_argument("--concurrency", type=int, default=6)
    args = p.parse_args()
    setup_logging()
    sys.exit(asyncio.run(main(args)))
