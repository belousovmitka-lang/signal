"""Candidate generation (section 7): ~500 posts per session from several sources, de-duplicated.

Sources fill their share of the target in priority order; a post keeps the first source that found
it (it decides the explanation and the exploration quota).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
from sqlalchemy import select

from ..config import FeedConfig
from ..db import EventRow, FeedStore, ImpressionRow, UserQuizRow
from .data import FeedData
from .usermodel import POSITIVE_ACTIONS, UserState

PIN, DROP_IN, NICHE, SKILL_MATCH, SIMILAR, TRENDING, EXPLORE, FILL = (
    "pin", "drop_in", "niche_match", "skill_match", "similar_builders", "trending_nearby", "exploration", "fill")


@dataclass
class Candidate:
    index: int                      # FeedData item index
    source: str
    node: int | None = None         # the niche or skill node that produced it
    social: int = 0                 # similar builders who saved it


def seen_post_ids(store: FeedStore, user_id: str, since: datetime) -> set[str]:
    with store.session() as s:
        return set(s.scalars(select(ImpressionRow.item_id).where(
            ImpressionRow.user_id == user_id, ImpressionRow.item_type == "post", ImpressionRow.shown_at >= since)))


def similar_builder_saves(store: FeedStore, config: FeedConfig, state: UserState, since: datetime) -> dict[str, int]:
    """post id -> number of builders with an overlapping quiz who saved it recently (section 6.2 step 3)."""
    mine = state.quiz_industries | state.quiz_skills
    if not mine:
        return {}
    with store.session() as s:
        peers = []
        for q in s.scalars(select(UserQuizRow).where(UserQuizRow.user_id != state.user_id)):
            theirs = {x.lower() for x in q.industries + q.skills + q.resume_skills}
            if theirs and len(mine & theirs) / len(mine | theirs) >= config.user_model.similar_builders_min_jaccard:
                peers.append(q.user_id)
        if not peers:
            return {}
        counts: dict[str, set[str]] = defaultdict(set)
        for uid, item in s.execute(select(EventRow.user_id, EventRow.item_id).where(
                EventRow.user_id.in_(peers), EventRow.item_type == "post", EventRow.at >= since,
                EventRow.event_type.in_(POSITIVE_ACTIONS))):
            counts[item].add(uid)
    return {item: len(users) for item, users in counts.items()}


def similar_builders_weight(config: FeedConfig, events: int) -> float:
    um = config.user_model
    full, zero = um.similar_builders_full_until_events, um.similar_builders_zero_at_events
    if events <= full:
        return 1.0
    return max(0.0, 1 - (events - full) / max(zero - full, 1))


def generate(data: FeedData, state: UserState, config: FeedConfig, store: FeedStore, now: datetime,
             rng: np.random.Generator, explore_more: bool = False,
             exploration_share: float | None = None) -> list[Candidate]:
    """`exploration_share` overrides the configured share (the filter-bubble guard raises it)."""
    cfg = config.candidates
    target = cfg.per_session
    seen = seen_post_ids(store, state.user_id, now - timedelta(days=cfg.exclude_seen_days))

    def allowed(i: int) -> bool:
        it = data.items[i]
        if it.post_id in seen or it.source in state.hidden_sources:
            return False
        if it.problem_id is not None and it.problem_id in state.served_problems:
            return False
        if it.problem_kind == "pain" and it.problem_score < state.min_problem_score:
            return False
        return not any(n in state.removed for n in data.ancestors(it.niche_id))

    out: dict[int, Candidate] = {}

    def take(indexes, source: str, limit: int, node: int | None = None, social: dict[str, int] | None = None) -> None:
        added = 0
        for i in indexes:
            if added >= limit or len(out) >= target:
                return
            if i in out or not allowed(i):
                continue
            out[i] = Candidate(i, source, node, (social or {}).get(data.items[i].post_id, 0))
            added += 1

    def nearest(indexes: list[int], centre: np.ndarray | None) -> list[int]:
        """Posts of a niche, closest to its centroid first; newest first when there is no centroid."""
        if not indexes:
            return []
        if centre is None:
            return sorted(indexes, key=lambda i: data.items[i].published_at, reverse=True)
        sims = data.vectors[indexes] @ centre
        return [indexes[k] for k in np.argsort(-sims)]

    # Guaranteed: pinned and dropped-in nodes.
    for node in [*state.pins, *(n for n, left in state.drop_ins.items() if left > 0)]:
        if node in data.industry.nodes:
            take(nearest(data.subtree_items(node), data.industry.centroids.get(node)),
                 PIN if node in state.pins else DROP_IN, 20, node)

    shares = cfg.shares
    if explore_more:   # "Explore more" after "You're caught up": mostly outside the map
        plan = [(EXPLORE, 0.7), (TRENDING, 0.15), (NICHE, 0.15)]
    else:
        plan = [(NICHE, shares.niche_match), (SKILL_MATCH, shares.skill_match),
                (SIMILAR, shares.similar_builders * similar_builders_weight(config, state.events)),
                (TRENDING, shares.trending_nearby),
                (EXPLORE, max(shares.exploration, exploration_share or 0.0))]
    top_niches = sorted(((n, state.niche_affinity(n)) for n in {*state.niche, *state.pins}
                         if n in data.industry.nodes and n not in state.removed), key=lambda x: -x[1])[:8]
    for source, share in plan:
        quota = int(round(target * share))
        if source == NICHE:
            total = sum(a for _, a in top_niches) or 1.0
            for node, aff in top_niches:
                take(nearest(data.subtree_items(node), data.industry.centroids.get(node)), NICHE,
                     int(round(quota * aff / total)), node)
        elif source == SKILL_MATCH:
            top_skills = sorted(((n, a) for n, a in state.skill.items()
                                 if a >= config.user_model.rings.regular_min), key=lambda x: -x[1])[:5]
            total = sum(a for _, a in top_skills) or 1.0
            for node, aff in top_skills:
                ids = sorted(data.by_skill.get(node, []), key=lambda i: -data.items[i].problem_score)
                take(ids, SKILL_MATCH, int(round(quota * aff / total)), node)
        elif source == SIMILAR and quota:
            social = similar_builder_saves(store, config, state, now - timedelta(days=cfg.exclude_seen_days))
            ids = [data.by_id[p].index for p, _ in sorted(social.items(), key=lambda x: -x[1]) if p in data.by_id]
            take(ids, SIMILAR, quota, social=social)
        elif source == TRENDING:
            near: set[int] = set()
            for node, _ in top_niches:
                parent = data.industry.nodes[node].parent_id
                near |= {parent} if parent is not None else set()
                near |= set(data.industry.children.get(parent, [])) if parent is not None else set()
            ids = [i for n in near for i in data.by_niche.get(n, [])
                   if data.items[i].problem_id is not None and data.items[i].trend >= 0.6]
            ids.sort(key=lambda i: -(data.items[i].trend + data.items[i].problem_score))
            take(ids, TRENDING, quota)
        elif source == EXPLORE:
            # Thompson sampling over niches off the map: draw from each niche's Beta and explore the
            # best draws, so unknown niches get tried and ones the user disliked fade out.
            um = config.user_model
            # Off the map: not a map node and not inside one (a chosen industry's branches are "yours").
            mapped = {n for n in data.industry.nodes if state.on_map(n)}
            pool = [n for n in data.industry.nodes if data.by_niche.get(n) and n not in state.removed
                    and not mapped & set(data.ancestors(n))]
            if pool:
                prior = (um.default_affinity * um.prior_strength, (1 - um.default_affinity) * um.prior_strength)
                draws = [(rng.beta(*state.niche_uncertainty.get(n, prior)), n) for n in pool]
                per_niche = max(1, quota // 8)
                for _, node in sorted(draws, reverse=True):
                    take(nearest(data.by_niche[node], data.industry.centroids.get(node)), EXPLORE, per_niche, node)
                    if sum(c.source == EXPLORE for c in out.values()) >= quota:
                        break
    # Top up with the best remaining posts from anywhere so a thin map never yields a thin feed.
    if len(out) < target:
        rest = sorted(range(len(data.items)), key=lambda i: -data.items[i].problem_score)
        take(rest, FILL, target - len(out))
    return list(out.values())
