"""Scheduled feed jobs (signal_feed/jobs.py): tagging, embeddings, problem scores, learning, weekly builds.

    python scripts/feed_jobs.py --once [--paid] [--only tag,embed]   # from cron, every 15 minutes
    python scripts/feed_jobs.py --dry-run                            # what is due now
    python scripts/feed_jobs.py --status                             # each job's last run

Crontab (free jobs only; add --paid to let it tag, embed, build and run detectors within jobs.max_daily_usd):

    */15 * * * * cd /path/to/signal-connectors && .venv/bin/python scripts/feed_jobs.py --once >> data/feed_jobs.log 2>&1
"""

import argparse
import fcntl
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from signal_connectors.models import utcnow  # noqa: E402
from signal_feed.config import load_feed_config  # noqa: E402
from signal_feed.db import FeedStore  # noqa: E402
from signal_feed.jobs import due, jobs, last_runs, tick  # noqa: E402
from signal_feed.settings import REPO_ROOT, FeedSettings  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--status", action="store_true")
    ap.add_argument("--paid", action="store_true", help="also run jobs that call Claude or Voyage")
    ap.add_argument("--only", help="comma-separated job names")
    args = ap.parse_args()
    settings = FeedSettings.from_env()
    store, config = FeedStore(settings.feed_database_url), load_feed_config(settings.config_file)
    only = set(args.only.split(",")) if args.only else None
    known = {j.name for j in jobs(config)}
    if only and only - known:
        raise SystemExit(f"unknown job(s): {', '.join(sorted(only - known))}; jobs: {', '.join(sorted(known))}")

    last, now = last_runs(store), utcnow()
    if args.status or args.dry_run:
        for j in jobs(config):
            r = last.get(j.name)
            when = f"{r.started_at:%Y-%m-%d %H:%M} {r.status}" if r else "never run"
            flag = "due" if due(j, r, now) else ""
            print(f"{j.name:<9} every {j.every.days * 24 + j.every.seconds // 3600:>3}h  {'paid' if j.paid else 'free'}"
                  f"  {when:<24} {flag}")
            if args.status and r and r.status == "failed" and r.output:
                print("          " + r.output.strip().splitlines()[-1][:160])
        return 0

    lock = open(REPO_ROOT / "data" / "feed_jobs.lock", "w")      # cron fires every 15 minutes; runs can be longer
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another tick is still running; skipping")
        return 0
    outcomes = tick(store, config, paid=args.paid, only=only)
    for o in outcomes:
        print(f"{utcnow():%Y-%m-%d %H:%M} {o.job}: {o.status}{' - ' + o.detail if o.detail else ''}")
    return 1 if any(o.status == "failed" for o in outcomes) else 0


if __name__ == "__main__":
    sys.exit(main())
