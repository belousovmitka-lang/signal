"""Feed composition (section 9): collapse duplicates, mix quotas, diversity, guaranteed pins and
drop-ins, and the "You're caught up" floor. Each item also gets a one-line reason it was picked; it
is logged for debugging and evaluation and never shown to the user."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..config import FeedConfig
from .candidates import DROP_IN, EXPLORE, PIN, SIMILAR, SKILL_MATCH, TRENDING
from .data import FeedData
from .ranker import Scored
from .usermodel import UserState

DEMAND, CHANGE, EXPLORATION, CORE = "demand", "change", "exploration", "core"


@dataclass
class Placed:
    scored: Scored
    category: str
    explanation: str               # internal reason, logged with the impression, never shown
    forced: str | None = None      # why a rule put it here (quota name), for the score breakdown


def category(s: Scored, data: FeedData) -> str:
    it = data.items[s.index]
    if it.post_type == "paid_demand":
        return DEMAND
    if it.post_type == "change":
        return CHANGE
    if s.candidate.source == EXPLORE or s.ring == "exploring":
        return EXPLORATION
    return CORE


def explain(s: Scored, data: FeedData, state: UserState) -> str:
    it = data.items[s.index]
    src = s.candidate.source
    node = s.candidate.node if s.candidate.node in data.industry.nodes else it.niche_id
    leaf = data.industry.nodes[node].label
    if src == PIN:
        text = f"Pinned: {leaf}"
    elif src == DROP_IN:
        text = f"You added {leaf} · trying it for a few sessions"
    elif src == SKILL_MATCH and s.candidate.node in data.skill.nodes:
        skill = data.skill.nodes[s.candidate.node].label
        whose = "one of your skills" if skill.lower() in state.quiz_skills else "a skill in posts you liked"
        text = f"{data.industry.nodes[it.niche_id].label} · needs {skill}, {whose}"
    elif src == SIMILAR:
        text = f"{leaf} · builders like you saved this"
    elif src == TRENDING:
        text = f"Rising in {data.industry.nodes[it.niche_id].label}"
    elif src == EXPLORE or s.ring == "exploring":
        text = f"Exploring: {data.industry.nodes[it.niche_id].label} · new for you"
    elif s.similar_saves:
        text = f"{data.industry.nodes[it.niche_id].label} · close to {s.similar_saves} " \
               f"post{'s' if s.similar_saves > 1 else ''} you liked"
    else:
        ring = "your Core" if s.ring == "core" else "your interests"
        text = f"{data.industry.nodes[it.niche_id].label} · in {ring}"
    if it.post_type == "paid_demand":
        text += " · paid demand"
    return text


def collapse(scored: Sequence[Scored], data: FeedData) -> list[Scored]:
    """The best-scoring post per problem (section 9 step 1); posts without a problem stand alone."""
    best: dict[object, Scored] = {}
    for s in scored:
        it = data.items[s.index]
        key = ("problem", it.problem_id) if it.problem_id is not None else ("post", it.post_id)
        if key not in best or s.score > best[key].score:
            best[key] = s
    return sorted(best.values(), key=lambda s: -s.score)


def compose(scored: Sequence[Scored], data: FeedData, state: UserState, config: FeedConfig, now: datetime,
            previous: Sequence[Scored] = (), explore_more: bool = False) -> tuple[list[Placed], bool]:
    """Fill one page. Returns the items and whether the user is caught up (strong items ran out)."""
    c = config.composition
    q = c.quotas
    size, window = c.page_size, c.window
    pool = collapse(scored, data)
    cats = {id(s): category(s, data) for s in pool}
    fresh_cut = now - timedelta(hours=c.fresh_hours)
    is_fresh = {id(s): data.items[s.index].published_at >= fresh_cut for s in pool}
    guaranteed = [s for s in pool if s.candidate.source in (PIN, DROP_IN)]
    nodes_to_serve = list(dict.fromkeys(s.candidate.node for s in guaranteed))[:c.pins_per_page]
    # Spread guaranteed nodes evenly: node k is due from slot (k + 1) * size // (m + 1) - 1.
    due = [(k + 1) * size // (len(nodes_to_serve) + 1) - 1 for k in range(len(nodes_to_serve))]
    served = 0
    page: list[Placed] = []
    history = list(previous)
    counts = {DEMAND: 0, CHANGE: 0, EXPLORATION: 0}
    fresh_in_first = 0
    caught_up = False

    def ok_diversity(s: Scored) -> bool:
        if not history:
            return True
        it, last = data.items[s.index], data.items[history[-1].index]
        if (it.problem_id is not None and it.problem_id == last.problem_id) or it.source == last.source:
            return False
        run = history[-c.max_same_type_in_a_row:]
        if len(run) == c.max_same_type_in_a_row and all(data.items[h.index].post_type == it.post_type for h in run):
            return False
        recent = history[-9:]
        return sum(data.items[h.index].niche_id == it.niche_id for h in recent) < c.max_same_leaf_per_10

    for slot in range(size):
        if not pool:
            caught_up = True
            break
        if pool[0].score < c.caught_up_score_floor and not explore_more:
            caught_up = True
            break
        n = slot + 1
        allowed = list(pool)
        if not explore_more:
            if counts[CHANGE] >= math.floor(q.change_max * window):
                allowed = [s for s in allowed if cats[id(s)] != CHANGE]
            if counts[EXPLORATION] >= math.floor(q.exploration_max * window):
                allowed = [s for s in allowed if cats[id(s)] != EXPLORATION]
        forced, rule = None, None
        # Guaranteed nodes (pins, drop-ins), spread through the page.
        while served < len(nodes_to_serve) and slot >= due[served] and forced is None:
            node = nodes_to_serve[served]
            options = [s for s in allowed if s.candidate.node == node and s.candidate.source in (PIN, DROP_IN)]
            if options:
                forced, rule = options, "guaranteed"
            else:
                served += 1          # nothing left to show for it on this page
        # Paced minimums: demand, exploration, and freshness in the first 10.
        if forced is None and not explore_more:
            for name, cat, share in ((DEMAND, DEMAND, q.paid_demand_min), (EXPLORATION, EXPLORATION, q.exploration_min)):
                if counts[cat] < math.floor(share * n):
                    options = [s for s in allowed if cats[id(s)] == cat]
                    if options:
                        forced, rule = options, f"{name}_min"
                        break
        if forced is None and slot < 10 and fresh_in_first < math.floor(c.fresh_share_first_10 * n):
            options = [s for s in allowed if is_fresh[id(s)]]
            if options:
                forced, rule = options, "fresh"
        options = forced if forced is not None else allowed
        if not options:
            options = list(pool)
        # Diversity first: a forced quota that can't be met without breaking it waits one slot.
        pick = next((s for s in options if ok_diversity(s)), None)
        if pick is None and forced is not None:
            pick = next((s for s in allowed if ok_diversity(s)), None)
            rule = None if pick is not None else rule
        pick = pick or options[0]
        pool.remove(pick)
        cat = cats[id(pick)]
        if cat in counts:
            counts[cat] += 1
        if slot < 10 and is_fresh[id(pick)]:
            fresh_in_first += 1
        if rule == "guaranteed":
            served += 1
        history.append(pick)
        page.append(Placed(pick, cat, explain(pick, data, state), rule))
    return page, caught_up
