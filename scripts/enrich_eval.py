"""Tagging-quality check against evals/enrichment_labels.yaml.

    python scripts/enrich_eval.py sample                 # draw the 200-post sample (ids only)
    python scripts/enrich_eval.py show [--flagged] [--n N] # print labeled posts with their labels
    python scripts/enrich_eval.py score [--run]          # compare tags with labels; --run tags missing ones first
"""

import argparse
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402
from sqlalchemy import select  # noqa: E402

from signal_feed.config import load_feed_config  # noqa: E402
from signal_feed.db import EnrichedPostRow, FeedStore  # noqa: E402
from signal_feed.enrichment.evalset import LABELS_FILE, draw_sample, load_labels, report, score  # noqa: E402
from signal_feed.posts import PostsReader  # noqa: E402
from signal_feed.settings import REPO_ROOT, FeedSettings  # noqa: E402

SAMPLE_FILE = REPO_ROOT / "evals" / "enrichment_sample.yaml"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("sample")
    show = sub.add_parser("show")
    show.add_argument("--flagged", action="store_true", help="only labels marked unsure")
    show.add_argument("--n", type=int)
    show.add_argument("--sample", action="store_true", help="show the unlabeled sample instead")
    run = sub.add_parser("score")
    run.add_argument("--run", action="store_true", help="tag labeled posts that aren't enriched yet (costs money)")
    args = parser.parse_args()

    settings = FeedSettings.from_env()
    config = load_feed_config(settings.config_file)
    posts = PostsReader(settings.posts_database_url)

    if args.command == "sample":
        picked = draw_sample(list(posts.iter_posts()), config.enrichment.prefilter.skip_sources)
        SAMPLE_FILE.parent.mkdir(exist_ok=True)
        SAMPLE_FILE.write_text(yaml.safe_dump(
            [{"post_id": p.id, "source": p.source, "source_type": p.source_type} for p in picked],
            sort_keys=False, allow_unicode=True))
        print(f"wrote {len(picked)} posts to {SAMPLE_FILE.relative_to(REPO_ROOT)}")
        return 0

    if args.command == "show":
        if args.sample:
            entries = [(e["post_id"], None) for e in yaml.safe_load(SAMPLE_FILE.read_text())]
        else:
            entries = [(lb.post_id, lb) for lb in load_labels().labels if lb.unsure or not args.flagged]
        entries = entries[:args.n] if args.n else entries
        by_id = {p.id: p for p in posts.get_posts([pid for pid, _ in entries])}
        for i, (pid, label) in enumerate(entries, 1):
            p = by_id.get(pid)
            if p is None:
                print(f"#{i} {pid}: post no longer in the database\n")
                continue
            print(f"#{i} [{p.source_type} / {p.source}] {p.url}")
            meta = {k: v for k, v in {"role": p.author_role, "community": p.community, "money": p.money,
                                      "snippet": p.is_snippet or None}.items() if v}
            if meta:
                print(f"   {meta}")
            if p.title:
                print(f"   TITLE: {p.title}")
            print(textwrap.indent(textwrap.fill(p.text[:1500], 110), "   "))
            if label:
                print(f"   LABEL: {label.model_dump(exclude={'post_id', 'source'}, exclude_none=True)}")
            print()
        return 0

    store = FeedStore(settings.feed_database_url)
    labels = load_labels().labels
    if args.run:
        from signal_feed.enrichment.llm import AnthropicTagger
        from signal_feed.enrichment.pipeline import Enricher
        enricher = Enricher(store, posts, config, AnthropicTagger())
        pending = set(enricher.pending_post_ids())
        todo = [lb.post_id for lb in labels if lb.post_id in pending]
        stats = enricher.enrich(todo)
        print(f"tagged {len(todo)} posts: ok {stats.ok}, dropped {stats.dropped}, needs_review {stats.needs_review},"
              f" escalated {stats.escalated}, cost ${stats.cost_usd:.4f}\n")
    with store.session() as s:
        enriched = {r.post_id: r for r in s.scalars(select(EnrichedPostRow).where(
            EnrichedPostRow.post_id.in_([lb.post_id for lb in labels]),
            EnrichedPostRow.prompt_version == config.enrichment.prompt_version))}
    result = score(labels, enriched, config.enrichment.spam_drop_threshold,
                   config.enrichment.drop_post_types)
    print(report(result))
    reviewed = sum(lb.reviewed for lb in labels)
    print(f"\nlabels: {len(labels)} ({reviewed} human-reviewed, {sum(lb.unsure for lb in labels)} flagged unsure)")
    if result.disagreements:
        print("\npost_type or industry disagreements:")
        for label, row in result.disagreements[:40]:
            print(f"  {label.post_id[:10]} label {label.post_type}/{label.industry}  tagger {row.post_type}/"
                  f"{(row.industry_path or ['-'])[0]}  conf {row.confidence}  {label.source}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
