"""The user's niche map (section 6.5-6.6): membership, rings, positions and statuses, and the map
actions (pin, unpin, drop-in, remove, nudge, reset).

Position: distance from the centre is 1 - affinity (the map has radius 1); the angle is fixed when a
node joins. A node whose ancestor is on the map sits near that ancestor's angle; any other node takes
the middle of the widest free gap, so the map never jumps between sessions.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select

from signal_connectors.models import utcnow

from ..config import FeedConfig
from ..db import (
    EventRow,
    FeedStore,
    MapChangeLogRow,
    NicheRow,
    PostNicheRow,
    UserDropInRow,
    UserMapNodeRow,
    UserMapSnapshotRow,
    UserNicheAffinityRow,
    UserPinRow,
    UserRemovedRow,
    UserRow,
    UserSkillAffinityRow,
)

ON_MAP = ("active", "context", "fading")


class MapError(ValueError):
    """A map action that can't apply (unknown node, wrong tree)."""


# --- membership and angles -------------------------------------------------------

def _ancestors(s, node_id: int) -> list[int]:
    out, node = [], s.get(NicheRow, node_id)
    while node is not None and node.parent_id is not None:
        out.append(node.parent_id)
        node = s.get(NicheRow, node.parent_id)
    return out


def _widest_gap_middle(angles: list[float]) -> float:
    if not angles:
        return 0.0
    a = sorted(x % 360 for x in angles)
    gaps = [((a[(i + 1) % len(a)] - a[i]) % 360 or 360, a[i]) for i in range(len(a))]
    width, start = max(gaps)
    return round((start + width / 2) % 360, 2)


def add_to_map(s, user_id: str, node_id: int, source: str, config: FeedConfig, now: datetime,
               angle: float | None = None) -> UserMapNodeRow:
    """Put a node on the map (or back on it, keeping its old angle)."""
    row = s.get(UserMapNodeRow, (user_id, node_id))
    if row is not None:
        if row.status not in ON_MAP or row.status == "fading":
            row.status, row.status_since = "active", now
        if angle is not None:
            row.angle = angle % 360
        return row
    rows = {r.node_id: r for r in s.scalars(select(UserMapNodeRow).where(UserMapNodeRow.user_id == user_id,
                                                                           UserMapNodeRow.status.in_(ON_MAP)))}
    if angle is None:
        anchor = next((a for a in _ancestors(s, node_id) if a in rows), None)
        if anchor is not None:
            spread = config.map.child_angle_spread_deg
            taken = {round(r.angle, 2) for r in rows.values()}
            k = 1
            while True:   # +1, -1, +2, -2 ... spreads around the anchor
                cand = round((rows[anchor].angle + spread * ((k + 1) // 2) * (1 if k % 2 else -1)) % 360, 2)
                if cand not in taken:
                    angle = cand
                    break
                k += 1
        else:
            free = [r.angle for r in rows.values() if not any(a in rows for a in _ancestors(s, r.node_id))]
            angle = _widest_gap_middle(free)
    row = UserMapNodeRow(user_id=user_id, node_id=node_id, status="active", source=source, angle=angle % 360,
                         added_at=now, status_since=now)
    s.add(row)
    s.flush()
    return row


def log_change(s, user_id: str, node_id: int | None, kind: str, message: str, now: datetime) -> None:
    s.add(MapChangeLogRow(user_id=user_id, node_id=node_id, kind=kind, message=message, at=now))


def ensure_map(store: FeedStore, config: FeedConfig, user_id: str, clock: Callable[[], datetime] = utcnow) -> None:
    """Users created before the map existed get their quiz niches as map nodes."""
    with store.session() as s, s.begin():
        if s.scalar(select(func.count()).select_from(UserMapNodeRow).where(UserMapNodeRow.user_id == user_id)):
            return
        user = s.get(UserRow, user_id)
        for key in (user.feed_prefs or {}).get("priors", {}):
            if key.startswith("n"):
                add_to_map(s, user_id, int(key[1:]), "quiz", config, clock())


# --- the view --------------------------------------------------------------------

def _ring(affinity: float, config: FeedConfig) -> str:
    rings = config.user_model.rings
    return "core" if affinity >= rings.core_min else "regular" if affinity >= rings.regular_min else "exploring"


def map_view(store: FeedStore, config: FeedConfig, user_id: str, now: datetime | None = None) -> dict:
    from .usermodel import load_state

    now = now or utcnow()
    ensure_map(store, config, user_id, clock=lambda: now)
    state = load_state(store, config, user_id, now)
    with store.session() as s:
        rows = list(s.scalars(select(UserMapNodeRow).where(UserMapNodeRow.user_id == user_id,
                                                           UserMapNodeRow.status.in_(ON_MAP))))
        on_map = {r.node_id for r in rows}
        last = s.scalars(select(UserMapSnapshotRow).where(UserMapSnapshotRow.user_id == user_id,
                                                          UserMapSnapshotRow.week < (now - timedelta(days=now.weekday())).date())
                         .order_by(UserMapSnapshotRow.week.desc())).first()
        before = {n["node_id"]: n["affinity"] for n in (last.data.get("nodes", []) if last else [])}
        nodes = []
        for r in rows:
            niche = s.get(NicheRow, r.node_id)
            if niche is None or niche.status != "active":
                continue
            ancestors = _ancestors(s, r.node_id)
            path = [s.get(NicheRow, a).label for a in reversed(ancestors)] + [niche.label]
            affinity = round(state.niche_affinity(r.node_id), 4)
            radius = round(1 - affinity, 4)
            flags = []
            if (now - r.added_at).days < config.map.new_days:
                flags.append("new")
            if r.node_id in before and affinity - before[r.node_id] >= config.map.rising_delta:
                flags.append("rising")
            if r.status == "fading" or (r.node_id in before and before[r.node_id] - affinity >= config.map.rising_delta):
                flags.append("fading")
            alpha, beta = state.niche_uncertainty.get(r.node_id, (None, None))
            nodes.append({
                "node_id": r.node_id, "label": niche.label, "path": path,
                "parent_id": next((a for a in ancestors if a in on_map), None),
                "ring": _ring(affinity, config), "affinity": affinity,
                "uncertainty": round(math.sqrt(alpha * beta / ((alpha + beta) ** 2 * (alpha + beta + 1))), 4)
                if alpha else None,
                "radius": radius, "angle": r.angle,
                "x": round(radius * math.cos(math.radians(r.angle)), 4),
                "y": round(radius * math.sin(math.radians(r.angle)), 4),
                "status": r.status, "flags": flags, "source": r.source,
                "pinned": r.node_id in state.pins, "drop_in_exposures_left": state.drop_ins.get(r.node_id)})
    nodes.sort(key=lambda n: (-n["affinity"], n["label"]))
    return {"user_id": user_id, "radius": 1.0, "nodes": nodes}


def node_detail(store: FeedStore, config: FeedConfig, user_id: str, node_id: int, now: datetime | None = None) -> dict:
    """Path, place on the map, and the user's activity in the node's subtree. No prose about why."""
    now = now or utcnow()
    view = {n["node_id"]: n for n in map_view(store, config, user_id, now)["nodes"]}
    with store.session() as s:
        niche = s.get(NicheRow, node_id)
        if niche is None or niche.tree != "industry":
            raise MapError(f"unknown niche {node_id}")
        sub, frontier = {node_id}, [node_id]
        while frontier:
            kids = list(s.scalars(select(NicheRow.id).where(NicheRow.parent_id.in_(frontier),
                                                            NicheRow.status == "active")))
            sub |= set(kids)
            frontier = kids
        posts = select(PostNicheRow.post_id).where(PostNicheRow.niche_id.in_(sub), PostNicheRow.tree == "industry")
        counts = dict(s.execute(select(EventRow.event_type, func.count()).where(
            EventRow.user_id == user_id, EventRow.item_type == "post", EventRow.item_id.in_(posts))
            .group_by(EventRow.event_type)).all())
        path = [s.get(NicheRow, a).label for a in reversed(_ancestors(s, node_id))] + [niche.label]
    positive = sum(counts.get(t, 0) for t in ("save", "share", "id_build", "idea", "outreach", "interested"))
    return {"node_id": node_id, "label": niche.label, "path": path, "on_map": node_id in view,
            **({k: view[node_id][k] for k in ("ring", "affinity", "radius", "angle", "status", "flags", "source",
                                               "pinned")} if node_id in view else {}),
            "activity": {"saves": positive, "reads": counts.get("read", 0),
                         "skips": counts.get("scroll_past", 0) + counts.get("not_for_me", 0)}}


# --- actions -------------------------------------------------------------------------

def _niche(s, node_id: int) -> NicheRow:
    niche = s.get(NicheRow, node_id)
    if niche is None or niche.tree != "industry" or niche.status != "active":
        raise MapError(f"unknown niche {node_id}")
    return niche


def raise_affinity(s, user_id: str, node_id: int, mean: float, config: FeedConfig, now: datetime) -> None:
    """Lift a niche's affinity to at least `mean` by adding positive evidence (never by rewriting the
    belief: decay assumes alpha and beta only ever grow above the prior)."""
    from .usermodel import _decayed

    um = config.user_model
    prior = (s.get(UserRow, user_id).feed_prefs or {}).get("priors", {}).get(f"n{node_id}", um.default_affinity)
    row = s.get(UserNicheAffinityRow, (user_id, node_id))
    if row is None:
        row = UserNicheAffinityRow(user_id=user_id, node_id=node_id, alpha=prior * um.prior_strength,
                                   beta=(1 - prior) * um.prior_strength, updated_at=now)
        s.add(row)
    else:
        row.alpha, row.beta = _decayed(row.alpha, row.beta, prior, um.prior_strength,
                                       (now - row.updated_at).total_seconds() / 86400, um.evidence_half_life_days)
    mean = min(mean, 0.99)
    row.alpha = max(row.alpha, mean / (1 - mean) * row.beta)
    row.updated_at = now


def pin(store: FeedStore, config: FeedConfig, user_id: str, node_id: int, radius: float, angle: float | None = None,
        clock: Callable[[], datetime] = utcnow) -> None:
    """Fix a node where the user placed it: affinity = 1 - radius (section 6.4 "Pins override")."""
    now = clock()
    with store.session() as s, s.begin():
        niche = _niche(s, node_id)
        affinity = round(min(1.0, max(0.0, 1 - radius)), 4)
        s.execute(delete(UserRemovedRow).where(UserRemovedRow.user_id == user_id, UserRemovedRow.node_id == node_id))
        row = s.get(UserPinRow, (user_id, node_id))
        if row is None:
            s.add(UserPinRow(user_id=user_id, node_id=node_id, affinity=affinity, angle=angle, pinned_at=now))
        else:
            row.affinity, row.angle, row.pinned_at = affinity, angle, now
        add_to_map(s, user_id, node_id, "pin", config, now, angle)
        log_change(s, user_id, node_id, "pinned", f"You pinned {niche.label}", now)


def unpin(store: FeedStore, config: FeedConfig, user_id: str, node_id: int, clock: Callable[[], datetime] = utcnow) -> None:
    """Back to learned behavior: events kept accumulating underneath the pin."""
    now = clock()
    with store.session() as s, s.begin():
        niche = _niche(s, node_id)
        if s.execute(delete(UserPinRow).where(UserPinRow.user_id == user_id, UserPinRow.node_id == node_id)).rowcount:
            log_change(s, user_id, node_id, "unpinned", f"You unpinned {niche.label}", now)


def drop_in(store: FeedStore, config: FeedConfig, user_id: str, node_id: int, clock: Callable[[], datetime] = utcnow) -> None:
    """Add a node from the catalog: affinity starts at 0.35 with a budget of 8 exposures (section 6.4)."""
    um, now = config.user_model, clock()
    with store.session() as s, s.begin():
        niche = _niche(s, node_id)
        s.execute(delete(UserRemovedRow).where(UserRemovedRow.user_id == user_id, UserRemovedRow.node_id == node_id))
        raise_affinity(s, user_id, node_id, um.drop_in_affinity, config, now)
        row = s.get(UserDropInRow, (user_id, node_id))
        if row is None:
            s.add(UserDropInRow(user_id=user_id, node_id=node_id, exposures_left=um.drop_in_exposure_budget, added_at=now))
        else:
            row.exposures_left, row.added_at = um.drop_in_exposure_budget, now
        add_to_map(s, user_id, node_id, "drop_in", config, now)
        log_change(s, user_id, node_id, "dropped_in", f"You added {niche.label}", now)


def remove(store: FeedStore, config: FeedConfig, user_id: str, node_id: int, allow_retest: bool = False,
           clock: Callable[[], datetime] = utcnow) -> None:
    """Affinity forced to 0 for the node and everything under it; re-testable after 60 days only if the
    user allows it (section 6.1)."""
    now = clock()
    with store.session() as s, s.begin():
        niche = _niche(s, node_id)
        row = s.get(UserRemovedRow, (user_id, node_id))
        if row is None:
            s.add(UserRemovedRow(user_id=user_id, node_id=node_id, retest_allowed=allow_retest, removed_at=now))
        else:
            row.retest_allowed, row.removed_at = allow_retest, now
        s.execute(delete(UserPinRow).where(UserPinRow.user_id == user_id, UserPinRow.node_id == node_id))
        s.execute(delete(UserDropInRow).where(UserDropInRow.user_id == user_id, UserDropInRow.node_id == node_id))
        for m in s.scalars(select(UserMapNodeRow).where(UserMapNodeRow.user_id == user_id,
                                                        UserMapNodeRow.status.in_(ON_MAP))):
            if m.node_id == node_id or node_id in _ancestors(s, m.node_id):
                m.status, m.status_since = "off", now
        log_change(s, user_id, node_id, "removed", f"You removed {niche.label}", now)


def nudge(store: FeedStore, config: FeedConfig, user_id: str, node_id: int, direction: str,
          clock: Callable[[], datetime] = utcnow) -> None:
    """"Show more" / "Show less": counts as an event of `map.nudge_weight` on the node itself."""
    from .usermodel import _bump

    if direction not in ("more", "less"):
        raise MapError("direction must be 'more' or 'less'")
    um, now = config.user_model, clock()
    with store.session() as s, s.begin():
        _niche(s, node_id)
        prior = (s.get(UserRow, user_id).feed_prefs or {}).get("priors", {}).get(f"n{node_id}", um.default_affinity)
        sign = 1 if direction == "more" else -1
        _bump(s, UserNicheAffinityRow, user_id, node_id, sign * config.map.nudge_weight * um.evidence_per_weight,
              prior, config, now)
        if direction == "more":
            add_to_map(s, user_id, node_id, "discovered", config, now)


def reset(store: FeedStore, config: FeedConfig, user_id: str, clock: Callable[[], datetime] = utcnow) -> None:
    """Section 14: back to the quiz. Affinities, pins, drop-ins, removals and the map are cleared and the
    quiz priors re-seeded; hidden sources and "already exists" marks stay."""
    um, now = config.user_model, clock()
    with store.session() as s, s.begin():
        for cls in (UserNicheAffinityRow, UserSkillAffinityRow, UserPinRow, UserDropInRow, UserRemovedRow,
                    UserMapNodeRow):
            s.execute(delete(cls).where(cls.user_id == user_id))
        user = s.get(UserRow, user_id)
        user.type_preference, user.source_preference = {}, {}
        for key, mean in (user.feed_prefs or {}).get("priors", {}).items():
            cls = UserNicheAffinityRow if key[0] == "n" else UserSkillAffinityRow
            s.add(cls(user_id=user_id, node_id=int(key[1:]), alpha=mean * um.prior_strength,
                      beta=(1 - mean) * um.prior_strength, updated_at=now))
        s.flush()
        for key in (user.feed_prefs or {}).get("priors", {}):
            if key[0] == "n":
                add_to_map(s, user_id, int(key[1:]), "quiz", config, now)
        log_change(s, user_id, None, "reset", "You reset your map", now)
