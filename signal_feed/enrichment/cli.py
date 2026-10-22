"""Enrichment CLI (scripts/enrich.py is the entry point).

    python scripts/enrich.py status
    python scripts/enrich.py run [--limit N] [--sync | --batch]
    python scripts/enrich.py collect          # finish submitted batches (no paid calls)
    python scripts/enrich.py escalate [--limit N]   # deferred retries/escalations, capped per run
    python scripts/enrich.py embed [--limit N]
"""

from __future__ import annotations

import argparse
import logging
import os

from sqlalchemy import func, select

from signal_connectors.models import utcnow

from ..config import FeedConfig, load_feed_config
from ..db import EnrichedPostRow, EnrichmentBatchRow, FeedStore
from ..posts import PostsReader
from ..settings import FeedSettings
from .costs import CostLedger
from .pipeline import Enricher, RunStats


def _require(var: str) -> None:
    if not os.environ.get(var):
        raise SystemExit(f"{var} is not set: add it to .env (see .env.example)")


def _print_stats(label: str, stats: RunStats) -> None:
    print(f"{label}: ok {stats.ok}, dropped {stats.dropped}, skipped {stats.skipped}, "
          f"needs_review {stats.needs_review}, escalated {stats.escalated}, transient {stats.transient}, "
          f"cost ${stats.cost_usd:.4f}")
    if stats.stopped:
        print(f"stopped early: {stats.stopped}")


def status(store: FeedStore, enricher: Enricher, config: FeedConfig) -> None:
    with store.session() as s:
        counts = dict(s.execute(select(EnrichedPostRow.status, func.count()).where(
            EnrichedPostRow.prompt_version == config.enrichment.prompt_version).group_by(EnrichedPostRow.status)).all())
        embedded = s.scalar(select(func.count()).where(EnrichedPostRow.embedding.is_not(None))) or 0
        open_batches = list(s.scalars(select(EnrichmentBatchRow.id).where(EnrichmentBatchRow.status == "open")))
    ledger = CostLedger(store)
    print(f"prompt {config.enrichment.prompt_version}: {counts or 'nothing enriched yet'}")
    print(f"pending: {len(enricher.pending_post_ids())}, embedded: {embedded}, open batches: {open_batches or 'none'}, "
          f"follow-ups waiting: {enricher.followup_count()}")
    from .prompt import prompt_fingerprint
    with store.session() as s:
        stale = s.scalar(select(func.count()).select_from(EnrichedPostRow).where(
            EnrichedPostRow.prompt_version == config.enrichment.prompt_version,
            EnrichedPostRow.status.in_(("ok", "dropped")),          # needs_review rows carry no tags
            (EnrichedPostRow.prompt_hash.is_(None)) | (EnrichedPostRow.prompt_hash != prompt_fingerprint()))) or 0
    if stale:
        print(f"note: {stale} posts at {config.enrichment.prompt_version} were tagged with a different or unknown prompt "
              f"(the industry list, config/niches.yaml or config/skills.yaml changed since); bump prompt_version to "
              f"re-tag them")
    print(f"this month: Claude ${ledger.month_spend('anthropic'):.4f} of ${config.enrichment.monthly_budget_usd:.2f}"
          f" (incl. batch reservations), Voyage ${ledger.month_spend('voyage'):.4f}"
          f" of ${config.embeddings.monthly_budget_usd:.2f}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage A enrichment")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    run = sub.add_parser("run", help="tag pending posts")
    run.add_argument("--limit", type=int)
    mode = run.add_mutually_exclusive_group()
    mode.add_argument("--sync", action="store_true", help="call the API per post now")
    mode.add_argument("--batch", action="store_true", help="submit to the Batch API (50%% off, up to 24h)")
    sub.add_parser("collect", help="finish batches that have ended (no paid calls)")
    esc = sub.add_parser("escalate", help="run deferred retries and escalations (capped per run)")
    esc.add_argument("--limit", type=int)
    embed = sub.add_parser("embed", help="embed tagged posts")
    embed.add_argument("--limit", type=int)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    settings = FeedSettings.from_env()
    config = load_feed_config(settings.config_file)
    store = FeedStore(settings.feed_database_url)
    posts = PostsReader(settings.posts_database_url)

    if args.command == "status":
        status(store, Enricher(store, posts, config, tagger=None), config)   # type: ignore[arg-type]
        return 0
    if args.command == "embed":
        _require("VOYAGE_API_KEY")
        from .embeddings import VoyageEmbedder
        enricher = Enricher(store, posts, config, tagger=None, embedder=VoyageEmbedder(config.embeddings))  # type: ignore[arg-type]
        print(f"embedded {enricher.embed_pending(args.limit)} posts")
        from ..feed.clips import sync_clips
        from ..feed.data import load as load_feed_data
        print(f"learned {sync_clips(store, posts, config, load_feed_data(store, posts, config), enricher.embedder)} clips")
        return 0

    _require("ANTHROPIC_API_KEY")
    from .batch import BatchRunner
    from .llm import AnthropicTagger
    enricher = Enricher(store, posts, config, AnthropicTagger())
    if args.command == "collect":
        _print_stats("collected", BatchRunner(enricher).collect())
        print(f"follow-ups waiting: {enricher.followup_count()} (run `escalate`)")
        return 0
    if args.command == "escalate":
        _print_stats("escalated", enricher.escalate_followups(args.limit))
        print(f"follow-ups left: {enricher.followup_count()}")
        return 0

    purged = enricher.purge_deleted()
    if purged:
        print(f"purged enrichment of {purged} posts deleted at the origin")
    pending = enricher.pending_post_ids(args.limit)
    use_batch = args.batch or (not args.sync and len(pending) >= config.enrichment.batch_min_posts)
    started = utcnow()
    if use_batch:
        batch_ids, stats = BatchRunner(enricher).submit(pending)
        _print_stats("pre-filtered", stats)
        print(f"submitted {batch_ids or 'no batches'}; run `collect` later (most finish within an hour)")
    else:
        _print_stats(f"tagged {len(pending)} posts in {(utcnow() - started).seconds}s", enricher.enrich(pending))
    return 0
