"""Stage A: enrich raw posts once (FEED_ALGORITHM.md section 3).

Per post: cheap pre-filter -> tagger model -> one retry on invalid output -> escalation to the
larger model on a refusal or low confidence -> PII scrub -> enriched_posts row. Each post is saved
as soon as it is done. Embeddings are a separate pass over rows that are `ok`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from pydantic import BaseModel
from sqlalchemy import delete, func, select

from signal_connectors.models import RawPost, utcnow

from ..config import FeedConfig, ModelCall
from ..db import EnrichedPostRow, EnrichmentBatchRow, FeedStore, InferredEvidenceRow, ObligationRow
from ..posts import PostsReader
from .costs import BudgetExceededError, CostLedger, Usage, claude_usd, claude_worst_case_usd
from .embeddings import Embedder
from .llm import TagAttempt, Tagger, TransientError
from .pii import scrub
from .prompt import prompt_fingerprint, system_prompt, user_prompt

log = logging.getLogger(__name__)

# Errors worth one more try on the same model; a refusal or a rejected request won't change.
RETRY_SAME_MODEL = {"invalid_json", "max_tokens"}


@dataclass
class RunStats:
    ok: int = 0
    dropped: int = 0
    skipped: int = 0
    needs_review: int = 0
    escalated: int = 0
    transient: int = 0
    cost_usd: float = 0.0
    stopped: str | None = None         # why the run ended early (budget), if it did
    statuses: dict[str, str] = field(default_factory=dict)

    def add(self, row: EnrichedPostRow) -> None:
        setattr(self, row.status, getattr(self, row.status) + 1)
        self.escalated += int(row.escalated)
        self.cost_usd += row.cost_usd
        self.statuses[row.post_id] = row.status


class Enricher:
    def __init__(self, store: FeedStore, posts: PostsReader, config: FeedConfig, tagger: Tagger,
                 embedder: Embedder | None = None, clock: Callable[[], datetime] = utcnow) -> None:
        self.store, self.posts, self.config = store, posts, config
        self.cfg = config.enrichment
        self.tagger, self.embedder, self.clock = tagger, embedder, clock
        self.ledger = CostLedger(store)

    # --- what needs doing ----------------------------------------------------

    def pending_post_ids(self, limit: int | None = None, source: str | None = None) -> list[str]:
        """Posts never enriched, or enriched by an older prompt, and not waiting in an open batch; only from
        `source` when given (e.g. the X posts an inferred run is waiting on)."""
        with self.store.session() as s:
            current = set(s.scalars(select(EnrichedPostRow.post_id).where(
                EnrichedPostRow.prompt_version == self.cfg.prompt_version)))
            in_batches = {pid for ids in s.scalars(select(EnrichmentBatchRow.post_ids).where(
                EnrichmentBatchRow.status == "open")) for pid in ids}
        only = self.posts.ids_from_source(source) if source else None
        pending = []
        for post_id in self.posts.all_ids():
            if only is not None and post_id not in only:
                continue
            if post_id not in current and post_id not in in_batches:
                pending.append(post_id)
                if limit is not None and len(pending) >= limit:
                    break
        return pending

    def prefilter(self, post: RawPost) -> str | None:
        pf = self.cfg.prefilter
        if any(post.source.startswith(prefix) for prefix in pf.skip_sources):
            return "aggregate_source"
        if len((post.title or "") + post.text) < pf.min_chars:
            return "too_short"
        return None

    # --- sync path -----------------------------------------------------------

    def enrich(self, post_ids: Sequence[str]) -> RunStats:
        stats = RunStats()
        for post in self._load(post_ids):
            try:
                row = self.enrich_post(post)
            except TransientError as exc:
                log.warning("transient error on %s, will retry next run: %s", post.id, exc)
                stats.transient += 1
                continue
            except BudgetExceededError as exc:
                stats.stopped = str(exc)
                break
            self._save(row)
            stats.add(row)
        return stats

    def enrich_post(self, post: RawPost, first: TagAttempt | None = None, first_cost: float = 0.0,
                    defer_followups: bool = False) -> EnrichedPostRow:
        """Tag one post. `first` is a tagger result already in hand (from a batch), if any. With
        `defer_followups` no paid call is made: a needed retry or escalation is marked on the row
        (`followup`) for `escalate_followups`, which runs them under a per-run cap."""
        if reason := self.prefilter(post):
            return self._row(post, status="skipped", reason=reason)
        system, user = system_prompt(), user_prompt(post, self.cfg.max_input_chars)
        cost = first_cost
        tries = 1 + self.cfg.max_retries_invalid_json
        if defer_followups and first is not None:
            esc = self.cfg.escalation
            if first.output is None:
                followup = "retry" if first.error in RETRY_SAME_MODEL else "escalate"
                row = self._row(post, status="needs_review", reason=first.error, cost=cost, model=first.model)
            else:
                unsure = first.output.confidence < esc.below_confidence
                followup = "escalate" if unsure else None
                row = self._tagged_row(post, first.output, first.model, False, cost, reason=None)
            row.followup = followup
            return row
        if first is None:
            attempt, spent = self._call(self.cfg.tagger, "tag", post, system, user, tries=tries)
            cost += spent
        elif first.output is None and first.error in RETRY_SAME_MODEL:
            attempt, spent = self._call(self.cfg.tagger, "tag", post, system, user, tries=tries - 1)
            cost += spent
        else:
            attempt = first

        escalated, esc_error = False, None
        esc = self.cfg.escalation
        unsure = attempt.output is not None and attempt.output.confidence < esc.below_confidence
        if attempt.error == "refusal" or unsure:
            e_attempt, spent = self._call(esc, "escalate", post, system, user, tries=tries,
                                          fallbacks=esc.server_fallbacks)
            cost += spent
            if e_attempt.output is not None:
                attempt, escalated = e_attempt, True
            else:
                esc_error = e_attempt.error

        if attempt.output is None:
            return self._row(post, status="needs_review", reason=esc_error or attempt.error, cost=cost,
                             model=attempt.model, escalated=escalated)
        return self._tagged_row(post, attempt.output, attempt.model, escalated, cost,
                                reason=f"escalation_{esc_error}" if esc_error else None)

    def followup_count(self) -> int:
        with self.store.session() as s:
            return s.scalar(select(func.count()).select_from(EnrichedPostRow).where(
                EnrichedPostRow.followup.is_not(None),
                EnrichedPostRow.prompt_version == self.cfg.prompt_version)) or 0

    def escalate_followups(self, limit: int | None = None) -> RunStats:
        """Run the retries and escalations batch collection deferred, at most `escalation.max_per_run`
        (or `limit`, if lower) per run. A failed escalation keeps the tagger's answer."""
        cap = self.cfg.escalation.max_per_run if limit is None else min(limit, self.cfg.escalation.max_per_run)
        with self.store.session() as s:
            rows = list(s.scalars(select(EnrichedPostRow).where(
                EnrichedPostRow.followup.is_not(None), EnrichedPostRow.prompt_version == self.cfg.prompt_version)
                .order_by(EnrichedPostRow.enriched_at).limit(cap)))
            for r in rows:
                s.expunge(r)
        stats = RunStats()
        posts = {p.id: p for p in self.posts.get_posts([r.post_id for r in rows])}
        esc = self.cfg.escalation
        for old in rows:
            post = posts.get(old.post_id)
            if post is None:
                continue
            try:
                if old.followup == "retry":
                    row = self.enrich_post(post, first_cost=old.cost_usd or 0.0)
                else:
                    system, user = system_prompt(), user_prompt(post, self.cfg.max_input_chars)
                    attempt, spent = self._call(esc, "escalate", post, system, user,
                                                tries=1 + self.cfg.max_retries_invalid_json,
                                                fallbacks=esc.server_fallbacks)
                    cost = (old.cost_usd or 0.0) + spent
                    if attempt.output is not None:
                        row = self._tagged_row(post, attempt.output, attempt.model, True, cost, reason=None)
                    else:          # keep what the tagger said (or needs_review), now settled
                        old.followup, old.cost_usd = None, cost
                        old.status_reason = old.status_reason or f"escalation_{attempt.error}"
                        with self.store.session() as s, s.begin():
                            s.merge(old)
                        stats.add(old)
                        continue
            except TransientError as exc:
                log.warning("transient error on %s, will retry next run: %s", post.id, exc)
                stats.transient += 1
                continue
            except BudgetExceededError as exc:
                stats.stopped = str(exc)
                break
            row.followup = None
            self._save(row)
            stats.add(row)
        return stats

    def _call(self, call: ModelCall, purpose: str, post: RawPost, system: str, user: str, *, tries: int,
              fallbacks: bool = False) -> tuple[TagAttempt, float]:
        spent = 0.0
        attempt = TagAttempt(output=None, model=call.model, error="not_called")
        for _ in range(max(tries, 0)):
            worst = claude_worst_case_usd(call, len(system) + len(user), self.cfg)
            self.ledger.check("anthropic", worst, self.cfg.monthly_budget_usd, self.clock())
            attempt = self.tagger.tag(call, system, user, fallbacks=fallbacks)
            spent += self.record(attempt.usages, purpose, post.id, request_id=attempt.request_id)
            if attempt.output is not None or attempt.error not in RETRY_SAME_MODEL:
                break
        return attempt, spent

    def record(self, usages: Iterable[Usage], purpose: str, post_id: str, *, batch: bool = False,
               batch_id: str | None = None, request_id: str | None = None) -> float:
        total = 0.0
        for usage in usages:
            usd = claude_usd(usage, self.cfg, batch=batch)
            self.ledger.record(provider="anthropic", purpose=purpose, usage=usage, usd=usd, batch=batch,
                               post_id=post_id, batch_id=batch_id, request_id=request_id, at=self.clock())
            total += usd
        return total

    # --- rows ----------------------------------------------------------------

    def _tagged_row(self, post: RawPost, out: BaseModel, model: str, escalated: bool, cost: float,
                    reason: str | None) -> EnrichedPostRow:
        status = "ok"
        if out.post_type in self.cfg.drop_post_types:
            status, reason = "dropped", out.post_type
        elif out.spam_score > self.cfg.spam_drop_threshold:
            status, reason = "dropped", "spam"
        money = out.money_signal.model_dump() if out.money_signal else None
        if money:
            money["quote"] = scrub(money["quote"])
        return self._row(
            post, status=status, reason=reason, cost=cost, model=model, escalated=escalated,
            post_type=out.post_type, industry_path=[out.industry, *out.industry_subpath],
            skills_needed=list(out.skills_needed), who_has_pain=scrub(out.who_has_pain),
            pain_statement=scrub(out.pain_statement), current_workaround=scrub(out.current_workaround),
            tools_mentioned=[t.strip() for t in out.tools_mentioned if t.strip()],
            intensity=out.intensity, money_signal=money, buyer_likelihood=out.buyer_likelihood,
            quality=out.quality, spam_score=out.spam_score, toxicity=out.toxicity, language=out.language,
            confidence=out.confidence, prompt_hash=prompt_fingerprint(),
            task=scrub(out.task) if out.task else None,
            task_automatable=out.task_automatable if out.task else None,
            data_moved=[d.model_dump() for d in out.data_moved] or None,
            obligations_out=[o.model_dump() for o in out.obligations])

    def _row(self, post: RawPost, *, status: str, reason: str | None, cost: float = 0.0,
             model: str | None = None, escalated: bool = False, obligations_out: list[dict] | None = None,
             **fields) -> EnrichedPostRow:
        row = EnrichedPostRow(post_id=post.id, status=status, status_reason=reason, cost_usd=cost, model=model,
                              escalated=escalated, prompt_version=self.cfg.prompt_version,
                              enriched_at=self.clock(), embedding=None, pain_embedding=None, task_embedding=None,
                              embedding_model=None, **({"language": post.language} | fields))
        row._obligations = obligations_out or []      # written by _save (section 17, Detector 2)
        return row

    def _save(self, row: EnrichedPostRow) -> None:
        obligations = getattr(row, "_obligations", [])
        with self.store.session() as s, s.begin():
            # Re-tagging replaces the post's obligations.
            s.execute(delete(ObligationRow).where(ObligationRow.post_id == row.post_id))
            ids = []
            for o in obligations:
                ob = ObligationRow(post_id=row.post_id, who=scrub(o["who"]), what=scrub(o["what"]),
                                   deadline=_parse_date(o.get("deadline")), industries=list(o.get("industries") or []),
                                   created_at=self.clock())
                s.add(ob)
                s.flush()
                ids.append(ob.id)
            row.obligation_ids = ids or None
            s.merge(row)

    def _load(self, post_ids: Sequence[str]) -> list[RawPost]:
        by_id = {p.id: p for p in self.posts.get_posts(post_ids)}
        return [by_id[pid] for pid in post_ids if pid in by_id]

    # --- housekeeping --------------------------------------------------------

    def purge_deleted(self) -> int:
        """Drop enrichment for posts the connectors purged (platform deletion rules)."""
        with self.store.session() as s:
            enriched = set(s.scalars(select(EnrichedPostRow.post_id)))
        gone = enriched - self.posts.existing_ids(enriched)
        with self.store.session() as s:          # evidence links to deleted posts go too (X: within 24h)
            cited = set(s.scalars(select(InferredEvidenceRow.post_id).where(InferredEvidenceRow.post_id.is_not(None))))
        gone_cited = cited - self.posts.existing_ids(cited)
        if gone or gone_cited:
            with self.store.session() as s, s.begin():
                s.execute(delete(EnrichedPostRow).where(EnrichedPostRow.post_id.in_(gone)))
                s.execute(delete(InferredEvidenceRow).where(InferredEvidenceRow.post_id.in_(gone_cited)))
        return len(gone | gone_cited)

    # --- embeddings ----------------------------------------------------------

    def embed_pending(self, limit: int | None = None) -> int:
        """Embed `ok` rows that lack vectors: the post (pain statement + title + start of the text) for
        niches and retrieval, and the pain statement alone for problem clustering (section 5.1)."""
        if self.embedder is None:
            raise RuntimeError("no embedder configured")
        ok = EnrichedPostRow.status == "ok"
        # Reviews and tool listings stay out of the feed but are searched for existing tools (section 17).
        searchable = ok | ((EnrichedPostRow.status == "dropped")
                           & EnrichedPostRow.post_type.in_(self.config.inferred.embed_dropped_types))
        done = self._embed(select(EnrichedPostRow).where(searchable, EnrichedPostRow.embedding.is_(None)), limit,
                           lambda row, post: embedding_text(row, post, self.config.embeddings.max_input_chars),
                           "embedding")
        self._embed(select(EnrichedPostRow).where(ok, EnrichedPostRow.pain_embedding.is_(None),
                                                  EnrichedPostRow.pain_statement.is_not(None)), limit,
                    lambda row, post: row.pain_statement, "pain_embedding")
        # The task alone, for clustering paid work by what is being done (section 17, Detector 1).
        self._embed(select(EnrichedPostRow).where(ok, EnrichedPostRow.task_embedding.is_(None),
                                                  EnrichedPostRow.task.is_not(None)), limit,
                    lambda row, post: row.task, "task_embedding")
        self._embed_obligations()
        return done

    def _embed_obligations(self) -> int:
        """Obligations from rule documents, as "who: what", for matching them to job duties (Detector 2)."""
        ecfg = self.config.embeddings
        with self.store.session() as s:
            todo = list(s.scalars(select(ObligationRow).where(ObligationRow.embedding.is_(None))))
        for i in range(0, len(todo), ecfg.batch_size):
            chunk = todo[i:i + ecfg.batch_size]
            texts = [f"{o.who}: {o.what}" for o in chunk]
            self.ledger.check("voyage", sum(map(len, texts)) / 2 * ecfg.price_per_mtok / 1_000_000,
                              ecfg.monthly_budget_usd, self.clock())
            result = self.embedder.embed(texts)
            self.ledger.record(provider="voyage", purpose="embed", usage=Usage(result.model, result.total_tokens),
                               usd=result.total_tokens * ecfg.price_per_mtok / 1_000_000, at=self.clock())
            with self.store.session() as s, s.begin():
                for o, vector in zip(chunk, result.vectors, strict=True):
                    s.get(ObligationRow, o.id).embedding = vector
        return len(todo)

    def _embed(self, stmt, limit: int | None, text_of, attr: str) -> int:
        ecfg = self.config.embeddings
        stmt = stmt.order_by(EnrichedPostRow.post_id)
        if limit is not None:
            stmt = stmt.limit(limit)
        with self.store.session() as s:
            rows = list(s.scalars(stmt))
        done = 0
        for i in range(0, len(rows), ecfg.batch_size):
            chunk = rows[i:i + ecfg.batch_size]
            posts = {p.id: p for p in self.posts.get_posts([r.post_id for r in chunk])}
            chunk = [r for r in chunk if r.post_id in posts]
            if not chunk:
                continue
            texts = [text_of(r, posts[r.post_id]) for r in chunk]
            worst = sum(len(t) for t in texts) / 2 * ecfg.price_per_mtok / 1_000_000
            self.ledger.check("voyage", worst, ecfg.monthly_budget_usd, self.clock())
            result = self.embedder.embed(texts)
            self.ledger.record(provider="voyage", purpose="embed", usage=Usage(result.model, result.total_tokens),
                               usd=result.total_tokens * ecfg.price_per_mtok / 1_000_000, at=self.clock())
            with self.store.session() as s, s.begin():
                for row, vector in zip(chunk, result.vectors, strict=True):
                    db_row = s.get(EnrichedPostRow, row.post_id)
                    setattr(db_row, attr, vector)
                    db_row.embedding_model = result.model
            done += len(chunk)
        return done


def embedding_text(row: EnrichedPostRow, post: RawPost, max_chars: int) -> str:
    parts = [row.pain_statement, post.title, post.text]
    return "\n".join(p for p in parts if p)[:max_chars]


def _parse_date(value: str | None):
    """YYYY-MM-DD from the model, or None when it is missing or malformed (never guessed)."""
    from datetime import date
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None
