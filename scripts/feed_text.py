"""Headlines for feed items (signal_feed/feed/explain.py).

    python scripts/feed_text.py headlines --estimate      # how many items need one, and the expected cost
    python scripts/feed_text.py headlines [--limit N]     # write them (Claude; daily job `headlines`)
"""

import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from signal_feed.config import load_feed_config  # noqa: E402
from signal_feed.db import FeedStore  # noqa: E402
from signal_feed.enrichment.costs import BudgetExceededError  # noqa: E402
from signal_feed.feed.data import load  # noqa: E402
from signal_feed.feed.explain import headlines_for, needs_headline, write_headlines  # noqa: E402
from signal_feed.posts import PostsReader  # noqa: E402
from signal_feed.settings import FeedSettings  # noqa: E402

# Measured on 199 headlines (2026-10-29): $0.0179 with the tagger model.
USD_PER_HEADLINE = 0.00009


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="command", required=True)
    h = sub.add_parser("headlines")
    h.add_argument("--limit", type=int)
    h.add_argument("--estimate", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    settings = FeedSettings.from_env()
    store, config = FeedStore(settings.feed_database_url), load_feed_config(settings.config_file)
    posts = PostsReader(settings.posts_database_url)
    data = load(store, posts, config)
    have = set(headlines_for(store, [it.post_id for it in data.items]))
    todo = sum(1 for it in data.items if it.post_id not in have and needs_headline(it, config))
    if args.estimate:
        print(f"{todo} of {len(data.items)} items need a headline; about ${todo * USD_PER_HEADLINE:.2f}")
        return 0
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("ANTHROPIC_API_KEY is not set")
    from signal_feed.enrichment.llm import AnthropicTagger
    from signal_feed.llm_task import LLMTask
    llm = LLMTask(store, config.enrichment, AnthropicTagger())
    try:
        n = write_headlines(store, posts, config, data, llm, args.limit)
    except BudgetExceededError as exc:
        raise SystemExit(f"stopped: {exc}") from None
    print(f"wrote {n} headlines (of {min(todo, args.limit or todo)} asked), cost ${llm.spent:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
