"""Inferred problems (FEED_ALGORITHM.md section 17): run detectors, list results.

    python scripts/inferred.py run [--detector paid_workarounds] [--no-llm]   # weekly; Detector 1 also daily
    python scripts/inferred.py list [--status published|candidate|rejected|expired]
    python scripts/inferred.py weights      # weekly: detector weights from insider votes and actions (17.7)

Without the LLM (or with --no-llm) candidates are found and stored but held as `candidate`: nothing is
published before the critic has checked its evidence.
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from signal_feed.config import load_feed_config  # noqa: E402
from signal_feed.db import FeedStore, InferredEvidenceRow, InferredProblemRow  # noqa: E402
from signal_feed.enrichment.costs import BudgetExceededError  # noqa: E402
from signal_feed.inferred.pipeline import DETECTORS, run_detector  # noqa: E402
from signal_feed.posts import PostsReader  # noqa: E402
from signal_feed.settings import FeedSettings  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--detector", choices=sorted(DETECTORS), action="append")
    run.add_argument("--no-llm", action="store_true")
    sub.add_parser("weights", help="weekly: update detector weights from validation (17.7)")
    ls = sub.add_parser("list")
    ls.add_argument("--status")
    args = ap.parse_args()
    settings = FeedSettings.from_env()
    store, config = FeedStore(settings.feed_database_url), load_feed_config(settings.config_file)
    if args.command == "run":
        llm = None
        if not args.no_llm and os.environ.get("ANTHROPIC_API_KEY"):
            from signal_feed.enrichment.llm import AnthropicTagger
            from signal_feed.llm_task import LLMTask
            llm = LLMTask(store, config.enrichment, AnthropicTagger())
        from signal_feed.inferred.corroborate import x_searcher
        searcher = x_searcher() if llm is not None else None    # X corroboration (connectors' search_x, once built)
        if llm is not None and searcher is None:
            print("note: signal_connectors.ondemand.search_x not built yet: near misses get a plan, no X search")
        for name in args.detector or list(DETECTORS):          # in pipeline order
            try:
                r = run_detector(store, PostsReader(settings.posts_database_url), config, name, llm,
                                 searcher=searcher)
            except BudgetExceededError as exc:   # incl. a billing refusal: a message, not a traceback
                raise SystemExit(f"stopped: {exc}\n(run again with --no-llm to find candidates without the critic)") from None
            print(f"{name}: {r.clusters} groups examined, {r.candidates} candidates -> {r.published} published, "
                  f"{r.rejected} rejected, {r.held} held for the critic, {r.expired} expired; "
                  f"{r.critic_runs} critic calls, LLM cost ${llm.spent if llm else 0:.4f}; "
                  f"{r.near_misses} near misses, {r.plans} plans, {r.searches} X searches")
            if r.search_stopped:
                print(f"  X search stopped: {r.search_stopped}")
            for line in r.items:
                print(f"  {line}")
        return 0
    if args.command == "weights":
        from signal_feed.inferred.validation import update_weights
        for w in update_weights(store, config):
            print(f"{w.detector} [{w.combo}]: weight {w.weight} ({w.published} published, {w.confirmed} confirmed, "
                  f"{w.acted} acted on, {w.rejected} rejected by insiders)")
        return 0
    with store.session() as s:
        q = select(InferredProblemRow).order_by(InferredProblemRow.score.desc())
        if args.status:
            q = q.where(InferredProblemRow.status == args.status)
        for p in s.scalars(q):
            ev = list(s.scalars(select(InferredEvidenceRow).where(InferredEvidenceRow.inferred_id == p.id)))
            print(f"#{p.id} [{p.status}{'/' + p.confidence if p.confidence else ''}] {p.hypothesis}")
            print(f"    {len(ev)} evidence items from {len({e.source_type for e in ev})} source types; "
                  f"{len(p.counter_evidence)} counter-evidence; critic: {p.critic_notes.get('verdict', 'not run')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
