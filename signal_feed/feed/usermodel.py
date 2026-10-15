"""The user model (section 6): Beta affinities per niche and skill, quiz priors, event updates
with hierarchy propagation and decay, and the per-user feed preferences.

An affinity is stored as Beta(alpha, beta) including its prior; its value is alpha / (alpha + beta).
Evidence (what events added on top of the prior) halves every `evidence_half_life_days`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select

from signal_connectors.models import utcnow

from ..config import FeedConfig
from ..db import (
    EnrichedPostRow,
    EventRow,
    FeedStore,
    InferredProblemRow,
    NicheRow,
    PostNicheRow,
    ProblemPostRow,
    ProblemRow,
    UserDropInRow,
    UserMapNodeRow,
    UserNicheAffinityRow,
    UserPinRow,
    UserQuizRow,
    UserRemovedRow,
    UserRow,
    UserSkillAffinityRow,
)
from ..niches.tree import INDUSTRY, SKILL
from ..posts import PostsReader
from .usermap import ON_MAP, add_to_map

POSITIVE_ACTIONS = ("save", "share", "id_build", "idea", "outreach", "interested")


class UnknownNodeError(ValueError):
    pass


def _resolve(s, tree: str, label: str) -> int:
    """'Healthcare' or 'Healthcare > Pharmacy' (any depth) -> active node id."""
    parent = None
    node = None
    for part in [p.strip() for p in label.split(">")]:
        q = select(NicheRow).where(NicheRow.tree == tree, NicheRow.status == "active",
                                   func.lower(NicheRow.label) == part.lower())
        q = q.where(NicheRow.parent_id.is_(None)) if parent is None else q.where(NicheRow.parent_id == parent)
        node = s.scalars(q).first()
        if node is None:
            raise UnknownNodeError(f"unknown {tree} niche: {label}")
        parent = node.id
    return node.id


def onboard(store: FeedStore, config: FeedConfig, industries: Sequence[str], skills: Sequence[str],
            resume_skills: Sequence[str] = (), user_id: str | None = None,
            clock: Callable[[], datetime] = utcnow, works_in: Sequence[str] = ()) -> str:
    """Create a user from the quiz and seed priors (section 6.2 step 1). Raises UnknownNodeError for a
    label that isn't in the tree."""
    um = config.user_model
    with store.session() as s:
        niche_ids = [_resolve(s, INDUSTRY, label) for label in industries]
        skill_ids = [_resolve(s, SKILL, label) for label in skills]
        resume_ids = [_resolve(s, SKILL, label) for label in resume_skills]
        for label in works_in:                    # checked like the rest; stored as labels (17.7 insiders)
            _resolve(s, INDUSTRY, label)
    uid = store.create_user(user_id)
    now = clock()
    priors = {f"n{n}": um.quiz_prior_affinity for n in niche_ids}
    priors |= {f"s{n}": um.resume_prior_affinity for n in resume_ids}
    priors |= {f"s{n}": um.quiz_prior_affinity for n in skill_ids}
    with store.session() as s, s.begin():
        s.add(UserQuizRow(user_id=uid, industries=list(industries), skills=list(skills),
                          resume_skills=list(resume_skills), answered_at=now, works_in=list(works_in) or None))
        user = s.get(UserRow, uid)
        user.feed_prefs = {"priors": priors}
        for key, mean in priors.items():
            row_cls = UserNicheAffinityRow if key[0] == "n" else UserSkillAffinityRow
            s.add(row_cls(user_id=uid, node_id=int(key[1:]), alpha=mean * um.prior_strength,
                          beta=(1 - mean) * um.prior_strength, updated_at=now))
        s.flush()
        for n in niche_ids:
            add_to_map(s, uid, n, "quiz", config, now)
    return uid


@dataclass
class UserState:
    """Everything ranking needs about one user, with evidence decayed to `now`."""

    user_id: str
    niche: dict[int, float] = field(default_factory=dict)        # node -> affinity 0-1 (rows only)
    niche_uncertainty: dict[int, tuple[float, float]] = field(default_factory=dict)   # node -> (alpha, beta)
    skill: dict[int, float] = field(default_factory=dict)
    map_nodes: set[int] = field(default_factory=set)               # on the map (active, context, fading)
    pins: dict[int, float] = field(default_factory=dict)
    drop_ins: dict[int, int] = field(default_factory=dict)       # node -> exposures left
    removed: set[int] = field(default_factory=set)
    type_pref: dict[str, float] = field(default_factory=dict)
    type_evidence: dict[str, tuple[float, float]] = field(default_factory=dict)   # type -> (positive, negative)
    source_pref: dict[str, float] = field(default_factory=dict)
    hidden_sources: set[str] = field(default_factory=set)
    served_problems: set[int] = field(default_factory=set)
    min_problem_score: float = 0.0
    quiz_industries: set[str] = field(default_factory=set)
    quiz_skills: set[str] = field(default_factory=set)
    events: int = 0                                               # weighted events so far
    saved: list[str] = field(default_factory=list)                # most recent first
    not_for_me: list[str] = field(default_factory=list)
    default: float = 0.1
    mode: str = "exploring"                                       # exploring | committed (section 6.7)
    committed_problem: int | None = None
    committed_niche: int | None = None
    committed_competitors: tuple[str, ...] = ()                   # lower-cased product names

    def niche_affinity(self, node: int) -> float:
        if node in self.removed:
            return 0.0
        if node in self.pins:
            return self.pins[node]
        return self.niche.get(node, self.default)

    def on_map(self, node: int) -> bool:
        return node in self.map_nodes or node in self.pins or node in self.drop_ins


def _decayed(alpha: float, beta: float, prior: float, strength: float, days: float, half_life: float) -> tuple[float, float]:
    """Shrink the evidence above the prior by 0.5 ** (days / half_life)."""
    k = 0.5 ** (max(days, 0.0) / half_life)
    pa, pb = prior * strength, (1 - prior) * strength
    return pa + max(alpha - pa, 0.0) * k, pb + max(beta - pb, 0.0) * k


def load_state(store: FeedStore, config: FeedConfig, user_id: str, now: datetime | None = None,
               recent: int = 20) -> UserState:
    um = config.user_model
    now = now or utcnow()
    st = UserState(user_id=user_id, default=um.default_affinity)
    with store.session() as s:
        user = s.get(UserRow, user_id)
        prefs = user.feed_prefs or {}
        priors = prefs.get("priors", {})
        for cls, target, prefix in ((UserNicheAffinityRow, st.niche, "n"), (UserSkillAffinityRow, st.skill, "s")):
            for r in s.scalars(select(cls).where(cls.user_id == user_id)):
                prior = priors.get(f"{prefix}{r.node_id}", um.default_affinity)
                a, b = _decayed(r.alpha, r.beta, prior, um.prior_strength,
                                (now - r.updated_at).total_seconds() / 86400, um.evidence_half_life_days)
                target[r.node_id] = a / (a + b)
                if prefix == "n":
                    st.niche_uncertainty[r.node_id] = (a, b)
        st.pins = {r.node_id: r.affinity for r in s.scalars(select(UserPinRow).where(UserPinRow.user_id == user_id))}
        st.drop_ins = {r.node_id: r.exposures_left
                       for r in s.scalars(select(UserDropInRow).where(UserDropInRow.user_id == user_id))}
        st.removed = set(s.scalars(select(UserRemovedRow.node_id).where(UserRemovedRow.user_id == user_id)))
        st.map_nodes = set(s.scalars(select(UserMapNodeRow.node_id).where(
            UserMapNodeRow.user_id == user_id, UserMapNodeRow.status.in_(ON_MAP))))
        st.mode = user.mode or "exploring"
        if st.mode == "committed" and user.committed_problem_id is not None:
            problem = s.get(ProblemRow, user.committed_problem_id)
            if problem is not None and problem.status == "active":
                st.committed_problem, st.committed_niche = problem.id, problem.niche_id
                st.committed_competitors = tuple(c.lower() for c in problem.competitors or [])
            else:
                st.mode = "exploring"        # the problem is gone: fall back rather than show an empty feed
        st.type_pref = dict(user.type_preference or {})
        st.type_evidence = {t: (float(a), float(b)) for t, (a, b) in prefs.get("type_evidence", {}).items()}
        st.source_pref = dict(user.source_preference or {})
        st.hidden_sources = set(prefs.get("hidden_sources", []))
        st.served_problems = set(prefs.get("served_problems", []))
        st.min_problem_score = float(prefs.get("min_problem_score", 0.0))
        quiz = s.get(UserQuizRow, user_id)
        if quiz:
            st.quiz_industries = {x.lower() for x in quiz.industries}
            st.quiz_skills = {x.lower() for x in quiz.skills + quiz.resume_skills}
        st.events = s.scalar(select(func.count()).select_from(EventRow).where(
            EventRow.user_id == user_id, EventRow.weight != 0)) or 0
        for etype, item_id in s.execute(select(EventRow.event_type, EventRow.item_id).where(
                EventRow.user_id == user_id, EventRow.item_type == "post",
                EventRow.event_type.in_((*POSITIVE_ACTIONS, "not_for_me"))).order_by(EventRow.at.desc())):
            bucket = st.not_for_me if etype == "not_for_me" else st.saved
            if len(bucket) < recent and item_id not in bucket:
                bucket.append(item_id)
    return st


# --- updates ---------------------------------------------------------------------

def _bump(s, cls, user_id: str, node_id: int, delta: float, prior: float, config: FeedConfig, now: datetime) -> None:
    um = config.user_model
    row = s.get(cls, (user_id, node_id))
    if row is None:
        row = cls(user_id=user_id, node_id=node_id, alpha=prior * um.prior_strength,
                  beta=(1 - prior) * um.prior_strength, updated_at=now)
        s.add(row)
    else:
        row.alpha, row.beta = _decayed(row.alpha, row.beta, prior, um.prior_strength,
                                       (now - row.updated_at).total_seconds() / 86400, um.evidence_half_life_days)
    if delta > 0:
        row.alpha += delta
    else:
        row.beta += -delta
    row.updated_at = now


def apply_events(store: FeedStore, posts: PostsReader, config: FeedConfig, events: Iterable[EventRow],
                 clock: Callable[[], datetime] = utcnow) -> int:
    """Update affinities and preferences from stored events (section 6.4). Returns how many moved
    the model (weight != 0)."""
    um = config.user_model
    events = [e for e in events if e.weight != 0]
    if not events:
        return 0
    now = clock()
    post_ids = {e.item_id for e in events if e.item_type == "post"}
    problem_ids = {int(e.item_id) for e in events if e.item_type == "problem" and e.item_id.isdigit()}
    sources = {p.id: p.source for p in posts.get_posts(list(post_ids))}
    with store.session() as s, s.begin():
        leaf: dict[str, int] = {}
        skills: dict[str, list[int]] = {}
        for pid, nid, tree in s.execute(select(PostNicheRow.post_id, PostNicheRow.niche_id, PostNicheRow.tree)
                                        .where(PostNicheRow.post_id.in_(post_ids))):
            if tree == INDUSTRY:
                leaf[pid] = nid
            else:
                skills.setdefault(pid, []).append(nid)
        rows = {r.post_id: r for r in s.scalars(select(EnrichedPostRow).where(EnrichedPostRow.post_id.in_(post_ids)))}
        problems = {p.id: p for p in s.scalars(select(ProblemRow).where(ProblemRow.id.in_(problem_ids)))}
        post_problem = dict(s.execute(select(ProblemPostRow.post_id, ProblemPostRow.problem_id)
                                      .where(ProblemPostRow.post_id.in_(post_ids))).all())
        parents = dict(s.execute(select(NicheRow.id, NicheRow.parent_id)).all())
        users = {}
        for e in events:
            user = users.get(e.user_id) or s.get(UserRow, e.user_id)
            users[e.user_id] = user
            prefs = dict(user.feed_prefs or {})
            priors = prefs.get("priors", {})
            if e.item_type == "post":
                niche, skill_nodes = leaf.get(e.item_id), skills.get(e.item_id, [])
                post_type = rows[e.item_id].post_type if e.item_id in rows else None
                source, problem_id = sources.get(e.item_id), post_problem.get(e.item_id)
            elif e.item_type == "inferred":
                inf = s.get(InferredProblemRow, int(e.item_id)) if e.item_id.isdigit() else None
                niche, skill_nodes, post_type, source = (inf.niche_id if inf else None), [], "inferred", None
                problem_id = None
            else:
                prob = problems.get(int(e.item_id)) if e.item_id.isdigit() else None
                niche, skill_nodes, post_type, source = (prob.niche_id if prob else None), [], None, None
                problem_id = prob.id if prob else None
            to_niche, to_skills, w = True, True, e.weight
            if e.event_type == "hide_source":
                to_niche = to_skills = False
                if source:
                    prefs["hidden_sources"] = sorted(set(prefs.get("hidden_sources", [])) | {source})
            elif e.event_type == "not_for_me" and e.reason == "not_true":
                to_niche = to_skills = False      # 17.8: routes to validation, not a dislike of the niche
                if e.item_type == "inferred" and e.item_id.isdigit():
                    from ..inferred.validation import vote_in_session
                    vote_in_session(s, config, e.user_id, int(e.item_id), "no", None, "not_for_me", now)
            elif e.event_type == "not_for_me" and e.reason:
                to_niche = e.reason == "industry_not_interesting"
                to_skills = e.reason == "not_my_skills"
                if e.reason == "too_small":
                    prefs["min_problem_score"] = round(min(1.0, float(prefs.get("min_problem_score", 0.0))
                                                           + um.too_small_step), 4)
                elif e.reason == "already_exists" and problem_id is not None:
                    prefs["served_problems"] = sorted(set(prefs.get("served_problems", [])) | {problem_id})
                elif e.reason == "too_crowded":
                    prefs["too_crowded"] = int(prefs.get("too_crowded", 0)) + 1   # no competition data yet
            evidence = w * um.evidence_per_weight
            if to_niche and niche is not None:
                share = (1.0, um.propagation.parent, um.propagation.grandparent)
                node = niche
                for k in share:
                    if node is None:
                        break
                    _bump(s, UserNicheAffinityRow, e.user_id, node, evidence * k,
                          priors.get(f"n{node}", um.default_affinity), config, now)
                    node = parents.get(node)
            if to_skills:
                for node in skill_nodes:
                    _bump(s, UserSkillAffinityRow, e.user_id, node, evidence,
                          priors.get(f"s{node}", um.default_affinity), config, now)
            if post_type and e.event_type != "hide_source":
                te = dict(prefs.get("type_evidence", {}))
                pos, neg = te.get(post_type, (0.0, 0.0))
                te[post_type] = [round(pos + max(evidence, 0), 4), round(neg + max(-evidence, 0), 4)]
                prefs["type_evidence"] = te
                tp = dict(user.type_preference or {})
                tp[post_type] = round(max(-1.0, min(1.0, tp.get(post_type, 0.0) + um.type_preference_rate * w)), 4)
                user.type_preference = tp
            if source:
                cap = um.source_preference_cap
                sp = dict(user.source_preference or {})
                sp[source] = round(max(-cap, min(cap, sp.get(source, 0.0) + um.source_preference_rate * w)), 4)
                user.source_preference = sp
            user.feed_prefs = prefs
    return len(events)
