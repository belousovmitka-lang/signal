"""The weekly map job (section 6.5-6.6, and 4.2 step 5 for tree changes).

Per user: follow tree changes (merged niches pass their state to the survivor, retired ones to their
parent), narrow (promote a child that holds most of a node's recent positive weight), discover (an
off-map niche the user keeps engaging with joins the map), fade (many skips and no positives), re-open
removed niches the user allowed to be re-tested, and write the weekly snapshot. Each change gets a
plain-language change-log entry for the map screen.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import delete, select

from signal_connectors.models import utcnow

from ..config import FeedConfig
from ..db import (
    EventRow,
    FeedStore,
    NicheRow,
    PostNicheRow,
    ProfilePendingUpdateRow,
    UserDropInRow,
    UserMapNodeRow,
    UserMapSnapshotRow,
    UserNicheAffinityRow,
    UserPinRow,
    UserProfileNicheRow,
    UserRemovedRow,
    UserRow,
)
from .usermap import ON_MAP, add_to_map, log_change, map_view, raise_affinity
from .usermodel import load_state


@dataclass
class WeeklyStats:
    users: int = 0
    migrated: int = 0
    promoted: list[str] = field(default_factory=list)
    discovered: list[str] = field(default_factory=list)
    fading: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    reopened: int = 0
    snapshots: int = 0
    profile_updates: int = 0


def week_of(day: datetime):
    d = day.date()
    return d - timedelta(days=d.weekday())


def migrate_tree_changes(store: FeedStore) -> int:
    """Per-user rows on merged or retired niches move to the survivor or the parent (section 4.2 step 5)."""
    moved = 0
    with store.session() as s, s.begin():
        target: dict[int, int] = {}
        for n in s.scalars(select(NicheRow).where(NicheRow.status.in_(("merged", "retired")))):
            to = n.merged_into_id if n.status == "merged" else n.parent_id
            if to is not None:
                target[n.id] = to
        if not target:
            return 0

        def final(node: int) -> int:
            seen = set()
            while node in target and node not in seen:
                seen.add(node)
                node = target[node]
            return node

        for u in s.scalars(select(ProfilePendingUpdateRow).where(ProfilePendingUpdateRow.node_id.in_(target),
                                                                 ProfilePendingUpdateRow.status == "pending")):
            u.node_id = final(u.node_id)
            moved += 1
        for cls in (UserNicheAffinityRow, UserMapNodeRow, UserPinRow, UserDropInRow, UserRemovedRow,
                    UserProfileNicheRow):
            for row in list(s.scalars(select(cls).where(cls.node_id.in_(target)))):
                to = final(row.node_id)
                existing = s.get(cls, (row.user_id, to))
                if existing is None:
                    s.add(cls(**{c.name: getattr(row, c.name) for c in cls.__table__.columns} | {"node_id": to}))
                elif cls is UserNicheAffinityRow and row.alpha + row.beta > existing.alpha + existing.beta:
                    existing.alpha, existing.beta = row.alpha, row.beta   # keep the better-evidenced belief
                s.delete(row)
                moved += 1
    return moved


def _events(s, user_id: str, since: datetime) -> list[tuple[int, float]]:
    """(industry leaf, weight) for each weighted post event since `since`."""
    return list(s.execute(select(PostNicheRow.niche_id, EventRow.weight)
                          .join(PostNicheRow, (PostNicheRow.post_id == EventRow.item_id) & (PostNicheRow.tree == "industry"))
                          .where(EventRow.user_id == user_id, EventRow.item_type == "post", EventRow.at >= since,
                                 EventRow.weight != 0)).all())


def run_user(store: FeedStore, config: FeedConfig, user_id: str, now: datetime, stats: WeeklyStats) -> None:
    nar, um, mp = config.user_model.narrowing, config.user_model, config.map
    state = load_state(store, config, user_id, now)
    with store.session() as s, s.begin():
        parents = dict(s.execute(select(NicheRow.id, NicheRow.parent_id).where(NicheRow.status == "active")).all())
        labels = dict(s.execute(select(NicheRow.id, NicheRow.label)).all())

        def chain(leaf: int) -> list[int]:
            out = [leaf]
            while parents.get(out[-1]) is not None:
                out.append(parents[out[-1]])
            return out

        recent = _events(s, user_id, now - timedelta(weeks=nar.lookback_weeks))
        fade_window = _events(s, user_id, now - timedelta(weeks=nar.fade_weeks_without_positive))
        pos_weight: dict[int, float] = defaultdict(float)   # node -> positive weight in its subtree
        pos_count: dict[int, int] = defaultdict(int)
        for leaf, w in recent:
            if w > 0:
                for node in chain(leaf):
                    pos_weight[node] += w
                    pos_count[node] += 1
        neg_recent: dict[int, int] = defaultdict(int)
        pos_recent: dict[int, int] = defaultdict(int)
        for leaf, w in fade_window:
            for node in chain(leaf):
                (pos_recent if w > 0 else neg_recent)[node] += 1
        rows = {r.node_id: r for r in s.scalars(select(UserMapNodeRow).where(UserMapNodeRow.user_id == user_id))}
        active = [r for r in rows.values() if r.status in ON_MAP]

        # Narrowing: one child holds most of a node's recent positive weight.
        for r in active:
            if r.status == "fading" or pos_weight.get(r.node_id, 0) <= 0:
                continue
            kids = [k for k, p in parents.items() if p == r.node_id]
            if not kids:
                continue
            best = max(kids, key=lambda k: pos_weight.get(k, 0))
            share = pos_weight.get(best, 0) / pos_weight[r.node_id]
            child_row = rows.get(best)
            if share >= nar.child_share_of_positive and pos_count.get(best, 0) >= nar.child_min_positive_events \
                    and not (child_row and child_row.status in ("active", "context")):
                # The promoted child inherits the parent's affinity x 0.9 (or keeps its own if higher).
                raise_affinity(s, user_id, best, state.niche_affinity(r.node_id) * nar.promoted_affinity_factor,
                               config, now)
                rows[best] = add_to_map(s, user_id, best, "promoted", config, now)
                if r.source != "pin":
                    r.status, r.status_since = "context", now
                log_change(s, user_id, best, "promoted", f"{labels[r.node_id]} narrowed to {labels[best]}", now)
                stats.promoted.append(f"{user_id}: {labels[r.node_id]} > {labels[best]}")

        # Discovery: an off-map niche (nothing above it on the map) the user keeps engaging with.
        mapped = {n for n, r in rows.items() if r.status in ON_MAP}
        for node, count in pos_count.items():
            if node in mapped or node in state.removed or count < mp.discover_min_positive_events:
                continue
            if state.niche_affinity(node) < um.rings.regular_min or any(a in mapped for a in chain(node)[1:]):
                continue
            # Only the deepest qualifying node: skip a node if a child of it also qualifies.
            if any(pos_count.get(k, 0) >= mp.discover_min_positive_events
                   and state.niche_affinity(k) >= um.rings.regular_min for k, p in parents.items() if p == node):
                continue
            rows[node] = add_to_map(s, user_id, node, "discovered", config, now)
            mapped.add(node)
            log_change(s, user_id, node, "discovered", f"{labels[node]} joined your map", now)
            stats.discovered.append(f"{user_id}: {labels[node]}")

        # Fading: many skips and no positives for N weeks; N more weeks and it leaves the map.
        weeks = timedelta(weeks=nar.fade_weeks_without_positive)
        for r in [r for r in rows.values() if r.status in ON_MAP]:
            if r.node_id in state.pins:
                continue
            if pos_recent.get(r.node_id, 0):
                if r.status == "fading":
                    r.status, r.status_since = "active", now
                continue
            if r.status == "fading" and now - r.status_since >= weeks:
                r.status, r.status_since = "off", now
                log_change(s, user_id, r.node_id, "off_map", f"{labels[r.node_id]} left your map", now)
                stats.dropped.append(f"{user_id}: {labels[r.node_id]}")
            elif r.status != "fading" and neg_recent.get(r.node_id, 0) >= nar.fade_min_negative_events:
                r.status, r.status_since = "fading", now
                log_change(s, user_id, r.node_id, "fading",
                           f"{labels[r.node_id]} fading after {neg_recent[r.node_id]} skips", now)
                stats.fading.append(f"{user_id}: {labels[r.node_id]}")

        # Removed niches the user allowed to be re-tested come back into exploration after N days.
        cutoff = now - timedelta(days=um.removed_retest_after_days)
        stats.reopened += s.execute(delete(UserRemovedRow).where(
            UserRemovedRow.user_id == user_id, UserRemovedRow.retest_allowed.is_(True),
            UserRemovedRow.removed_at <= cutoff)).rowcount

    # Weekly snapshot (for Replay and "How my focus narrowed").
    view = map_view(store, config, user_id, now)
    with store.session() as s, s.begin():
        keep = ("node_id", "label", "path", "ring", "affinity", "radius", "angle", "status")
        s.merge(UserMapSnapshotRow(user_id=user_id, week=week_of(now), created_at=now,
                                   data={"nodes": [{k: n[k] for k in keep} for n in view["nodes"]]}))
    stats.snapshots += 1
    from .profile import sync
    stats.profile_updates += sync(store, config, user_id, clock=lambda: now)


def run_weekly(store: FeedStore, config: FeedConfig, clock: Callable[[], datetime] = utcnow) -> WeeklyStats:
    now = clock()
    stats = WeeklyStats(migrated=migrate_tree_changes(store))
    with store.session() as s:
        users = list(s.scalars(select(UserRow.id)))
    for uid in users:
        run_user(store, config, uid, now, stats)
        stats.users += 1
    return stats
