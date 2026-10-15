"""Detector 4: labor pressure (section 17.3). Rising demand or pay for a role whose duties are mostly
routine. Per role x industry, job posts in the last 90 days are compared with the 90 before; the routine
share is the mean `task_automatable` the tagger gave the role's posts (v9). Supporting evidence: posts
where people in that role describe their own pain. Starts as a `trial` detector (17.11 step 5)."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from statistics import median

from sqlalchemy import select

from ..config import FeedConfig
from ..db import EnrichedPostRow, FeedStore, NicheRow, PostNicheRow
from ..posts import PostsReader
from .common import Candidate, Evidence, meets_minimum
from .rule_change import role_of

NAME = "labor_pressure"
TEMPLATE = "Demand for {role} is rising; {duties} are candidates for automation."


def _pay(row: EnrichedPostRow) -> float | None:
    m = row.money_signal or {}
    if m.get("kind") not in ("salary", "rate") or not m.get("amount_min"):
        return None
    return (m["amount_min"] + (m.get("amount_max") or m["amount_min"])) / 2


def find_candidates(store: FeedStore, posts: PostsReader, config: FeedConfig, now: datetime) -> tuple[list[Candidate], int]:
    cfg, icfg, jobs_cfg = config.inferred.labor_pressure, config.inferred, config.inferred.rule_change
    window = timedelta(days=cfg.window_days)
    with store.session() as s:
        jobs = list(s.scalars(select(EnrichedPostRow).where(EnrichedPostRow.status == "ok",
                                                            EnrichedPostRow.post_type == "paid_demand")))
        voices = list(s.scalars(select(EnrichedPostRow).where(EnrichedPostRow.status == "ok",
                                                              EnrichedPostRow.post_type.in_(("pain", "tool_gap")),
                                                              EnrichedPostRow.who_has_pain.is_not(None))))
        leaf = dict(s.execute(select(PostNicheRow.post_id, PostNicheRow.niche_id)
                              .where(PostNicheRow.tree == "industry")).all())
        parents = dict(s.execute(select(NicheRow.id, NicheRow.parent_id)).all())
    raw = {p.id: p for p in posts.get_posts([r.post_id for r in jobs] + [r.post_id for r in voices])}

    def root(n):
        while n is not None and parents.get(n) is not None:
            n = parents[n]
        return n

    groups: dict[tuple[int, str], dict[str, list[EnrichedPostRow]]] = defaultdict(lambda: {"now": [], "before": []})
    for r in jobs:
        p = raw.get(r.post_id)
        if p is None or r.post_id not in leaf:
            continue
        if p.source.split(":")[0] not in jobs_cfg.job_sources and (p.community or "") not in jobs_cfg.job_communities:
            continue
        role = role_of(p.title)
        age = now - p.published_at
        if role and age <= 2 * window:
            groups[(root(leaf[r.post_id]), role)]["now" if age <= window else "before"].append(r)
    out = []
    for (industry, role), g in groups.items():
        cur, prev = g["now"], g["before"]
        if len(cur) < cfg.min_current_posts:
            continue
        volume_rise = (len(cur) - len(prev)) / len(prev) if prev else None
        pay_now, pay_before = [x for x in map(_pay, cur) if x], [x for x in map(_pay, prev) if x]
        pay_rise = (median(pay_now) - median(pay_before)) / median(pay_before) if pay_now and pay_before else None
        rising = (volume_rise is not None and volume_rise >= cfg.min_rise) or (pay_rise is not None and pay_rise >= cfg.min_rise)
        rated = [r.task_automatable for r in cur if r.task_automatable is not None]
        if not rising or not rated or sum(rated) / len(rated) < cfg.min_routine:
            continue
        duties = [t for t, _ in Counter(r.task for r in cur if r.task).most_common(2)]
        evidence = [Evidence(post_id=r.post_id, source=raw[r.post_id].source, source_type=raw[r.post_id].source_type,
                             url=raw[r.post_id].url,
                             what_it_shows=f"{(raw[r.post_id].community or 'An employer').strip()} is hiring: "
                                           f"{raw[r.post_id].title}") for r in cur[:8]]
        # The role's own voice: people in it describing the work (another source type).
        for v in voices:
            if role in (v.who_has_pain or "").lower() and v.post_id in raw and leaf.get(v.post_id) and \
                    root(leaf[v.post_id]) == industry:
                evidence.append(Evidence(post_id=v.post_id, source=raw[v.post_id].source,
                                         source_type=raw[v.post_id].source_type, url=raw[v.post_id].url,
                                         what_it_shows=f"A {v.who_has_pain} describes the work: {v.pain_statement}"))
                if len(evidence) >= 12:
                    break
        if not meets_minimum(evidence, icfg):
            continue
        change = []
        if volume_rise is not None and volume_rise >= cfg.min_rise:
            change.append(f"{len(cur)} job posts in the last {cfg.window_days} days vs {len(prev)} before")
        if pay_rise is not None and pay_rise >= cfg.min_rise:
            change.append(f"median stated pay up {round(100 * pay_rise)}%")
        # A computed fact about the posts above: shown and given to the critic, never counted as evidence.
        evidence.insert(0, Evidence(post_id=None, record_id=f"jobs:{industry}:{role}", source="job posts (counted)",
                                    source_type="paid_demand", url="", what_it_shows="; ".join(change), counts=False))
        roles = role if role.endswith("s") else role + "s"
        out.append(Candidate(detector=NAME, task=duties[0] if duties else None, who=role,
                             niche_id=Counter(leaf[r.post_id] for r in cur).most_common(1)[0][0], evidence=evidence,
                             hypothesis=TEMPLATE.format(role=roles, duties=" and ".join(duties) or "their routine duties"),
                             rows=cur, posts=[raw[r.post_id] for r in cur]))
    return out, len(groups)
