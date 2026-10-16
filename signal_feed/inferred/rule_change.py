"""Detector 2: rule change x affected role (section 17.3). A new or proposed rule that creates work for
a specific role.

1. Obligations (who must comply, what they must now do, the deadline, the industries) are extracted from
   rule documents at tagging (prompt v9) and embedded as "who: what".
2. Job posts in the same industries are grouped by role (normalized title); a role matches when enough of
   its recent posts describe duties close to the obligation.
3. Candidate when an obligation maps to a role with >= 10 recent matching posts. Its deadline must be
   ahead; an obligation with no stated deadline (most of them) counts while its rule is recent.
3b. Without such a role, the affected people's own posts (pain, tool gaps) close to the obligation, from
   at least `min_voice_posts` different authors, stand in as the role evidence.
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
TEMPLATE_NO_DATE = "{rule} requires {who} to {what}; today this is done by {role} manually."
TEMPLATE_VOICES = "{rule} requires {who} to {what}{by_date}; the people affected already struggle with related work."

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


def _rule_name(post) -> str:
    """The rule's name. A public comment's title names the commenter, not the rule: use its docket."""
    raw = post.raw or {}
    if raw.get("comment_id") and raw.get("docket"):
        return f"{raw.get('agency') or 'The agency'}'s rule on docket {raw['docket']}"
    title = (post.title or "This rule").strip()
    return title if len(title) <= 90 else title[:87].rstrip() + "..."


def _one_per_rule(obligations: list, raw: dict, cosine: float) -> list:
    """The same obligation is extracted from the rule and from every comment and article about it: keep
    one per near-duplicate group (and per docket), preferring the rule's own document over a comment."""
    def rank(o):
        p = raw.get(o.post_id)
        return (bool(p is not None and (p.raw or {}).get("comment_id")), o.deadline is None, o.id)
    kept, vecs, dockets = [], [], set()
    for o in sorted(obligations, key=rank):
        p = raw.get(o.post_id)
        docket = (p.raw or {}).get("docket") if p is not None else None
        v = np.asarray(o.embedding, dtype=np.float32)
        v /= np.linalg.norm(v) or 1
        if (docket and docket in dockets) or any(float(v @ k) >= cosine for k in vecs):
            continue
        kept.append(o)
        vecs.append(v)
        if docket:
            dockets.add(docket)
    return kept


def _date_words(d) -> str:
    return f"{d.day} {d.strftime('%B %Y')}"


def find_candidates(store: FeedStore, posts: PostsReader, config: FeedConfig, now: datetime) -> tuple[list[Candidate], int]:
    """(candidates, obligations looked at)."""
    cfg, icfg = config.inferred.rule_change, config.inferred
    with store.session() as s:
        # The deadline must be ahead; with none stated, the rule must be recent (checked below).
        obligations = [o for o in s.scalars(select(ObligationRow).where(ObligationRow.embedding.is_not(None)))
                       if o.deadline is None or o.deadline >= now.date()]
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
        voices = list(s.scalars(select(EnrichedPostRow).where(EnrichedPostRow.status == "ok",
                                                              EnrichedPostRow.post_type.in_(("pain", "tool_gap")),
                                                              EnrichedPostRow.pain_embedding.is_not(None))))
    if not obligations:
        return [], 0
    raw = {p.id: p for p in posts.get_posts([o.post_id for o in obligations] + [r.post_id for r in jobs]
                                            + [r.post_id for r in catalog] + [r.post_id for r in voices])}
    since = now - timedelta(days=cfg.job_window_days)
    rule_since = now - timedelta(days=cfg.max_rule_age_days)
    obligations = _one_per_rule(obligations, raw, cfg.duplicate_cosine)
    voices = [v for v in voices if v.post_id in raw]
    # Pain statement alone (not the whole post): the closest match to "who must now do what".
    voice_vectors = unit_matrix([v.pain_embedding for v in voices]) if voices else np.zeros((0, 1))

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
        if rule_post is None or (o.deadline is None and rule_post.published_at < rule_since):
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
        heard = []
        if best is None and len(voices):
            # 3b: the affected people's own posts, one per author, not the rule's own post.
            sims = voice_vectors @ ob
            authors = set()
            for k in np.argsort(-sims):
                if sims[k] < cfg.voice_cosine or len(heard) >= cfg.max_voice_evidence:
                    break
                v = voices[k]
                author = raw[v.post_id].author_hash or v.post_id
                if v.post_id == o.post_id or author in authors:
                    continue
                authors.add(author)
                heard.append(v)
            if len(heard) < cfg.min_voice_posts:
                continue
        elif best is None:
            continue
        matching = best[1] if best else heard
        rule_short = _rule_name(rule_post)
        by_date = f" by {_date_words(o.deadline)}" if o.deadline else ""
        evidence = [Evidence(post_id=rule_post.id, record_id=(rule_post.raw or {}).get("document_number"),
                             source=rule_post.source, source_type=rule_post.source_type, url=rule_post.url,
                             what_it_shows=f"{rule_short}: requires {o.who} to {o.what}{by_date}")]
        if best:
            for r in matching[:cfg.max_job_evidence]:
                p = raw[r.post_id]
                evidence.append(Evidence(post_id=p.id, source=p.source, source_type=p.source_type, url=p.url,
                                         what_it_shows=f"{(p.community or 'An employer').strip()} is hiring: {p.title}"))
        else:
            for v in heard:
                p = raw[v.post_id]
                who = f"A {v.who_has_pain}" if v.who_has_pain else "Someone"
                evidence.append(Evidence(post_id=p.id, source=p.source, source_type=p.source_type, url=p.url,
                                         what_it_shows=f"{who} describes a related problem: {v.pain_statement}"))
        dockets = (rule_post.raw or {}).get("docket_ids") or []
        for c in [c for d in dockets for c in comments.get(d, [])][:cfg.max_comment_evidence]:
            evidence.append(Evidence(post_id=c.id, source=c.source, source_type=c.source_type, url=c.url,
                                     what_it_shows=f"A public comment on this rule: {(c.title or '').strip()[:120]}"))
        near_miss = not meets_minimum(evidence, icfg)   # corroboration may complete it
        placed = [leaf[r.post_id] for r in matching if r.post_id in leaf] or \
            ([leaf[o.post_id]] if o.post_id in leaf else [])
        niche = Counter(placed).most_common(1)[0][0] if placed else None
        counters = counter_evidence(ob, cat_vectors, catalog, raw, icfg, exclude={e.post_id for e in evidence})
        if best:
            role = best[0]
            roles = f"{role}s" if not role.endswith("s") else role
            hypothesis = (TEMPLATE.format(rule=rule_short, who=o.who, what=o.what, date=_date_words(o.deadline),
                                          role=roles) if o.deadline else
                          TEMPLATE_NO_DATE.format(rule=rule_short, who=o.who, what=o.what, role=roles))
        else:
            hypothesis = TEMPLATE_VOICES.format(rule=rule_short, who=o.who, what=o.what, by_date=by_date)
        out.append(Candidate(near_miss=near_miss, 
            detector=NAME, task=o.what, who=o.who, niche_id=niche, evidence=evidence, counter_evidence=counters,
            hypothesis=hypothesis, rows=matching, posts=[raw[r.post_id] for r in matching]))
    return out, len(obligations)
