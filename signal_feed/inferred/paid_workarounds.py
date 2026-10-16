"""Detector 1: paid workarounds (section 17.3). People paying a human, or buying a template, to do a
repetitive task. Within each niche, paid items are clustered by task; a cluster with enough paid items from
enough different buyers in the window becomes a candidate.

Inputs today: tenders (SAM.gov, TED, UK), job posts (job_boards) and bounties (GitHub). Template
marketplaces and automation-template listings are named by the spec but no connector collects them yet.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta

import numpy as np
from sqlalchemy import select

from ..config import FeedConfig
from ..db import EnrichedPostRow, FeedStore, PostNicheRow, TaskClusterRow
from ..posts import PostsReader
from ..problems.cluster import cluster_niche
from ..vectors import centroid, unit_matrix
from .common import Candidate, Evidence, buyer_key, buyer_label, counter_evidence, meets_minimum, plural, tender_kind

NAME = "paid_workarounds"
TEMPLATE = "{who} pay people to {task} by hand because no tool does it well."
SEARCHABLE = ("praise", "listing", "tool_gap", "change")

# "The council needs a contractor to service its fleet" -> "service its fleet" (pain statements only;
# v9 tags carry a real task and skip this).
_LEAD = re.compile(r"^.*?\b(?:needs?|wants?|is seeking|seeks|is looking for|is tendering for|is procuring|"
                   r"is buying|requires?|is soliciting (?:bids|offers|proposals|quotes)|invites (?:bids|tenders))\b"
                   r"(?:\s+(?:a|an|the|from)\s+[\w\- ]+?)?\s+(?:to|for)\s+", re.IGNORECASE)


def derived_task(pain_statement: str | None) -> str | None:
    if not pain_statement:
        return None
    text = _LEAD.sub("", pain_statement.strip().rstrip("."), count=1)
    return text[:1].lower() + text[1:] if text else None


def find_candidates(store: FeedStore, posts: PostsReader, config: FeedConfig, now: datetime) -> tuple[list[Candidate], int]:
    """(candidates, clusters looked at). Clusters are also written to `task_clusters`."""
    cfg, icfg = config.inferred.paid_workarounds, config.inferred
    since = now - timedelta(days=cfg.window_days)
    with store.session() as s:
        paid = list(s.scalars(select(EnrichedPostRow).where(EnrichedPostRow.status == "ok",
                                                            EnrichedPostRow.post_type == "paid_demand")))
        catalog = list(s.scalars(select(EnrichedPostRow).where(EnrichedPostRow.post_type.in_(SEARCHABLE),
                                                               EnrichedPostRow.embedding.is_not(None))))
        leaf = dict(s.execute(select(PostNicheRow.post_id, PostNicheRow.niche_id)
                              .where(PostNicheRow.tree == "industry")).all())
    raw = {p.id: p for p in posts.get_posts([r.post_id for r in paid] + [r.post_id for r in catalog])}
    groups: dict[int, list[tuple[EnrichedPostRow, str, list[float], bool]]] = defaultdict(list)
    for row in paid:
        post = raw.get(row.post_id)
        if post is None or post.published_at < since or row.post_id not in leaf:
            continue
        if row.task and row.task_embedding:
            if row.task_automatable is not None and row.task_automatable < cfg.min_automatable:
                continue                      # repairs, construction: not something software does
            groups[leaf[row.post_id]].append((row, row.task, row.task_embedding, False))
        elif cfg.allow_derived_tasks and row.pain_embedding:
            task = derived_task(row.pain_statement)
            if task:
                groups[leaf[row.post_id]].append((row, task, row.pain_embedding, True))
    cat_vectors = unit_matrix([r.embedding for r in catalog]) if catalog else np.zeros((0, 1))
    out, clusters = [], []
    for niche_id, items in groups.items():
        unit = unit_matrix([v for _, _, v, _ in items])
        for idx in cluster_niche(unit, cfg.cluster_cosine):
            members = [items[i] for i in idx]
            member_posts = [raw[r.post_id] for r, _, _, _ in members]
            buyers = {buyer_key(p) for p in member_posts}
            centre = centroid(unit[idx])
            medoid = members[int(np.argmax(unit[idx] @ centre))]
            qualifies = len(members) >= cfg.min_items and len(buyers) >= cfg.min_buyers
            clusters.append(TaskClusterRow(niche_id=niche_id, task=medoid[1], post_ids=[r.post_id for r, *_ in members],
                                           buyers=len(buyers), qualifies=qualifies, computed_at=now))
            if not qualifies:
                continue
            evidence = []
            for (row, task, _, _), post in zip(members, member_posts):
                money = f" ({row.money_signal['quote']})" if row.money_signal and row.money_signal.get("quote") else ""
                evidence.append(Evidence(post_id=row.post_id, source=post.source, source_type=post.source_type,
                                         url=post.url, what_it_shows=f"{buyer_label(post, row)} pays for: {task}{money}"))
            near_miss = not meets_minimum(evidence, icfg)   # corroboration may complete it
            kinds = [k for k in map(tender_kind, member_posts) if k]
            if near_miss and kinds and sum(k == "labor" for k in kinds) * 2 < len(kinds):
                continue                      # mostly buying software or goods: not worth a planning call
            roles = Counter(r.who_has_pain for r, *_ in members if r.who_has_pain)
            who = roles.most_common(1)[0][0] if roles else "buyers"
            post_centre = centroid(unit_matrix([r.embedding for r, *_ in members if r.embedding])) \
                if any(r.embedding for r, *_ in members) else None
            counters = [] if post_centre is None else counter_evidence(
                post_centre, cat_vectors, catalog, raw, icfg, exclude={r.post_id for r, *_ in members})
            tools = Counter(t for r, *_ in members for t in (r.tools_mentioned or []))
            if tools:
                counters.append({"post_id": None, "source": "buyers' own posts", "url": None,
                                 "what_it_shows": "Tools the buyers name: " + ", ".join(t for t, _ in tools.most_common(5))})
            out.append(Candidate(near_miss=near_miss, detector=NAME, hypothesis=TEMPLATE.format(who=plural(who), task=medoid[1]), who=who,
                                 task=medoid[1], niche_id=niche_id, evidence=evidence, counter_evidence=counters,
                                 derived=any(d for *_, d in members), rows=[r for r, *_ in members],
                                 posts=member_posts))
    with store.session() as s, s.begin():
        s.query(TaskClusterRow).delete()
        s.add_all(clusters)
    return out, len(clusters)
