"""Phase 4: the user map, map actions, the weekly job (narrowing, discovery, fading) and /map endpoints."""

import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from signal_feed.api import create_app
from signal_feed.db import (
    EventRow,
    MapChangeLogRow,
    NicheRow,
    UserDropInRow,
    UserMapNodeRow,
    UserMapSnapshotRow,
    UserNicheAffinityRow,
)
from signal_feed.feed import usermap
from signal_feed.feed.data import FeedDataCache
from signal_feed.feed.service import FeedService
from signal_feed.feed.usermodel import apply_events, load_state, onboard
from signal_feed.feed.weekly import WeeklyStats, migrate_tree_changes, run_user

from .test_feed import NOW, WORLD, world  # noqa: F401  (pytest fixture)


def clock(days=0):
    return lambda: NOW + timedelta(days=days)


def view(feed_store, feed_config, uid, days=0):
    return {n["label"]: n for n in usermap.map_view(feed_store, feed_config, uid, NOW + timedelta(days=days))["nodes"]}


def post_ids(raw, industry, branch, ptype=None):
    out, k = [], 0
    for ind, br, pt, n, _ in WORLD:
        for _ in range(n):
            if ind == industry and br == branch and (ptype is None or pt == ptype):
                out.append(raw[k].id)
            k += 1
    return out


def events(feed_store, reader, feed_config, uid, items, etype, weight, days_ago=1):
    rows = [EventRow(user_id=uid, client_event_id=uuid.uuid4().hex, session_id="s", item_type="post", item_id=i,
                     event_type=etype, weight=weight, at=NOW - timedelta(days=days_ago), received_at=NOW,
                     config_version=1) for i in items]
    with feed_store.session() as s, s.begin():
        s.add_all(rows)
    apply_events(feed_store, reader, feed_config, rows, clock=lambda: NOW - timedelta(days=days_ago))


def test_onboarding_puts_the_quiz_on_the_map_with_stable_angles(world, feed_store, feed_config):
    _, nodes, _, _, _ = world
    uid = onboard(feed_store, feed_config, ["Legal", "Healthcare", "Legal > Contract work"], [], clock=clock())
    m = view(feed_store, feed_config, uid)
    assert set(m) == {"Legal", "Healthcare", "Contract work"}
    legal, health, contract = m["Legal"], m["Healthcare"], m["Contract work"]
    assert legal["ring"] == "core" and legal["radius"] == pytest.approx(0.4) and "new" in legal["flags"]
    assert health["angle"] == pytest.approx((legal["angle"] + 180) % 360)          # widest gap
    assert contract["parent_id"] == nodes["Legal"]
    assert abs(contract["angle"] - legal["angle"]) == pytest.approx(feed_config.map.child_angle_spread_deg)
    assert view(feed_store, feed_config, uid, days=3)["Legal"]["angle"] == legal["angle"]    # never moves


def test_pin_overrides_learning_and_unpin_restores_it(world, feed_store, feed_config):
    _, nodes, _, _, _ = world
    uid = onboard(feed_store, feed_config, ["Legal"], [], clock=clock())
    usermap.pin(feed_store, feed_config, uid, nodes["Agriculture"], radius=0.2, angle=90, clock=clock())
    agri = view(feed_store, feed_config, uid)["Agriculture"]
    assert (agri["affinity"], agri["ring"], agri["angle"], agri["pinned"]) == (0.8, "core", 90, True)
    assert agri["y"] == pytest.approx(0.2) and agri["x"] == pytest.approx(0.0, abs=1e-4)
    usermap.unpin(feed_store, feed_config, uid, nodes["Agriculture"], clock=clock())
    agri = view(feed_store, feed_config, uid)["Agriculture"]
    assert not agri["pinned"] and agri["affinity"] == pytest.approx(feed_config.user_model.default_affinity)
    with pytest.raises(usermap.MapError):
        usermap.pin(feed_store, feed_config, uid, 999999, radius=0.5)


def test_drop_in_remove_nudge_and_reset(world, feed_store, feed_config):
    _, nodes, _, _, _ = world
    uid = onboard(feed_store, feed_config, ["Legal"], [], clock=clock())
    farm = nodes["Agriculture > Farm equipment"]
    usermap.drop_in(feed_store, feed_config, uid, farm, clock=clock())
    m = view(feed_store, feed_config, uid)
    assert m["Farm equipment"]["affinity"] == pytest.approx(0.35)
    assert m["Farm equipment"]["drop_in_exposures_left"] == feed_config.user_model.drop_in_exposure_budget
    usermap.nudge(feed_store, feed_config, uid, farm, "more", clock=clock())
    assert view(feed_store, feed_config, uid)["Farm equipment"]["affinity"] > 0.35
    usermap.nudge(feed_store, feed_config, uid, farm, "less", clock=clock())
    usermap.nudge(feed_store, feed_config, uid, farm, "less", clock=clock())
    assert view(feed_store, feed_config, uid)["Farm equipment"]["affinity"] < 0.35
    with pytest.raises(usermap.MapError):
        usermap.nudge(feed_store, feed_config, uid, farm, "sideways")
    # Removing Legal takes it and everything under it off the map, and its affinity to 0.
    usermap.drop_in(feed_store, feed_config, uid, nodes["Legal > Contract work"], clock=clock())
    usermap.remove(feed_store, feed_config, uid, nodes["Legal"], clock=clock())
    m = view(feed_store, feed_config, uid)
    assert "Legal" not in m and "Contract work" not in m
    assert load_state(feed_store, feed_config, uid, NOW).niche_affinity(nodes["Legal"]) == 0
    usermap.reset(feed_store, feed_config, uid, clock=clock())
    m = view(feed_store, feed_config, uid)
    assert set(m) == {"Legal"} and m["Legal"]["affinity"] == pytest.approx(0.6)
    with feed_store.session() as s:
        kinds = [c.kind for c in s.scalars(select(MapChangeLogRow).where(MapChangeLogRow.user_id == uid))]
    assert kinds == ["dropped_in", "dropped_in", "removed", "reset"]


def test_weekly_narrows_to_the_child_the_user_engages_with(world, feed_store, feed_config):
    reader, nodes, _, raw, _ = world
    uid = onboard(feed_store, feed_config, ["Legal"], [], clock=clock(-30))
    lit = post_ids(raw, "Legal", "Litigation support")
    events(feed_store, reader, feed_config, uid, lit[:6], "save", 4.0)
    events(feed_store, reader, feed_config, uid, post_ids(raw, "Legal", "Contract work")[:1], "save", 4.0)
    stats = WeeklyStats()
    run_user(feed_store, feed_config, uid, NOW, stats)
    assert stats.promoted == [f"{uid}: Legal > Litigation support"]
    m = view(feed_store, feed_config, uid)
    assert m["Legal"]["status"] == "context" and m["Litigation support"]["source"] == "promoted"
    assert m["Litigation support"]["affinity"] >= round(m["Legal"]["affinity"] * 0.9, 2) - 0.05
    with feed_store.session() as s:
        log = [c.message for c in s.scalars(select(MapChangeLogRow).where(MapChangeLogRow.user_id == uid))]
        snap = s.get(UserMapSnapshotRow, (uid, (NOW - timedelta(days=NOW.weekday())).date()))
    assert log == ["Legal narrowed to Litigation support"] and snap is not None
    again = WeeklyStats()
    run_user(feed_store, feed_config, uid, NOW, again)
    assert again.promoted == []                                             # already promoted


def test_weekly_discovers_and_fades(world, feed_store, feed_config):
    reader, nodes, _, raw, _ = world
    uid = onboard(feed_store, feed_config, ["Legal", "Healthcare"], [], clock=clock(-60))
    # Saves outside the map, in Agriculture > Farm equipment: it joins the map.
    events(feed_store, reader, feed_config, uid, post_ids(raw, "Agriculture", "Farm equipment")[:6], "save", 4.0)
    # Skips and no positives in Healthcare: it fades, and three weeks later leaves the map.
    events(feed_store, reader, feed_config, uid, post_ids(raw, "Healthcare", "Pharmacy")[:10], "not_for_me", -6)
    stats = WeeklyStats()
    run_user(feed_store, feed_config, uid, NOW, stats)
    assert stats.discovered == [f"{uid}: Farm equipment"]
    assert stats.fading == [f"{uid}: Healthcare"]
    m = view(feed_store, feed_config, uid)
    assert m["Farm equipment"]["source"] == "discovered" and "fading" in m["Healthcare"]["flags"]
    later = WeeklyStats()
    run_user(feed_store, feed_config, uid, NOW + timedelta(weeks=3), later)
    assert later.dropped == [f"{uid}: Healthcare"]
    assert "Healthcare" not in view(feed_store, feed_config, uid, days=21)


def test_tree_changes_move_user_state_to_the_survivor(world, feed_store, feed_config):
    _, nodes, _, _, _ = world
    uid = onboard(feed_store, feed_config, ["Legal > Contract work"], [], clock=clock())
    with feed_store.session() as s, s.begin():
        old = s.get(NicheRow, nodes["Legal > Contract work"])
        old.status, old.merged_into_id = "merged", nodes["Legal > Litigation support"]
    assert migrate_tree_changes(feed_store) == 2                              # affinity row + map row
    with feed_store.session() as s:
        assert s.get(UserMapNodeRow, (uid, nodes["Legal > Litigation support"])) is not None
        assert s.get(UserNicheAffinityRow, (uid, nodes["Legal > Litigation support"])) is not None


def test_map_endpoints(world, feed_store, feed_config):
    reader, nodes, _, raw, _ = world
    client = TestClient(create_app(feed_store, reader, feed_config,
                                   FeedService(feed_store, feed_config, FeedDataCache(feed_store, reader), seed=1)))
    uid = client.post("/onboarding", json={"industries": ["Legal"]}).json()["user_id"]
    m = client.get("/map", params={"user_id": uid}).json()
    assert [n["label"] for n in m["nodes"]] == ["Legal"] and m["radius"] == 1.0
    farm = nodes["Agriculture > Farm equipment"]
    assert client.post("/map/drop-in", json={"user_id": uid, "node_id": farm}).json() == {"ok": True}
    assert client.post("/map/pin", json={"user_id": uid, "node_id": farm, "radius": 0.3}).status_code == 200
    assert client.post("/map/nudge", json={"user_id": uid, "node_id": farm, "direction": "less"}).status_code == 200
    detail = client.get(f"/map/node/{farm}", params={"user_id": uid}).json()
    assert detail["path"] == ["Agriculture", "Farm equipment"] and detail["pinned"] and detail["affinity"] == 0.7
    assert set(detail["activity"]) == {"saves", "reads", "skips"} and not {"source", "flags", "why"} & set(detail)
    # The map is silent: no flags, sources or change log reach the app.
    node = next(n for n in client.get("/map", params={"user_id": uid}).json()["nodes"] if n["node_id"] == farm)
    assert not {"flags", "source", "drop_in_exposures_left"} & set(node) and node["status"] == "active"
    history = client.get("/map/history", params={"user_id": uid}).json()
    assert set(history) == {"snapshots"}
    with feed_store.session() as s:
        assert [c.kind for c in s.scalars(select(MapChangeLogRow).where(MapChangeLogRow.user_id == uid))] == \
            ["dropped_in", "pinned"]                                         # still logged internally
    assert client.post("/map/remove", json={"user_id": uid, "node_id": farm, "allow_retest": True}).status_code == 200
    assert client.post("/map/unpin", json={"user_id": uid, "node_id": farm}).status_code == 200
    assert client.post("/map/reset", json={"user_id": uid}).status_code == 200
    assert client.get("/map", params={"user_id": "ghost"}).status_code == 404
    assert client.post("/map/pin", json={"user_id": uid, "node_id": 999999, "radius": 0.1}).status_code == 404
    assert client.post("/map/pin", json={"user_id": uid, "node_id": farm, "radius": 2}).status_code == 422
    with feed_store.session() as s:
        assert s.get(UserDropInRow, (uid, farm)) is None                    # reset cleared it
