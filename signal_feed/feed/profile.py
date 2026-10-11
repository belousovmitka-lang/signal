"""Profile sync (section 6.6, 14): the map proposes, the user decides.

A niche that is in Core with real evidence (enough positive actions in it, or pinned there) becomes a
pending "add"; a published niche that left the map becomes a pending "remove". Nothing is published
without approval unless the user turned on auto-publish. Suggestions are neutral, like the rest of the
app: the niche and the action, never why. Public profiles show broad areas (industries); connections
see the exact niches.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import func, select

from signal_connectors.models import utcnow

from ..config import FeedConfig
from ..db import (
    EventRow,
    FeedStore,
    NicheRow,
    PostNicheRow,
    ProfilePendingUpdateRow,
    UserProfileNicheRow,
    UserRemovedRow,
    UserRow,
)
from .usermap import ON_MAP, map_view
from .usermodel import POSITIVE_ACTIONS

Audience = Literal["public", "connection", "self"]


class ProfileError(ValueError):
    pass


def _subtree(s, node_id: int) -> set[int]:
    out, frontier = {node_id}, [node_id]
    while frontier:
        kids = list(s.scalars(select(NicheRow.id).where(NicheRow.parent_id.in_(frontier))))
        out |= set(kids)
        frontier = kids
    return out


def _positive_events(s, user_id: str, node_id: int) -> int:
    posts = select(PostNicheRow.post_id).where(PostNicheRow.niche_id.in_(_subtree(s, node_id)),
                                               PostNicheRow.tree == "industry")
    return s.scalar(select(func.count()).select_from(EventRow).where(
        EventRow.user_id == user_id, EventRow.item_type == "post", EventRow.item_id.in_(posts),
        EventRow.event_type.in_(POSITIVE_ACTIONS), EventRow.weight > 0)) or 0


def _path(s, node_id: int) -> list[str]:
    out, node = [], s.get(NicheRow, node_id)
    while node is not None:
        out.append(node.label)
        node = s.get(NicheRow, node.parent_id) if node.parent_id is not None else None
    return out[::-1]


def _publish(s, user_id: str, node_id: int, add: bool, now: datetime) -> None:
    row = s.get(UserProfileNicheRow, (user_id, node_id))
    if add and row is None:
        s.add(UserProfileNicheRow(user_id=user_id, node_id=node_id, published_at=now))
    elif not add and row is not None:
        s.delete(row)


def sync(store: FeedStore, config: FeedConfig, user_id: str, clock: Callable[[], datetime] = utcnow) -> int:
    """Create pending updates for what changed since the last sync; apply them at once with auto-publish.
    Idempotent. Returns how many updates were created."""
    now = clock()
    cfg = config.profile
    view = map_view(store, config, user_id, now)
    core = {n["node_id"] for n in view["nodes"] if n["ring"] == "core" and n["status"] in ON_MAP}
    pinned = {n["node_id"] for n in view["nodes"] if n["pinned"]}
    on_map = {n["node_id"] for n in view["nodes"]}
    created = 0
    with store.session() as s, s.begin():
        user = s.get(UserRow, user_id)
        published = set(s.scalars(select(UserProfileNicheRow.node_id).where(UserProfileNicheRow.user_id == user_id)))
        removed = set(s.scalars(select(UserRemovedRow.node_id).where(UserRemovedRow.user_id == user_id)))
        pending = {(u.node_id, u.action): u for u in s.scalars(select(ProfilePendingUpdateRow).where(
            ProfilePendingUpdateRow.user_id == user_id, ProfilePendingUpdateRow.status == "pending"))}
        declined_since = now - timedelta(days=cfg.resuggest_after_days)
        declined = set(s.scalars(select(ProfilePendingUpdateRow.node_id).where(
            ProfilePendingUpdateRow.user_id == user_id, ProfilePendingUpdateRow.action == "add",
            ProfilePendingUpdateRow.status == "rejected", ProfilePendingUpdateRow.decided_at >= declined_since)))
        wanted: list[tuple[int, str]] = []
        for node in sorted(core - published - declined):
            if node in pinned or _positive_events(s, user_id, node) >= cfg.min_positive_events:
                wanted.append((node, "add"))
        for node in sorted(published - on_map) + sorted(published & removed):
            wanted.append((node, "remove"))
        # A pending suggestion that no longer holds is withdrawn (e.g. an "add" for a niche that left Core).
        still = set(wanted)
        for key, u in pending.items():
            if key not in still:
                u.status, u.decided_at = "withdrawn", now
        for node, action in dict.fromkeys(wanted):
            if (node, action) in pending:
                continue
            if user.auto_publish_profile:
                _publish(s, user_id, node, action == "add", now)
                s.add(ProfilePendingUpdateRow(user_id=user_id, node_id=node, action=action, status="approved",
                                              created_at=now, decided_at=now))
            else:
                s.add(ProfilePendingUpdateRow(user_id=user_id, node_id=node, action=action, status="pending",
                                              created_at=now))
            created += 1
    return created


def pending_updates(store: FeedStore, config: FeedConfig, user_id: str, clock: Callable[[], datetime] = utcnow) -> list[dict]:
    """Neutral suggestions: which niche, add or remove. Nothing about why."""
    sync(store, config, user_id, clock)
    with store.session() as s:
        rows = list(s.scalars(select(ProfilePendingUpdateRow).where(
            ProfilePendingUpdateRow.user_id == user_id, ProfilePendingUpdateRow.status == "pending")
            .order_by(ProfilePendingUpdateRow.id)))
        return [{"update_id": u.id, "node_id": u.node_id, "label": s.get(NicheRow, u.node_id).label,
                 "path": _path(s, u.node_id), "action": u.action} for u in rows]


def decide(store: FeedStore, user_id: str, decisions: Iterable[tuple[int, bool]],
           clock: Callable[[], datetime] = utcnow) -> int:
    """Approve or decline pending updates. Unknown or already decided ids raise ProfileError."""
    now = clock()
    done = 0
    with store.session() as s, s.begin():
        for update_id, approve in decisions:
            u = s.get(ProfilePendingUpdateRow, update_id)
            if u is None or u.user_id != user_id or u.status != "pending":
                raise ProfileError(f"no pending update {update_id}")
            u.status, u.decided_at = ("approved" if approve else "rejected"), now
            if approve:
                _publish(s, user_id, u.node_id, u.action == "add", now)
            done += 1
    return done


def unpublish(store: FeedStore, user_id: str, node_id: int) -> bool:
    """The user takes a niche off their profile directly (it stays on their map)."""
    with store.session() as s, s.begin():
        row = s.get(UserProfileNicheRow, (user_id, node_id))
        if row is None:
            return False
        s.delete(row)
        return True


def set_auto_publish(store: FeedStore, user_id: str, on: bool) -> None:
    with store.session() as s, s.begin():
        s.get(UserRow, user_id).auto_publish_profile = on


def profile_niches(store: FeedStore, user_id: str, audience: Audience) -> dict:
    """Section 14: public profiles show broad areas, connections see exact niches. Raw events never leave."""
    with store.session() as s:
        rows = list(s.scalars(select(UserProfileNicheRow).where(UserProfileNicheRow.user_id == user_id)
                              .order_by(UserProfileNicheRow.published_at)))
        paths = [_path(s, r.node_id) for r in rows if s.get(NicheRow, r.node_id) is not None]
    if audience == "public":
        return {"user_id": user_id, "areas": list(dict.fromkeys(p[0] for p in paths))}
    return {"user_id": user_id, "areas": list(dict.fromkeys(p[0] for p in paths)),
            "niches": [{"label": p[-1], "path": p} for p in paths]}
