"""Offline comparison (FEED_ALGORITHM.md section 13): replay held-out users' sessions and compare the
logged order, v1 and the latest v2 candidate on NDCG@20 for saves and ideas.

    python scripts/feed_evaluate.py              # report only
    python scripts/feed_evaluate.py --promote    # activate the candidate if it passes
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from signal_feed.config import load_feed_config  # noqa: E402
from signal_feed.db import FeedStore  # noqa: E402
from signal_feed.evaluation.replay import compare, promote_if_better  # noqa: E402
from signal_feed.settings import FeedSettings  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--promote", action="store_true")
    ap.add_argument("--candidate", type=int, help="v2 model id (default: the latest candidate)")
    args = ap.parse_args()
    settings = FeedSettings.from_env()
    store, config = FeedStore(settings.feed_database_url), load_feed_config(settings.config_file)
    report = (promote_if_better if args.promote else compare)(store, config, args.candidate)
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
