"""Detector 6: cross-industry transfer (section 17.3). A workflow well served by software in one industry
and done by hand in a similar one. The "by hand" side is Detector 1's workaround evidence (paid task
clusters, `task_clusters`); the "served" side is tool posts (reviews, listings, launches, tool complaints)
close to the same task in another industry, with none close to it in the workaround's own industry.
Starts as a `trial` detector (17.11 step 5)."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

import numpy as np
from sqlalchemy import select

from ..config import FeedConfig
from ..db import EnrichedPostRow, FeedStore, NicheRow, PostNicheRow, TaskClusterRow
from ..posts import PostsReader
from ..vectors import centroid, unit_matrix
from .common import Candidate, Evidence, buyer_label, meets_minimum
from .paid_workarounds import SEARCHABLE

NAME = "cross_industry"
TEMPLATE = "{task} has good tools in {served} but {unserved} still does it by hand."


def find_candidates(store: FeedStore, posts: PostsReader, config: FeedConfig, now: datetime) -> tuple[list[Candidate], int]:
    cfg, icfg, d1 = config.inferred.cross_industry, config.inferred, config.inferred.paid_workarounds
    with store.session() as s:
        clusters = list(s.scalars(select(TaskClusterRow).where(TaskClusterRow.qualifies.is_(True))))
        tools = list(s.scalars(select(EnrichedPostRow).where(EnrichedPostRow.post_type.in_(SEARCHABLE),
                                                             EnrichedPostRow.embedding.is_not(None))))
        members = {r.post_id: r for r in s.scalars(select(EnrichedPostRow).where(
            EnrichedPostRow.post_id.in_({p for c in clusters for p in c.post_ids})))}
        leaf = dict(s.execute(select(PostNicheRow.post_id, PostNicheRow.niche_id)
                              .where(PostNicheRow.tree == "industry")).all())
        nodes = {n.id: n for n in s.scalars(select(NicheRow).where(NicheRow.tree == "industry"))}
    raw = {p.id: p for p in posts.get_posts(list(members) + [r.post_id for r in tools])}

    def root(n):
        while n is not None and nodes.get(n) is not None and nodes[n].parent_id is not None:
            n = nodes[n].parent_id
        return n

    tool_vecs = unit_matrix([r.embedding for r in tools]) if tools else np.zeros((0, 1))
    out = []
    for c in clusters:
        rows = [members[p] for p in c.post_ids if p in members and raw.get(p) is not None]
        vecs = [r.task_embedding or r.pain_embedding for r in rows if r.task_embedding or r.pain_embedding]
        if len(rows) < d1.min_items or not vecs or not len(tools):
            continue
        centre = centroid(unit_matrix(vecs))
        home = root(c.niche_id)
        sims = tool_vecs @ centre
        by_industry: dict[int, list[int]] = defaultdict(list)
        for k in np.flatnonzero(sims >= cfg.task_cosine):
            ind = root(leaf.get(tools[k].post_id))
            if ind is not None and tools[k].post_id in raw:
                by_industry[ind].append(int(k))
        if by_industry.get(home):
            continue                          # the workaround's own industry already has tools: not a transfer
        served = max(by_industry.items(), key=lambda kv: len(kv[1]), default=(None, []))
        if served[0] is None or len(served[1]) < cfg.min_tool_posts:
            continue
        evidence = [Evidence(post_id=r.post_id, source=raw[r.post_id].source, source_type=raw[r.post_id].source_type,
                             url=raw[r.post_id].url,
                             what_it_shows=f"{buyer_label(raw[r.post_id], r)} pays for: {c.task}") for r in rows[:6]]
        for k in served[1][:6]:
            t = tools[k]
            evidence.append(Evidence(post_id=t.post_id, source=raw[t.post_id].source,
                                     source_type=raw[t.post_id].source_type, url=raw[t.post_id].url,
                                     what_it_shows=f"In {nodes[served[0]].label}, a tool for this: "
                                                   f"{', '.join(t.tools_mentioned or []) or raw[t.post_id].title}"))
        near_miss = not meets_minimum(evidence, icfg)   # corroboration may complete it
        task = c.task[:1].upper() + c.task[1:]
        out.append(Candidate(near_miss=near_miss, detector=NAME, task=c.task, who=None, niche_id=c.niche_id, evidence=evidence,
                             hypothesis=TEMPLATE.format(task=task, served=nodes[served[0]].label,
                                                        unserved=nodes[home].label if home in nodes else "another industry"),
                             rows=rows, posts=[raw[r.post_id] for r in rows]))
    return out, len(clusters)
