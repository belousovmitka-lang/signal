"""Backfills through the Message Batches API (50% off, results within 24 hours).

`submit` sends the tagger requests and reserves their worst-case cost against the budget.
`collect` (a later run) reads finished batches, records actual cost, releases the reservation and
finishes each post through the sync path: one retry on invalid output, escalation when unsure
(server-side fallbacks are not allowed on batches, so escalations always run sync).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

import anthropic

from ..db import EnrichmentBatchRow
from .costs import BudgetExceededError, claude_worst_case_usd
from .llm import TransientError, read_message, request_params
from .pipeline import Enricher, RunStats
from .prompt import system_prompt, user_prompt

log = logging.getLogger(__name__)

MAX_REQUESTS_PER_BATCH = 5000


class BatchRunner:
    def __init__(self, enricher: Enricher, client: Any | None = None) -> None:
        self.enricher = enricher
        self.client = client or anthropic.Anthropic(max_retries=4)

    def submit(self, post_ids: Sequence[str]) -> tuple[list[str], RunStats]:
        """Pre-filter, then submit the rest in batches. Returns batch ids and stats for skipped posts."""
        e, cfg = self.enricher, self.enricher.cfg
        stats, batch_ids = RunStats(), []
        posts = e._load(post_ids)
        to_tag = []
        for post in posts:
            if reason := e.prefilter(post):
                row = e._row(post, status="skipped", reason=reason)
                e._save(row)
                stats.add(row)
            else:
                to_tag.append(post)
        system = system_prompt()
        for i in range(0, len(to_tag), MAX_REQUESTS_PER_BATCH):
            chunk = to_tag[i:i + MAX_REQUESTS_PER_BATCH]
            requests, reserve = [], 0.0
            for post in chunk:
                user = user_prompt(post, cfg.max_input_chars)
                requests.append({"custom_id": post.id, "params": request_params(cfg.tagger, system, user)})
                reserve += claude_worst_case_usd(cfg.tagger, len(system) + len(user), cfg, batch=True)
            try:
                e.ledger.check("anthropic", reserve, cfg.monthly_budget_usd, e.clock())
            except BudgetExceededError as exc:
                stats.stopped = str(exc)
                break
            batch = self.client.messages.batches.create(requests=requests)
            with e.store.session() as s, s.begin():
                s.add(EnrichmentBatchRow(id=batch.id, model=cfg.tagger.model, prompt_version=cfg.prompt_version,
                                         post_ids=[p.id for p in chunk], status="open", reserved_usd=reserve,
                                         submitted_at=e.clock()))
            batch_ids.append(batch.id)
            log.info("submitted batch %s with %d posts (reserved $%.4f)", batch.id, len(chunk), reserve)
        return batch_ids, stats

    def collect(self) -> RunStats:
        """Finish every open batch that has ended; leave running ones for the next call."""
        e = self.enricher
        stats = RunStats()
        with e.store.session() as s:
            open_batches = list(s.query(EnrichmentBatchRow).filter(EnrichmentBatchRow.status == "open"))
        for batch_row in open_batches:
            batch = self.client.messages.batches.retrieve(batch_row.id)
            if batch.processing_status != "ended":
                continue
            # Read and price every result first, then release the reservation, so follow-up calls
            # are checked against actual spend rather than spend + reservation.
            firsts = []
            for result in self.client.messages.batches.results(batch_row.id):
                if result.result.type != "succeeded":
                    continue   # errored, expired, canceled: the post stays pending for the next run
                attempt = read_message(result.result.message)
                cost = e.record(attempt.usages, "tag", result.custom_id, batch=True, batch_id=batch_row.id)
                firsts.append((result.custom_id, attempt, cost))
            posts = {p.id: p for p in e.posts.get_posts([pid for pid, _, _ in firsts])}
            for post_id, attempt, cost in firsts:
                post = posts.get(post_id)
                if post is None:
                    continue   # purged at the origin while the batch ran
                try:
                    # No paid calls here: retries and escalations are marked for `enrich.py escalate`,
                    # which runs them under a cap, so collecting never spends beyond the batch itself.
                    row = e.enrich_post(post, first=attempt, first_cost=cost, defer_followups=True)
                except TransientError as exc:
                    log.warning("transient error finishing %s: %s", post_id, exc)
                    stats.transient += 1
                    continue
                except BudgetExceededError as exc:
                    stats.stopped = str(exc)
                    if attempt.output is None:
                        row = e._row(post, status="needs_review", reason="budget", cost=cost, model=attempt.model)
                    else:
                        row = e._tagged_row(post, attempt.output, attempt.model, False, cost, reason="budget")
                e._save(row)
                stats.add(row)
            # Marked collected only once every result is saved (results stay downloadable for 29 days).
            with e.store.session() as s, s.begin():
                row = s.get(EnrichmentBatchRow, batch_row.id)
                row.status, row.collected_at = "collected", e.clock()
        return stats
