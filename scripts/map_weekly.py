"""Weekly map job for every user: python scripts/map_weekly.py

Follows niche-tree changes, narrows, discovers, fades, re-opens removed niches the user allowed to be
re-tested, and writes each user's weekly map snapshot (FEED_ALGORITHM.md 6.5-6.6). Run it after the
weekly tree build (scripts/problems.py build).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from signal_feed.config import load_feed_config  # noqa: E402
from signal_feed.db import FeedStore  # noqa: E402
from signal_feed.feed.weekly import run_weekly  # noqa: E402
from signal_feed.settings import FeedSettings  # noqa: E402


def main() -> int:
    settings = FeedSettings.from_env()
    stats = run_weekly(FeedStore(settings.feed_database_url), load_feed_config(settings.config_file))
    print(f"{stats.users} users, {stats.snapshots} snapshots, {stats.migrated} rows moved off changed niches, "
          f"{stats.reopened} removed niches re-opened, {stats.profile_updates} profile suggestions")
    for label, items in (("promoted", stats.promoted), ("discovered", stats.discovered),
                         ("fading", stats.fading), ("left the map", stats.dropped)):
        for item in items:
            print(f"  {label}: {item}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
