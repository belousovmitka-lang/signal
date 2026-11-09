"""Assess problems (signal_feed/opportunities/assess.py): ratings, who to reach, how to validate.

    python scripts/assess_problems.py --dry-run          # how many need it and the worst-case cost
    python scripts/assess_problems.py --limit 3          # a sample
    python scripts/assess_problems.py                    # every new or changed problem (Sonnet, sync)
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from signal_feed.builder import ProblemBank  # noqa: E402
from signal_feed.config import load_feed_config  # noqa: E402
from signal_feed.db import FeedStore  # noqa: E402
from signal_feed.opportunities.assess import assess, todo, worst_case_usd  # noqa: E402
from signal_feed.posts import PostsReader  # noqa: E402
from signal_feed.settings import REPO_ROOT, FeedSettings  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--limit", type=int)
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    load_dotenv(REPO_ROOT / ".env")
    settings = FeedSettings.from_env()
    config = load_feed_config(settings.config_file)
    store = FeedStore(settings.feed_database_url)
    bank = ProblemBank(store, PostsReader(settings.posts_database_url))
    need = todo(store, bank.all())[: args.limit]
    print(f"{len(need)} problems to assess; worst case ${worst_case_usd(need, config):.2f}")
    if args.dry_run or not need:
        return 0
    from signal_feed.enrichment.llm import AnthropicTagger
    n, usd = assess(store, config, AnthropicTagger(), need)
    print(f"assessed {n}, cost ${usd:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
