"""Feed metrics dashboard (FEED_ALGORITHM.md section 13), as text or JSON.

    python scripts/feed_metrics.py                      # the last 7 days
    python scripts/feed_metrics.py --days 30 --json
    python scripts/feed_metrics.py --experiment explore-15   # A/B report with the ship verdict per arm
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from signal_feed.config import load_feed_config  # noqa: E402
from signal_feed.db import FeedStore  # noqa: E402
from signal_feed.evaluation.experiments import ab_report  # noqa: E402
from signal_feed.evaluation.metrics import weekly_metrics  # noqa: E402
from signal_feed.posts import PostsReader  # noqa: E402
from signal_feed.settings import FeedSettings  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--experiment")
    args = ap.parse_args()
    settings = FeedSettings.from_env()
    store, posts = FeedStore(settings.feed_database_url), PostsReader(settings.posts_database_url)
    now = datetime.now(timezone.utc)
    if args.experiment:
        out = ab_report(store, posts, load_feed_config(settings.config_file), args.experiment, now, args.days)
    else:
        out = weekly_metrics(store, posts, now, args.days)
    if args.json:
        print(json.dumps(out, indent=2, default=str))
        return 0
    for section, values in out.items():
        print(f"\n{section}")
        for name, value in (values.items() if isinstance(values, dict) else [("", values)]):
            print(f"  {name:30} {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
