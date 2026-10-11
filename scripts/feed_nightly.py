"""Nightly learning job: python scripts/feed_nightly.py

Retrains the "Was this useful?" value model (needs ranking.value_model_min_surveys answers) and the v2
ranking models (needs ranking.v2_min_labeled_impressions). A new value model is used at once; a v2 model
is registered as a candidate and used only after it beats v1 in the offline comparison (FEED_ALGORITHM.md
section 13).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from signal_feed.config import load_feed_config  # noqa: E402
from signal_feed.db import FeedStore  # noqa: E402
from signal_feed.learning.nightly import run_nightly  # noqa: E402
from signal_feed.settings import FeedSettings  # noqa: E402


def main() -> int:
    settings = FeedSettings.from_env()
    report = run_nightly(FeedStore(settings.feed_database_url), load_feed_config(settings.config_file))
    print(f"value model: {report.value}")
    print(f"v2 models:   {report.v2}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
