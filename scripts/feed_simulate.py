"""Simulated builders on a throwaway copy of the feed database: python scripts/feed_simulate.py --users 20 --days 6

Builders with hidden tastes go through calibration and daily feed pages; the script prints the save rate
and the mean hidden interest of what they were shown per day (against a random feed), then runs the
nightly training on what they did, the section 13 metrics, and the offline v1-vs-v2 comparison. Real users
and the real database are never touched.
"""

import argparse
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from signal_feed.config import load_feed_config  # noqa: E402
from signal_feed.db import FeedStore  # noqa: E402
from signal_feed.evaluation.metrics import weekly_metrics  # noqa: E402
from signal_feed.evaluation.replay import compare  # noqa: E402
from signal_feed.feed.data import load  # noqa: E402
from signal_feed.learning.nightly import run_nightly  # noqa: E402
from signal_feed.learning.sim import random_builders, simulate  # noqa: E402
from signal_feed.posts import PostsReader  # noqa: E402
from signal_feed.settings import FeedSettings  # noqa: E402

INDUSTRIES = ["Healthcare", "IT and developer tools", "Manufacturing", "Construction and trades",
              "Logistics and trucking", "Education", "Legal", "Agriculture", "Real estate", "Insurance"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--users", type=int, default=20)
    ap.add_argument("--days", type=int, default=6)
    ap.add_argument("--pages", type=int, default=2, help="pages per daily session")
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()
    settings = FeedSettings.from_env()
    if not settings.feed_database_url.startswith("sqlite:///"):
        raise SystemExit("this simulation copies a SQLite feed database; FEED_DATABASE_URL is not SQLite")
    tmp = Path(tempfile.mkdtemp())
    shutil.copy(settings.feed_database_url.removeprefix("sqlite:///"), tmp / "feed.db")
    config = load_feed_config(settings.config_file)
    config = config.model_copy(update={"ranking": config.ranking.model_copy(update={"models_dir": str(tmp / "models")})})
    store, posts = FeedStore(f"sqlite:///{tmp / 'feed.db'}"), PostsReader(settings.posts_database_url)
    builders = random_builders(args.users, INDUSTRIES, np.random.default_rng(args.seed))
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    stats = simulate(store, posts, config, builders, args.days, args.pages, start, args.seed)
    data = load(store, posts)
    paths = [data.path(it.niche_id) for it in data.items]
    random_interest = np.mean([np.mean([b.interest(p, it.post_type) for p, it in zip(paths, data.items)])
                               for b in builders])
    print(f"{args.users} builders x {args.days} days: {stats.shown} items shown, {stats.reads} reads, "
          f"{stats.saves} saves, {stats.ideas} ideas, {stats.not_for_me} not-for-me, {stats.surveys} survey answers")
    print(f"save rate by day:        {stats.per_session_save_rate}")
    print(f"mean interest by day:    {stats.per_session_mean_interest}   (random feed: {random_interest:.3f})")
    report = run_nightly(store, config, min_surveys=20, min_impressions=500)
    print(f"value model: {report.value}")
    print(f"v2 models:   {report.v2}")
    m = weekly_metrics(store, posts, start + timedelta(days=args.days), args.days)
    print(f"metrics: {m['product'] | m['feed_health']}")
    offline = compare(store, config)
    print("offline NDCG@20 (held-out users): " + "; ".join(
        f"{k} saves {offline[k]['save']['ndcg']} ideas {offline[k]['idea']['ndcg']}" for k in ("logged", "v1", "v2")
        if k in offline) + f" -> {offline.get('verdict')} {offline.get('reasons', '')}")
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
