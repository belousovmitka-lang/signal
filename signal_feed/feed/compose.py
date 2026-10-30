"""Feed composition (section 9): collapse duplicates, mix quotas, diversity, guaranteed pins and
drop-ins, and the "You're caught up" floor. Each item also gets a one-line reason it was picked; it
is logged for debugging and evaluation and never shown to the user."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..config import FeedConfig
from .candidates import (
    COMMITTED_SOURCES,
    COMPETITOR,
    DROP_IN,
    EVIDENCE,
    EXPLORE,
    PIN,
    RELATED_DEMAND,
    SIMILAR,
    SKILL_MATCH,
    TRENDING,
)
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
    if it.item_type == "inferred":
        text = f"Inferred ({it.confidence}) · {data.industry.nodes[it.niche_id].label}"
    elif src == EVIDENCE:
        text = f"Committed: evidence for problem {it.problem_id}"
    elif src == COMPETITOR:
        text = f"Committed: mentions a competitor ({', '.join(sorted(set(it.tools) & set(state.committed_competitors)))})"
    elif src == RELATED_DEMAND:
        text = f"Committed: related paid demand in {leaf}"
    elif src in COMMITTED_SOURCES:
        text = f"Committed: {leaf}"
    elif src == PIN:
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


def collapse(scored: Sequence[Scored], data: FeedData, keep_all: int | None = None) -> list[Scored]:
    """The best-scoring post per problem (section 9 step 1); posts without a problem stand alone. In
    committed mode the committed problem (`keep_all`) keeps all its posts: they are its evidence."""
    best: dict[object, Scored] = {}
    for s in scored:
        it = data.items[s.index]
        key = ("problem", it.problem_id) if it.problem_id is not None and it.problem_id != keep_all \
            else ("post", it.post_id)
        g = data.group_of.get(s.index)
        if key[0] == "problem" and g is not None and data.links.get(g):
            key = ("signal", min(g, *data.links[g]))     # problems linked across signal types are one problem
        if key not in best or s.score > best[key].score:
            best[key] = s
    return sorted(best.values(), key=lambda s: -s.score)


def drop_repeats(pool: list[Scored], data: FeedData, shown: Sequence[int], cosine: float) -> list[Scored]:
    """Leave out items that repeat one already shown (founder, 2026-10-30: "the problem should not be
    appearing more than once in one feed"): the same problem, a problem linked to it across signal types,
    or near-identical text."""
    if not shown or not pool:
        return pool
    problems = {data.items[i].problem_id for i in shown} - {None}
    groups = {g for i in shown if (g := data.group_of.get(i)) is not None}
    groups |= {h for g in list(groups) for h in data.links.get(g, ())}
    # An inferred problem is a synthesis of posts, so it reads like them: it's compared with other inferred
    # problems only.
    inferred = {i for i in shown if data.items[i].item_type == "inferred"}
    posts = [i for i in shown if i not in inferred]

    def near(s: Scored, among: list[int]) -> bool:
        return bool(among) and float((data.vectors[among] @ data.vectors[s.index]).max()) >= cosine
    out = []
    for s in pool:
        it = data.items[s.index]
        if it.item_type == "inferred":
            if s.index not in inferred and not near(s, sorted(inferred)):
                out.append(s)
        elif it.problem_id not in problems and data.group_of.get(s.index) not in groups and not near(s, posts):
            out.append(s)
    return out


def compose(scored: Sequence[Scored], data: FeedData, state: UserState, config: FeedConfig, now: datetime,
            previous: Sequence[Scored] = (), explore_more: bool = False,
            exploration_floor: float | None = None, shown: Sequence[int] = ()) -> tuple[list[Placed], bool]:
    """Fill one page. Returns the items and whether the user is caught up (strong items ran out).
    `exploration_floor` raises the exploration quota (the filter-bubble guard, section 10). `shown` are the
    indexes of every item the session has shown: nothing that repeats one of them is placed again, and
    nothing on the page repeats another."""
    c = config.composition
    q = c.quotas
    if exploration_floor:
        q = q.model_copy(update={"exploration_min": max(q.exploration_min, exploration_floor),
                                 "exploration_max": max(q.exploration_max, exploration_floor)})
    committed = state.mode == "committed" and state.committed_niche is not None
    focus: set[int] = set()
    if committed:   # section 6.7: everything outside the problem is capped; no forced exploration
        q = q.model_copy(update={"exploration_min": 0.0, "paid_demand_min": 0.0})
        focus = set(data.industry.subtree(state.committed_niche))
        cap = math.floor(config.user_model.committed_other_cap * c.window)

        def outside(s: Scored) -> bool:
            return s.candidate.source not in COMMITTED_SOURCES and data.items[s.index].niche_id not in focus
    others = 0
    size, window = c.page_size, c.window
    pool = collapse(scored, data, state.committed_problem if state.mode == "committed" else None)
    dup = config.signals.duplicate_cosine
    # Committed mode: the committed problem's posts are its evidence, shown together on purpose.
    evidence = [s for s in pool if committed and data.items[s.index].problem_id == state.committed_problem]
    pool = drop_repeats([s for s in pool if s not in evidence], data, shown, dup) + evidence
    floor = c.caught_up_score_floor

    def order(s: Scored) -> tuple:
        tier = data.items[s.index].signal_types if config.signals.lead_by_types and s.score >= floor else 0
        return (-tier, -s.score)
    pool.sort(key=order)
    cats = {id(s): category(s, data) for s in pool}
    # Quotas count flags, not the single display category: a tender found by exploration is both demand and
    # exploration. Exploration means items that came from exploration (not every low-affinity item, or a
    # thin map could never fill a page).
    flags = {id(s): {DEMAND: data.items[s.index].post_type == "paid_demand",
                     CHANGE: data.items[s.index].post_type == "change",
                     EXPLORATION: s.candidate.source == EXPLORE} for s in pool}
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
    type_counts: dict[str, int] = {}
    fresh_in_first = 0
    caught_up = False

    def ok_diversity(s: Scored, level: int = 0) -> bool:
        """Level 0 checks every rule; when nothing passes, rules are dropped one at a time: the leaf limit
        (level 1), then the post-type run (2), then the source and problem rule (3)."""
        if not history or level >= 3:
            return True
        it, last = data.items[s.index], data.items[history[-1].index]
        if (it.problem_id is not None and it.problem_id == last.problem_id) or it.source == last.source:
            return False
        run = history[-c.max_same_type_in_a_row:]
        if level < 2 and len(run) == c.max_same_type_in_a_row and all(data.items[h.index].post_type == it.post_type
                                                                       for h in run):
            return False
        if committed and it.niche_id in focus:
            return True          # committed mode narrows to one niche on purpose: no per-leaf limit there
        recent = history[-9:]
        return level >= 1 or sum(data.items[h.index].niche_id == it.niche_id for h in recent) < c.max_same_leaf_per_10

    for slot in range(size):
        if not pool:
            caught_up = True
            break
        if max(s.score for s in pool) < c.caught_up_score_floor and not explore_more:
            caught_up = True
            break
        n = slot + 1
        allowed = list(pool)
        if committed and others >= cap:
            allowed = [s for s in allowed if not outside(s)]
        if committed and counts[DEMAND] >= math.floor(config.user_model.committed_demand_max * c.window):
            allowed = [s for s in allowed if not flags[id(s)][DEMAND]]
        if not explore_more:
            if counts[DEMAND] >= math.floor(q.paid_demand_max * window):
                allowed = [s for s in allowed if not flags[id(s)][DEMAND]]
            full = {t for t, n in type_counts.items() if n >= math.floor(q.type_max * window)}
            if full:
                allowed = [s for s in allowed if data.items[s.index].post_type not in full]
            if counts[CHANGE] >= math.floor(q.change_max * window):
                allowed = [s for s in allowed if not flags[id(s)][CHANGE]]
            if counts[EXPLORATION] >= math.floor(q.exploration_max * window):
                allowed = [s for s in allowed if not flags[id(s)][EXPLORATION]]
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
                    options = [s for s in allowed if flags[id(s)][cat]]
                    if options:
                        forced, rule = options, f"{name}_min"
                        break
        if forced is None and slot < 10 and fresh_in_first < math.floor(c.fresh_share_first_10 * n):
            options = [s for s in allowed if is_fresh[id(s)]]
            if options:
                forced, rule = options, "fresh"
        options = forced if forced is not None else allowed
        if not options and committed and others >= cap:
            caught_up = True     # the problem's own items ran out; never fill past the cap
            break
        if not options and page:
            break                # every candidate breaks a cap: end the page here, the next one continues
        if not options:
            options = list(pool)
        # Diversity first: a forced quota that can't be met without breaking it waits one slot; when nothing
        # passes every rule, the rules are relaxed one at a time rather than all at once.
        pick = None
        for level in range(4):
            pick = next((s for s in options if ok_diversity(s, level)), None)
            if pick is None and forced is not None:
                pick = next((s for s in allowed if ok_diversity(s, level)), None)
                if pick is not None:
                    rule = None
            if pick is not None:
                break
        pick = pick or options[0]
        pool.remove(pick)
        if pick not in evidence:                                  # and whatever repeats it
            pool = drop_repeats([s for s in pool if s not in evidence], data, [pick.index], dup) + \
                [s for s in pool if s in evidence]
            pool.sort(key=order)
        type_counts[data.items[pick.index].post_type] = type_counts.get(data.items[pick.index].post_type, 0) + 1
        if committed and outside(pick):
            others += 1
        cat = cats[id(pick)]
        for name, on in flags[id(pick)].items():
            counts[name] += on
        if slot < 10 and is_fresh[id(pick)]:
            fresh_in_first += 1
        if rule == "guaranteed":
            served += 1
        history.append(pick)
        page.append(Placed(pick, cat, explain(pick, data, state), rule))
    return page, caught_up
