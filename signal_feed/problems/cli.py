"""Niche tree and problems CLI (scripts/problems.py is the entry point).

    python scripts/problems.py build [--no-llm] [--no-split]   # seed, grow the tree, rebuild problems
    python scripts/problems.py tree [--skills] [--min-support N]
    python scripts/problems.py top [--niche LABEL] [--per-niche N] [--min-posts N]
    python scripts/problems.py rescore      # hourly: refresh scores (trend) without re-clustering
"""

from __future__ import annotations

import argparse
import logging
import os
import textwrap
from collections import defaultdict

from sqlalchemy import select

from ..config import load_feed_config
from ..db import FeedStore, NicheRow, ProblemRow
from ..llm_task import LLMTask
from ..niches.growth import NicheGrower
from ..niches.naming import NicheNamer
from ..niches.tree import INDUSTRY, SKILL, Tree, embed_seed_labels, seed_tree
from ..posts import PostsReader
from ..settings import FeedSettings
from .cluster import ProblemBuilder


def build(store: FeedStore, posts: PostsReader, config, use_llm: bool, split: bool) -> None:
    llm = None
    if use_llm:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise SystemExit("ANTHROPIC_API_KEY is not set: add it to .env, or pass --no-llm")
        from ..enrichment.llm import AnthropicTagger
        llm = LLMTask(store, config.enrichment, AnthropicTagger())
    print(f"seeded {seed_tree(store)} new nodes (roots and config/niches.yaml branches)")
    from ..enrichment.schema import industry_names, niche_seeds
    bare = [n for n in industry_names() if n not in niche_seeds()]
    if bare:
        print(f"note: no branches in config/niches.yaml for {', '.join(bare)} (posts stay on the industry)")
    if os.environ.get("VOYAGE_API_KEY"):
        from ..enrichment.embeddings import VoyageEmbedder
        print(f"embedded {embed_seed_labels(store, VoyageEmbedder(config.embeddings), config)} branch descriptions")
    else:
        print("VOYAGE_API_KEY not set: branches are reached by tag only")
    g = NicheGrower(store, posts, config, NicheNamer(llm)).run(split=split)
    print(f"tree: {len(g.created)} new, {len(g.merged)} merged, {len(g.retired)} retired; {g.placed} placements")
    for line in g.created:
        print(f"  + {line}")
    for line in g.merged:
        print(f"  = {line}")
    for line in g.retired:
        print(f"  - {line}")
    p = ProblemBuilder(store, posts, config, llm).run()
    # (BudgetExceededError, incl. BillingError, propagates: a build without the LLM is `--no-llm`.)
    print(f"problems: {p.problems} pain, {p.demand} demand ({p.kept_ids} kept their id, {p.new} new, {p.gone} gone); "
          f"{p.merge_checks} merge checks, {p.merges} merged, {p.same_author_merges} same-author demand joined; LLM cost ${llm.spent if llm else 0:.4f}")


def print_tree(store: FeedStore, tree: str, min_support: int) -> None:
    t = Tree.load(store, tree)

    def walk(nid: int, depth: int) -> None:
        node = t.nodes[nid]
        if node.support < min_support and depth and not node.seeded:
            return
        count = f"{node.support}" if node.support else "no posts yet"
        print(f"{'  ' * depth}{node.label}  ({count}){'  [grown]' if depth and not node.seeded else ''}")
        for child in sorted(t.children.get(nid, []), key=lambda c: -t.nodes[c].support):
            walk(child, depth + 1)

    for root in sorted(t.roots.values(), key=lambda r: -t.nodes[r].support):
        if t.nodes[root].support >= min_support:
            walk(root, 0)


def print_top(store: FeedStore, config, niche: str | None, per_niche: int, min_posts: int,
              kinds: tuple[str, ...] = ("pain", "demand")) -> None:
    t = Tree.load(store, INDUSTRY)
    with store.session() as s:
        problems = list(s.scalars(select(ProblemRow).where(ProblemRow.status == "active",
                                                           ProblemRow.post_count >= min_posts)))
    by_niche = defaultdict(list)
    for p in problems:
        if p.kind in kinds:
            by_niche[p.niche_id].append(p)
    early = config.problems.early_signal_below
    order = sorted(by_niche, key=lambda n: t.path(n))
    for nid in order:
        path = " > ".join(t.path(nid))
        if niche and niche.lower() not in path.lower():
            continue
        here = by_niche[nid]
        print(f"\n{path}  ({sum(p.kind == 'pain' for p in here)} problems, {sum(p.kind == 'demand' for p in here)} demand)")
        for kind in kinds:
            top = sorted((p for p in here if p.kind == kind), key=lambda p: -p.score)[:per_niche]
            if top:
                print(f"  {'Problems' if kind == 'pain' else 'Demand (tenders, jobs, gigs)'}:")
            _print_items(top, early)


def _print_items(items, early: float) -> None:
    for p in items:
        evidence = "early signal" if p.confidence < early else f"confidence {p.confidence:.2f}"
        parts = " ".join(f"{k[:5]} {v:.2f}" for k, v in p.score_breakdown.items())
        print(f"  {p.score:.2f}  " + textwrap.shorten(p.title, 110))
        extra = [f"{p.post_count} posts", f"{p.distinct_authors} voices", f"{len(p.sources)} sources", evidence]
        if p.roles:
            extra.append("who: " + ", ".join(p.roles[:2]))
        if p.competitors:
            extra.append("tools: " + ", ".join(p.competitors[:3]))
        print(f"        {' | '.join(extra)}")
        print(f"        {parts}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Niche tree and problems")
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build")
    b.add_argument("--no-llm", action="store_true", help="name niches from tags; skip merge checks (free)")
    b.add_argument("--no-split", action="store_true", help="don't create new niches this run")
    tr = sub.add_parser("tree")
    tr.add_argument("--skills", action="store_true")
    tr.add_argument("--min-support", type=int, default=1)
    top = sub.add_parser("top")
    top.add_argument("--niche")
    top.add_argument("--per-niche", type=int, default=3)
    top.add_argument("--min-posts", type=int, default=1)
    top.add_argument("--kind", choices=["pain", "demand"], help="only problems or only demand")
    sub.add_parser("rescore", help="refresh problem scores (hourly)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    settings = FeedSettings.from_env()
    config = load_feed_config(settings.config_file)
    store = FeedStore(settings.feed_database_url)
    if args.command == "build":
        from ..enrichment.costs import BudgetExceededError
        try:
            build(store, PostsReader(settings.posts_database_url), config, not args.no_llm, not args.no_split)
        except BudgetExceededError as exc:
            raise SystemExit(f"stopped: {exc}\n(the tree and problems from before this run are unchanged up to "
                             "the step that stopped; `build --no-llm` runs without Claude)") from None
    elif args.command == "rescore":
        print(f"rescored {ProblemBuilder(store, PostsReader(settings.posts_database_url), config).rescore()} problems")
    elif args.command == "tree":
        print_tree(store, SKILL if args.skills else INDUSTRY, args.min_support)
    else:
        print_top(store, config, args.niche, args.per_niche, args.min_posts,
                  (args.kind,) if args.kind else ("pain", "demand"))
    return 0
