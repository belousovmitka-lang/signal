"""Section 13 metrics over a window (default: the last 7 days), optionally for a subset of users (an A/B
arm). Definitions are spelled out next to each number because "save rate" can mean several things.

Guardrails (time spent, sessions per day, scroll depth) are reported to be watched, never optimized.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection
from datetime import datetime, timedelta

from sqlalchemy import func, select

from ..db import EventRow, FeedStore, ImpressionRow, PostNicheRow, ProblemPostRow, ProblemRow
from ..posts import PostsReader


def _ratio(a: float, b: float) -> float | None:
    return round(a / b, 4) if b else None


def weekly_metrics(store: FeedStore, posts: PostsReader, now: datetime, days: int = 7,
                   users: Collection[str] | None = None) -> dict:
    since = now - timedelta(days=days)
    with store.session() as s:
        iq = select(ImpressionRow.id, ImpressionRow.user_id, ImpressionRow.session_id, ImpressionRow.item_id,
                    ImpressionRow.position, ImpressionRow.candidate_source).where(ImpressionRow.shown_at >= since, ImpressionRow.shown_at <= now,
                                                          ImpressionRow.candidate_source != "calibration")
        eq = select(EventRow.user_id, EventRow.session_id, EventRow.item_id, EventRow.event_type, EventRow.weight,
                    EventRow.dwell_ms, EventRow.impression_id).where(EventRow.at >= since, EventRow.at <= now)
        if users is not None:
            iq, eq = iq.where(ImpressionRow.user_id.in_(users)), eq.where(EventRow.user_id.in_(users))
        imps, evs = list(s.execute(iq)), list(s.execute(eq))
        leaf = dict(s.execute(select(PostNicheRow.post_id, PostNicheRow.niche_id)
                              .where(PostNicheRow.tree == "industry")).all())
        # Coverage: active problems with a post shown to anyone in 30 days (all users, by definition).
        shown_30 = select(ImpressionRow.item_id).where(ImpressionRow.shown_at >= now - timedelta(days=30))
        active = s.scalar(select(func.count()).select_from(ProblemRow).where(ProblemRow.status == "active")) or 0
        covered = s.scalar(select(func.count(func.distinct(ProblemPostRow.problem_id))).where(
            ProblemPostRow.post_id.in_(shown_30))) or 0
        first_seen = dict(s.execute(select(ImpressionRow.user_id, func.min(ImpressionRow.shown_at))
                                    .group_by(ImpressionRow.user_id)).all())
        seen_days: dict[str, set] = defaultdict(set)
        for uid, at in s.execute(select(ImpressionRow.user_id, ImpressionRow.shown_at)):
            seen_days[uid].add(at)
    exploration_imps = {i.id for i in imps if i.candidate_source == "exploration"}
    source = {p.id: p.source for p in posts.get_posts(list({i.item_id for i in imps}))}
    count = defaultdict(int)
    per_user = defaultdict(lambda: defaultdict(int))
    for e in evs:
        if e.weight != 0 or e.event_type in ("useful_yes", "useful_no"):
            count[e.event_type] += 1
            per_user[e.user_id][e.event_type] += 1
    shown = len(imps)
    active_users = {i.user_id for i in imps}
    sessions = {(i.user_id, i.session_id) for i in imps}
    sessions_with_save = {(e.user_id, e.session_id) for e in evs if e.event_type == "save" and e.weight > 0}
    north = [u for u in active_users if per_user[u]["save"] >= 3 and per_user[u]["idea"] >= 1]
    # Diversity: distinct leaf niches and sources per block of 20 consecutive items in a session.
    blocks, block_sources = defaultdict(set), defaultdict(set)
    for i in imps:
        blocks[(i.user_id, i.session_id, i.position // 20)].add(leaf.get(i.item_id))
        block_sources[(i.user_id, i.session_id, i.position // 20)].add(source.get(i.item_id))
    explore_saves = sum(1 for e in evs if e.event_type == "save" and e.weight > 0 and e.impression_id in exploration_imps)
    explore_shown = len(exploration_imps)
    depth = defaultdict(int)
    for i in imps:
        depth[(i.user_id, i.session_id)] = max(depth[(i.user_id, i.session_id)], i.position + 1)
    dwell = sum(e.dwell_ms or 0 for e in evs)

    def retention(day_from: int, day_to: int) -> dict:
        cohort = [u for u, first in first_seen.items() if first <= now - timedelta(days=day_to)
                  and (users is None or u in users)]
        back = [u for u in cohort if any(first_seen[u] + timedelta(days=day_from) <= at < first_seen[u] + timedelta(days=day_to)
                                         for at in seen_days[u])]
        return {"cohort": len(cohort), "returned": _ratio(len(back), len(cohort))}

    return {
        "window": {"from": since.isoformat(), "to": now.isoformat(), "users": len(active_users),
                   "sessions": len(sessions), "items_shown": shown},
        "product": {
            "north_star_builders": len(north),             # saved >= 3 posts and converted >= 1 idea this window
            "north_star_share": _ratio(len(north), len(active_users)),
            "saves_per_100_items": _ratio(100 * count["save"], shown),
            "sessions_with_a_save": _ratio(len(sessions_with_save & sessions), len(sessions)),
            "idea_conversion": _ratio(count["idea"], count["save"]),          # ideas per save
            "outreach_rate": _ratio(count["outreach"], count["idea"]),       # outreach per idea
            "not_for_me_rate": _ratio(count["not_for_me"], shown),
            "useful_share": _ratio(count["useful_yes"], count["useful_yes"] + count["useful_no"]),
            "retention_d7": retention(7, 14),                # came back in days 7-13 after their first item
            "retention_d30": retention(30, 60),              # came back in days 30-59
        },
        "feed_health": {
            "distinct_niches_per_20": _ratio(sum(len(b - {None}) for b in blocks.values()), len(blocks)),
            "distinct_sources_per_20": _ratio(sum(len(b - {None}) for b in block_sources.values()), len(block_sources)),
            "exploration_acceptance": _ratio(explore_saves, explore_shown),   # saves per exploration item shown
            "coverage_30d": _ratio(covered, active),        # active problems with a post shown in 30 days
        },
        "guardrails": {                                     # watch, never optimize
            "time_spent_minutes_per_user": _ratio(dwell / 60000, len(active_users)),
            "sessions_per_user_per_day": _ratio(len(sessions), len(active_users) * days),
            "scroll_depth_items": _ratio(sum(depth.values()), len(depth)),
        },
    }


def inferred_metrics(store: FeedStore, now: datetime, days: int = 30) -> dict:
    """Section 17.10, per detector: insider confirmation rate of published items, critic rejection rate,
    share with no existing solution found; and save / idea rates of inferred vs reported items shown."""
    from ..db import DetectorStatsRow, InferredProblemRow, InsiderVoteRow
    since = now - timedelta(days=days)
    with store.session() as s:
        problems = list(s.scalars(select(InferredProblemRow)))
        votes = list(s.scalars(select(InsiderVoteRow).where(InsiderVoteRow.insider.is_(True))))
        runs = list(s.scalars(select(DetectorStatsRow).where(DetectorStatsRow.run_at >= since)))
        shown = list(s.execute(select(ImpressionRow.item_type, ImpressionRow.id).where(
            ImpressionRow.shown_at >= since, ImpressionRow.candidate_source != "calibration")))
        acts = list(s.execute(select(EventRow.impression_id, EventRow.event_type).where(
            EventRow.at >= since, EventRow.event_type.in_(("save", "idea")), EventRow.weight > 0)))
    by_imp = dict((i, t) for t, i in shown)
    out: dict = {"detectors": {}}
    for det in sorted({p.detector for p in problems} | {r.detector for r in runs}):
        mine = [p for p in problems if p.detector == det]
        published = [p for p in mine if p.status == "published"]
        voted = {v.inferred_id for v in votes}
        rated = [p for p in published if p.id in voted]
        confirmed = [p for p in rated if (p.validation or {}).get("yes", 0) > (p.validation or {}).get("no", 0)]
        critic = sum(r.critic_runs for r in runs if r.detector == det)
        out["detectors"][det] = {
            "published": len(published),
            "insider_confirmation_rate": _ratio(len(confirmed), len(rated)),        # target >= 0.5 (17.10)
            "critic_rejection_rate": _ratio(sum(r.rejected for r in runs if r.detector == det), critic),
            "no_existing_solution_share": _ratio(sum(1 for p in published if not any(
                c.get("post_id") for c in p.counter_evidence or []) and not (p.critic_notes or {}).get(
                "existing_solution")), len(published)),
        }
    for kind, item_type in (("inferred", "inferred"), ("reported", "post")):
        n = sum(1 for t, _ in shown if t == item_type)
        out[kind] = {"shown": n,
                     "save_rate": _ratio(sum(1 for i, e in acts if e == "save" and by_imp.get(i) == item_type), n),
                     "idea_rate": _ratio(sum(1 for i, e in acts if e == "idea" and by_imp.get(i) == item_type), n)}
    return out
