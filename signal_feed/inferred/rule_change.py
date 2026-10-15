"""Detector 2: rule change x affected role (section 17.3). A new or proposed rule that creates work for
a specific role.

1. Obligations (who must comply, what they must now do, the deadline, the industries) are extracted from
   rule documents at tagging (prompt v9) and embedded as "who: what".
2. Job posts in the same industries are grouped by role (normalized title); a role matches when enough of
   its recent posts describe duties close to the obligation.
3. Candidate when an obligation with a future deadline maps to a role with >= 10 recent matching posts.
4. Public comments on the rule (Regulations.gov comments on its docket) join as supporting evidence.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta

import numpy as np
from sqlalchemy import select

from ..config import FeedConfig
from ..db import EnrichedPostRow, FeedStore, NicheRow, ObligationRow, PostNicheRow
from ..posts import PostsReader
from ..vectors import unit_matrix
from .common import Candidate, Evidence, counter_evidence, meets_minimum
from .paid_workarounds import SEARCHABLE

NAME = "rule_change"
TEMPLATE = "{rule} requires {who} to {what} by {date}; today this is done by {role} manually."

_SENIORITY = re.compile(r"\b(senior|sr\.?|junior|jr\.?|lead|principal|staff|head of|chief|associate|assistant|"
                        r"intern|i{1,3}|iv|[0-9])\b", re.IGNORECASE)


def role_of(title: str | None) -> str | None:
    """'Senior Payroll Specialist (Remote) - Austin, TX' -> 'payroll specialist'."""
    if not title:
        return None
    t = re.split(r"\s[-|–]\s|\(|,", title)[0]
    t = _SENIORITY.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip().lower()
    return t or None


def _date_words(d) -> str:
    return f"{d.day} {d.strftime('%B %Y')}"


def find_candidates(store: FeedStore, posts: PostsReader, config: FeedConfig, now: datetime) -> tuple[list[Candidate], int]:
    """(candidates, obligations looked at)."""
    cfg, icfg = config.inferred.rule_change, config.inferred
    with store.session() as s:
        obligations = [o for o in s.scalars(select(ObligationRow).where(ObligationRow.embedding.is_not(None)))
                       if o.deadline is not None and o.deadline >= now.date()]       # the deadline must be ahead
        roots = {n.label.lower(): n.id for n in s.scalars(select(NicheRow).where(NicheRow.tree == "industry",
                                                                                 NicheRow.parent_id.is_(None)))}
        parents = dict(s.execute(select(NicheRow.id, NicheRow.parent_id)).all())
        leaf = dict(s.execute(select(PostNicheRow.post_id, PostNicheRow.niche_id)
                              .where(PostNicheRow.tree == "industry")).all())
        rule_rows = {r.post_id: r for r in s.scalars(select(EnrichedPostRow).where(
            EnrichedPostRow.post_id.in_({o.post_id for o in obligations})))}
        jobs = list(s.scalars(select(EnrichedPostRow).where(EnrichedPostRow.status == "ok",
                                                            EnrichedPostRow.post_type == "paid_demand",
                                                            EnrichedPostRow.embedding.is_not(None))))
        catalog = list(s.scalars(select(EnrichedPostRow).where(EnrichedPostRow.post_type.in_(SEARCHABLE),
                                                               EnrichedPostRow.embedding.is_not(None))))
    if not obligations:
        return [], 0
    raw = {p.id: p for p in posts.get_posts([o.post_id for o in obligations] + [r.post_id for r in jobs]
                                            + [r.post_id for r in catalog])}
    since = now - timedelta(days=cfg.job_window_days)

    def root_of(niche: int | None) -> int | None:
        while niche is not None and parents.get(niche) is not None:
            niche = parents[niche]
        return niche

    # Job posts by (industry root, role).
    by_role: dict[tuple[int, str], list[EnrichedPostRow]] = defaultdict(list)
    for r in jobs:
        p = raw.get(r.post_id)
        if p is None or p.published_at < since or r.post_id not in leaf:
            continue
        if p.source.split(":")[0] not in cfg.job_sources and (p.community or "") not in cfg.job_communities:
            continue
        role = role_of(p.title)
        if role:
            by_role[(root_of(leaf[r.post_id]), role)].append(r)
    # Comments by docket (Regulations.gov comments name their docket; rules list theirs).
    comments: dict[str, list] = defaultdict(list)
    for p in posts.posts_from_source("regulations_gov"):
        if (p.raw or {}).get("docket"):
            comments[p.raw["docket"]].append(p)
    cat_vectors = unit_matrix([r.embedding for r in catalog]) if catalog else np.zeros((0, 1))
    out = []
    for o in obligations:
        rule_post = raw.get(o.post_id)
        if rule_post is None:
            continue
        industries = [roots[i.lower()] for i in o.industries if i.lower() in roots]
        if not industries and o.post_id in rule_rows and rule_rows[o.post_id].industry_path:
            top = rule_rows[o.post_id].industry_path[0].lower()
            industries = [roots[top]] if top in roots else []
        ob = np.asarray(o.embedding, dtype=np.float32)
        ob /= np.linalg.norm(ob) or 1
        best = None
        for (root, role), rows in by_role.items():
            if root not in industries:
                continue
            sims = unit_matrix([r.embedding for r in rows]) @ ob
            matching = [rows[k] for k in np.argsort(-sims) if sims[k] >= cfg.role_cosine]
            if len(matching) >= cfg.min_role_posts and (best is None or len(matching) > len(best[1])):
                best = (role, matching)
        if best is None:
            continue
        role, matching = best
        rule_title = (rule_post.title or "This rule").strip()
        rule_short = rule_title if len(rule_title) <= 90 else rule_title[:87].rstrip() + "..."
        evidence = [Evidence(post_id=rule_post.id, record_id=(rule_post.raw or {}).get("document_number"),
                             source=rule_post.source, source_type=rule_post.source_type, url=rule_post.url,
                             what_it_shows=f"{rule_short}: requires {o.who} to {o.what} by {_date_words(o.deadline)}")]
        for r in matching[:cfg.max_job_evidence]:
            p = raw[r.post_id]
            evidence.append(Evidence(post_id=p.id, source=p.source, source_type=p.source_type, url=p.url,
                                     what_it_shows=f"{(p.community or 'An employer').strip()} is hiring: {p.title}"))
        dockets = (rule_post.raw or {}).get("docket_ids") or []
        for c in [c for d in dockets for c in comments.get(d, [])][:cfg.max_comment_evidence]:
            evidence.append(Evidence(post_id=c.id, source=c.source, source_type=c.source_type, url=c.url,
                                     what_it_shows=f"A public comment on this rule: {(c.title or '').strip()[:120]}"))
        if not meets_minimum(evidence, icfg):
            continue
        niche = Counter(leaf[r.post_id] for r in matching).most_common(1)[0][0]
        counters = counter_evidence(ob, cat_vectors, catalog, raw, icfg, exclude={e.post_id for e in evidence})
        out.append(Candidate(
            detector=NAME, task=o.what, who=o.who, niche_id=niche, evidence=evidence, counter_evidence=counters,
            hypothesis=TEMPLATE.format(rule=rule_short, who=o.who, what=o.what, date=_date_words(o.deadline),
                                       role=f"{role}s" if not role.endswith("s") else role),
            rows=matching, posts=[raw[r.post_id] for r in matching]))
    return out, len(obligations)
