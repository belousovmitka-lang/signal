"""OPPORTUNITIES.md 18.3: tag the opportunity fields (signal_feed/enrichment/opportunity_fields.py).

    python scripts/opportunity_fields.py sample [--n 500] [--dry-run]   # tag a review sample, write CSV + HTML
    python scripts/opportunity_fields.py export                         # rewrite the review files
    python scripts/opportunity_fields.py status

The sample's post ids are kept in data/review/opportunity_fields_sample.json, so re-running `sample` tags
only the ones not yet tagged at the current `opportunity_fields.version`. The full backfill is not here yet:
it waits for the sample's labels to be checked.
"""

import argparse
import json
import logging
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from signal_feed.config import load_feed_config  # noqa: E402
from signal_feed.db import EnrichedPostRow, FeedStore  # noqa: E402
from signal_feed.enrichment.opportunity_fields import OpportunityFieldsPass, export_review, sample_ids  # noqa: E402
from signal_feed.posts import PostsReader  # noqa: E402
from signal_feed.settings import REPO_ROOT, FeedSettings  # noqa: E402

REVIEW = REPO_ROOT / "data" / "review"
IDS = REVIEW / "opportunity_fields_sample.json"


def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample")
    s.add_argument("--n", type=int, default=500)
    s.add_argument("--dry-run", action="store_true", help="pick the sample and print the worst-case cost only")
    sub.add_parser("export")
    sub.add_parser("status")
    args = p.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    load_dotenv(REPO_ROOT / ".env")
    settings = FeedSettings.from_env()
    config = load_feed_config(settings.config_file)
    store, posts = FeedStore(settings.feed_database_url), PostsReader(settings.posts_database_url)
    version = config.enrichment.opportunity_fields.version

    if args.cmd == "status":
        with store.session() as ses:
            print(dict(ses.execute(select(EnrichedPostRow.opp_version, func.count()).group_by(EnrichedPostRow.opp_version)).all()))
        return 0
    if args.cmd == "export":
        ids = json.loads(IDS.read_text())
    else:
        ids = json.loads(IDS.read_text()) if IDS.exists() else sample_ids(store, posts, args.n)
        REVIEW.mkdir(parents=True, exist_ok=True)
        IDS.write_text(json.dumps(ids))
        with store.session() as ses:
            done = set(ses.scalars(select(EnrichedPostRow.post_id).where(
                EnrichedPostRow.post_id.in_(ids), EnrichedPostRow.opp_version == version)))
        todo = [i for i in ids if i not in done]
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise SystemExit("ANTHROPIC_API_KEY is not set")
        from signal_feed.enrichment.llm import AnthropicTagger
        run = OpportunityFieldsPass(store, posts, config, AnthropicTagger())
        print(f"sample {len(ids)} posts, {len(todo)} to tag; worst case ${run.worst_case_usd(len(todo)):.2f}")
        if args.dry_run:
            return 0
        results = run.run(todo)
        failed = Counter(r.error for r in results if r.output is None)
        print(f"tagged {sum(r.output is not None for r in results)}, failed {dict(failed) or 0}, "
              f"cost ${sum(r.cost for r in results):.4f}")
    csv_path, html_path = export_review(store, posts, ids, REVIEW / f"opportunity_fields_{version}")
    print(f"review: {html_path}\n        {csv_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
