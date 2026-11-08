"""Step 2 of OPPORTUNITIES.md (18.4): job clusters, posts about the same product need.

Founder rule (2026-11-08): the core of every cluster is posts that could be shown on their own (project
score >= 80, a software or device need, sure labels, open deadline, US or matched to a US post:
`opportunity_fields.shown`). Posts scoring 60-79 with a product need are supporting evidence: they attach
to the nearest cluster when close enough, and never form or join the core. Non-US posts may support a
cluster whose core is American.

What is compared is the need as US buyers have it (`us_version`, else `job_statement`), embedded once per
post (`need_embedding`). Cores are clustered by average linkage at `core_cosine`; each supporting post joins
the cluster with the closest core centroid if the cosine reaches `attach_cosine`. Spec 18.4 clusters within
buyer_type + niche; here buyer type isn't a wall (a solo bookkeeper and a small firm want the same PDF
converter) and the borderline LLM check is left for later.
"""

from __future__ import annotations

import logging
import statistics
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime

import numpy as np
from sqlalchemy import delete, select

from signal_connectors.models import RawPost, utcnow

from ..config import FeedConfig
from ..db import EnrichedPostRow, FeedStore, JobClusterPostRow, JobClusterRow
from ..enrichment.costs import CostLedger, Usage
from ..enrichment.opportunity_fields import PRODUCT_TYPES, shown
from ..inferred.common import buyer_key
from ..posts import PostsReader, _chunks
from ..problems.cluster import cluster_niche
from ..vectors import centroid, unit_matrix

log = logging.getLogger(__name__)


def need_text(row: EnrichedPostRow) -> str | None:
    return row.us_version or row.job_statement


def role(row: EnrichedPostRow, source: str, config: FeedConfig, today: date) -> str | None:
    """core | supporting | None."""
    ocfg, ccfg = config.enrichment.opportunity_fields, config.opportunities
    if row.status != "ok" or row.opp_version != ocfg.version or not need_text(row):
        return None
    if shown(row, ocfg, today, source):
        return "core"
    if (row.project_score or 0) >= ccfg.support_min_score and row.project_type in PRODUCT_TYPES \
            and (row.opp_confidence or 0) >= ocfg.min_confidence:
        return "supporting"
    return None


def embed_needs(store: FeedStore, embedder, config: FeedConfig, rows: Sequence[EnrichedPostRow]) -> int:
    """Embed the need of rows that lack `need_embedding` (Voyage, metered)."""
    ecfg, ledger = config.embeddings, CostLedger(store)
    todo = [r for r in rows if r.need_embedding is None]
    for i in range(0, len(todo), ecfg.batch_size):
        chunk = todo[i:i + ecfg.batch_size]
        texts = [need_text(r) for r in chunk]
        ledger.check("voyage", sum(map(len, texts)) / 2 * ecfg.price_per_mtok / 1_000_000, ecfg.monthly_budget_usd)
        result = embedder.embed(texts)
        ledger.record(provider="voyage", purpose="need_embedding", usage=Usage(result.model, result.total_tokens),
                      usd=result.total_tokens * ecfg.price_per_mtok / 1_000_000)
        with store.session() as s, s.begin():
            for r, v in zip(chunk, result.vectors, strict=True):
                s.get(EnrichedPostRow, r.post_id).need_embedding = v
                r.need_embedding = v
    return len(todo)


@dataclass
class Cluster:
    core: list[int]                                   # indexes into the core list
    support: list[tuple[int, float]] = field(default_factory=list)
    centre: np.ndarray | None = None


def _usd(money: dict | None) -> float | None:
    if not money or (money.get("currency") or "USD").upper() != "USD":
        return None
    lo, hi = money.get("amount_min"), money.get("amount_max")
    vals = [v for v in (lo, hi) if isinstance(v, (int, float)) and v > 0]
    return sum(vals) / len(vals) if vals else None


def _price(amounts: list[float]) -> dict | None:
    if not amounts:
        return None
    q = statistics.quantiles(amounts, n=4) if len(amounts) > 1 else [amounts[0]] * 3
    return {"count": len(amounts), "median": statistics.median(amounts), "p25": q[0], "p75": q[2]}


class JobClusterBuilder:
    def __init__(self, store: FeedStore, posts: PostsReader, config: FeedConfig, embedder=None,
                 clock: Callable[[], datetime] = utcnow) -> None:
        self.store, self.posts, self.config, self.embedder, self.clock = store, posts, config, embedder, clock
        self.cfg = config.opportunities

    def _candidates(self) -> tuple[list[EnrichedPostRow], list[EnrichedPostRow], dict[str, RawPost]]:
        ocfg = self.config.enrichment.opportunity_fields
        with self.store.session() as s:
            rows = list(s.scalars(select(EnrichedPostRow).where(
                EnrichedPostRow.status == "ok", EnrichedPostRow.opp_version == ocfg.version,
                EnrichedPostRow.project_score >= self.cfg.support_min_score)))
            for r in rows:
                s.expunge(r)
        raw = {p.id: p for part in _chunks([r.post_id for r in rows]) for p in self.posts.get_posts(part)}
        today = self.clock().date()
        core, support = [], []
        for r in rows:
            if r.post_id not in raw:
                continue
            match role(r, raw[r.post_id].source, self.config, today):
                case "core":
                    core.append(r)
                case "supporting":
                    support.append(r)
        return core, support, raw

    def build(self) -> list[JobClusterRow]:
        core, support, raw = self._candidates()
        if self.embedder is not None:
            embed_needs(self.store, self.embedder, self.config, core + support)
        core = [r for r in core if r.need_embedding is not None]
        support = [r for r in support if r.need_embedding is not None]
        clusters: list[Cluster] = []
        if core:
            cu = unit_matrix([r.need_embedding for r in core])
            clusters = [Cluster(core=list(ix)) for ix in cluster_niche(cu, self.cfg.core_cosine)]
            for c in clusters:
                c.centre = centroid(cu[c.core])
            if support:
                centres = np.stack([c.centre for c in clusters])
                su = unit_matrix([r.need_embedding for r in support])
                sims = su @ centres.T
                for i, row in enumerate(sims):
                    k = int(row.argmax())
                    if row[k] >= self.cfg.attach_cosine:
                        clusters[k].support.append((i, float(row[k])))
        out = []
        now = self.clock()
        with self.store.session() as s, s.begin():
            s.execute(delete(JobClusterPostRow))
            s.execute(delete(JobClusterRow))
            for c in clusters:
                cores = [core[i] for i in c.core]
                sups = [(support[i], cos) for i, cos in c.support]
                members = cores + [r for r, _ in sups]
                posts_ = [raw[r.post_id] for r in members]
                best = max(cores, key=lambda r: (r.project_score or 0, r.opp_confidence or 0))
                recurring = [r.recurrence for r in members if r.recurrence]
                row = JobClusterRow(
                    need=need_text(best), project_idea=best.project_idea, job_statement=best.job_statement,
                    buyer_type=Counter(r.buyer_type for r in cores).most_common(1)[0][0],
                    buyer_segment=best.buyer_segment, core_posts=len(cores), supporting_posts=len(sups),
                    distinct_buyers=len({buyer_key(p) for p in posts_}),
                    source_types=sorted({p.source_type for p in posts_}),
                    sources=dict(Counter(p.source.split(":")[0] for p in posts_).most_common()),
                    price=_price([v for r in members if (v := _usd(r.money_signal)) is not None]),
                    recurring_share=(sum(x == "recurring" for x in recurring) / len(recurring)) if recurring else None,
                    best_score=best.project_score or 0,
                    first_seen=min(p.published_at for p in posts_), last_seen=max(p.published_at for p in posts_),
                    built_at=now)
                s.add(row)
                s.flush()
                cu = unit_matrix([r.need_embedding for r in cores])
                for r, v in zip(cores, cu, strict=True):
                    s.add(JobClusterPostRow(cluster_id=row.id, post_id=r.post_id, role="core", cosine=float(v @ c.centre)))
                for r, cos in sups:
                    s.add(JobClusterPostRow(cluster_id=row.id, post_id=r.post_id, role="supporting", cosine=cos))
                out.append(row)
        log.info("built %d job clusters from %d core and %d supporting candidates", len(out), len(core), len(support))
        return out
