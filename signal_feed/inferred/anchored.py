"""Anchor-first detector (2026-10-15). Starts from one authoritative fact instead of waiting for a cluster:
a platform or vendor change, a recall, a launch (`change` posts outside the rule sources, which Detector 2
reads through their obligations), or a tender paying people for routine information work. The planning
call (corroborate.py) decides whether the fact creates routine work for a specific group and writes the
claim; corroboration is the affected people's own posts, from the corpus (pain statements close to the
anchor) and from a targeted X search. The usual evidence minimum, critic and confidence then apply.

Anchors already planned come first, so cached plans keep their candidates alive at no cost; new anchors
are planned up to `corroboration.max_plans_per_run`. Starts as a `trial` detector (insider prompts only).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
from sqlalchemy import select

from ..config import FeedConfig
from ..db import CorroborationRow, EnrichedPostRow, FeedStore, PostNicheRow, TaskClusterRow
from ..posts import PostsReader
from ..vectors import unit_matrix
from .common import Candidate, Evidence, tender_kind
from .corroborate import candidate_key

NAME = "anchored"


def _short(text: str | None, n: int = 160) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 3].rstrip() + "..."


def find_candidates(store: FeedStore, posts: PostsReader, config: FeedConfig, now: datetime) -> tuple[list[Candidate], int]:
    cfg = config.inferred.anchored
    since = now - timedelta(days=cfg.window_days)
    with store.session() as s:
        changes = list(s.scalars(select(EnrichedPostRow).where(
            EnrichedPostRow.status == "ok", EnrichedPostRow.post_type == "change",
            EnrichedPostRow.embedding.is_not(None), EnrichedPostRow.obligation_ids.is_(None))))
        tenders = list(s.scalars(select(EnrichedPostRow).where(
            EnrichedPostRow.status == "ok", EnrichedPostRow.post_type == "paid_demand",
            EnrichedPostRow.embedding.is_not(None), EnrichedPostRow.task.is_not(None),
            EnrichedPostRow.task_automatable >= cfg.min_automatable)))
        voices = list(s.scalars(select(EnrichedPostRow).where(
            EnrichedPostRow.status == "ok", EnrichedPostRow.post_type.in_(("pain", "tool_gap")),
            EnrichedPostRow.pain_embedding.is_not(None))))
        clustered = {p for c in s.scalars(select(TaskClusterRow).where(TaskClusterRow.qualifies.is_(True)))
                     for p in c.post_ids}                      # Detector 1 already covers these
        planned = set(s.scalars(select(CorroborationRow.key).where(CorroborationRow.detector == NAME)))
        leaf = dict(s.execute(select(PostNicheRow.post_id, PostNicheRow.niche_id)
                              .where(PostNicheRow.tree == "industry")).all())
    raw = {p.id: p for p in posts.get_posts([r.post_id for r in changes + tenders + voices])}

    def recent(r, sources_ok):
        p = raw.get(r.post_id)
        return p is not None and p.published_at >= since and sources_ok(p.source.split(":")[0])

    titled = lambda r: bool((raw[r.post_id].title or "").strip())    # noqa: E731  (an untitled post isn't a fact)
    anchors = [r for r in changes if recent(r, lambda src: src not in cfg.skip_sources) and titled(r)]
    anchors += [r for r in tenders if r.post_id not in clustered
                and recent(r, lambda src: src in cfg.labor_tender_sources)
                and tender_kind(raw[r.post_id]) == "labor"]           # buying software or goods: not an anchor
    voices = [v for v in voices if v.post_id in raw]
    vvec = unit_matrix([v.pain_embedding for v in voices]) if voices else np.zeros((0, 1))

    out = []
    for a in anchors:
        p = raw[a.post_id]
        what = f"{_short(p.title) or 'A notice'}" + (f" (task: {a.task})" if a.post_type == "paid_demand" and a.task else "")
        evidence = [Evidence(post_id=p.id, source=p.source, source_type=p.source_type, url=p.url, what_it_shows=what)]
        rows, cposts = [a], [p]
        if len(voices):
            vec = np.asarray(a.embedding, dtype=np.float32)
            sims = vvec @ (vec / (np.linalg.norm(vec) or 1))
            authors = set()
            for k in np.argsort(-sims):
                if sims[k] < cfg.voice_cosine or len(rows) > cfg.max_voice_evidence:
                    break
                v, vp = voices[k], raw[voices[k].post_id]
                author = vp.author_hash or vp.id
                if vp.id == p.id or author in authors:
                    continue
                authors.add(author)
                who = f"A {v.who_has_pain}" if v.who_has_pain else "Someone"
                evidence.append(Evidence(post_id=vp.id, source=vp.source, source_type=vp.source_type, url=vp.url,
                                         what_it_shows=f"{who} describes a related problem: {v.pain_statement}"))
                rows.append(v)
                cposts.append(vp)
        cand = Candidate(detector=NAME, hypothesis=_short(f"{p.title}. {a.pain_statement or ''}", 400), who=None,
                         task=a.task, niche_id=leaf.get(p.id), evidence=evidence, rows=rows, posts=cposts,
                         anchor=True, near_miss=True,
                         texts={p.id: _short(p.text, 600)})
        out.append(cand)
    # Planned anchors always come back (free). New ones: those the affected people already talk about, then
    # the newest; a quarter of the slots go to labor tenders, which rarely have corpus voices yet.
    rank = lambda c: (-len(c.evidence), -c.posts[0].published_at.timestamp())   # noqa: E731
    again = [c for c in out if candidate_key(c) in planned]
    new = sorted((c for c in out if candidate_key(c) not in planned), key=rank)
    tender_slots = cfg.max_anchors_per_run // 4
    fresh_tenders = [c for c in new if c.rows[0].post_type == "paid_demand"][:tender_slots]
    fresh_changes = [c for c in new if c.rows[0].post_type != "paid_demand"]
    fresh_changes = fresh_changes[: cfg.max_anchors_per_run - len(fresh_tenders)]
    return again + sorted(fresh_changes + fresh_tenders, key=rank), len(anchors)
