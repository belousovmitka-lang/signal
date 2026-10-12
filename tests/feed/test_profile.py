"""Phase 5: profile sync. The map proposes, the user approves; public profiles show broad areas only."""

from fastapi.testclient import TestClient
from sqlalchemy import select

from signal_feed.api import create_app
from signal_feed.db import ProfilePendingUpdateRow, UserProfileNicheRow
from signal_feed.feed import profile, usermap
from signal_feed.feed.data import FeedDataCache
from signal_feed.feed.service import FeedService
from signal_feed.feed.usermodel import onboard
from signal_feed.feed.weekly import WeeklyStats, run_user

from .test_feed import NOW, world  # noqa: F401  (pytest fixture)
from .test_map import clock, events, post_ids


def pending(feed_store, feed_config, uid, days=0):
    return [(u["label"], u["action"]) for u in profile.pending_updates(feed_store, feed_config, uid, clock(days))]


def test_core_niches_are_suggested_only_with_evidence(world, feed_store, feed_config):
    reader, nodes, _, raw, _ = world
    uid = onboard(feed_store, feed_config, ["Legal"], [], clock=clock(-1))
    assert pending(feed_store, feed_config, uid) == []                 # quiz choice alone: no suggestion
    events(feed_store, reader, feed_config, uid, post_ids(raw, "Legal", "Litigation support")[:3], "save", 4.0)
    updates = profile.pending_updates(feed_store, feed_config, uid, clock())
    assert [(u["label"], u["action"], u["path"]) for u in updates] == [("Legal", "add", ["Legal"])]
    assert set(updates[0]) == {"update_id", "node_id", "label", "path", "action"}     # neutral: no reason
    assert profile.profile_niches(feed_store, uid, "public") == {"user_id": uid, "areas": []}   # not yet
    profile.decide(feed_store, uid, [(updates[0]["update_id"], True)], clock())
    assert profile.profile_niches(feed_store, uid, "public")["areas"] == ["Legal"]
    assert pending(feed_store, feed_config, uid) == []                 # published: nothing left to ask


def test_pins_suggest_at_once_and_public_shows_only_broad_areas(world, feed_store, feed_config):
    _, nodes, _, _, _ = world
    uid = onboard(feed_store, feed_config, ["Legal"], [], clock=clock(-1))
    usermap.pin(feed_store, feed_config, uid, nodes["Healthcare > Pharmacy"], radius=0.1, clock=clock())
    [update] = profile.pending_updates(feed_store, feed_config, uid, clock())
    assert (update["label"], update["action"]) == ("Pharmacy", "add")
    profile.decide(feed_store, uid, [(update["update_id"], True)], clock())
    assert profile.profile_niches(feed_store, uid, "public") == {"user_id": uid, "areas": ["Healthcare"]}
    connection = profile.profile_niches(feed_store, uid, "connection")
    assert connection["niches"] == [{"label": "Pharmacy", "path": ["Healthcare", "Pharmacy"]}]


def test_declined_suggestions_wait_and_stale_ones_are_withdrawn(world, feed_store, feed_config):
    _, nodes, _, _, _ = world
    uid = onboard(feed_store, feed_config, ["Legal"], [], clock=clock(-1))
    pharmacy = nodes["Healthcare > Pharmacy"]
    usermap.pin(feed_store, feed_config, uid, pharmacy, radius=0.1, clock=clock())
    [update] = profile.pending_updates(feed_store, feed_config, uid, clock())
    profile.decide(feed_store, uid, [(update["update_id"], False)], clock())
    assert pending(feed_store, feed_config, uid, days=30) == []
    assert pending(feed_store, feed_config, uid, days=91) == [("Pharmacy", "add")]   # offered again later
    # The pin moves out to the rim: the niche leaves Core and the open suggestion is withdrawn.
    usermap.pin(feed_store, feed_config, uid, pharmacy, radius=0.9, clock=clock(91))
    assert pending(feed_store, feed_config, uid, days=91) == []
    with feed_store.session() as s:
        statuses = [u.status for u in s.scalars(select(ProfilePendingUpdateRow).order_by(ProfilePendingUpdateRow.id))]
    assert statuses == ["rejected", "withdrawn"]


def test_leaving_the_map_suggests_removal(world, feed_store, feed_config):
    _, nodes, _, _, _ = world
    uid = onboard(feed_store, feed_config, ["Legal"], [], clock=clock(-1))
    usermap.pin(feed_store, feed_config, uid, nodes["Legal"], radius=0.1, clock=clock())
    [add] = profile.pending_updates(feed_store, feed_config, uid, clock())
    profile.decide(feed_store, uid, [(add["update_id"], True)], clock())
    usermap.remove(feed_store, feed_config, uid, nodes["Legal"], clock=clock())
    [remove] = profile.pending_updates(feed_store, feed_config, uid, clock())
    assert (remove["label"], remove["action"]) == ("Legal", "remove")
    assert profile.profile_niches(feed_store, uid, "public")["areas"] == ["Legal"]   # still there until approved
    profile.decide(feed_store, uid, [(remove["update_id"], True)], clock())
    assert profile.profile_niches(feed_store, uid, "public")["areas"] == []


def test_auto_publish_skips_the_question(world, feed_store, feed_config):
    _, nodes, _, _, _ = world
    uid = onboard(feed_store, feed_config, ["Legal"], [], clock=clock(-1))
    profile.set_auto_publish(feed_store, uid, True)
    usermap.pin(feed_store, feed_config, uid, nodes["Legal"], radius=0.1, clock=clock())
    assert profile.sync(feed_store, feed_config, uid, clock()) == 1
    assert pending(feed_store, feed_config, uid) == []
    assert profile.profile_niches(feed_store, uid, "public")["areas"] == ["Legal"]
    with feed_store.session() as s:
        assert [u.status for u in s.scalars(select(ProfilePendingUpdateRow))] == ["approved"]   # recorded


def test_weekly_job_syncs_profiles(world, feed_store, feed_config):
    _, nodes, _, _, _ = world
    uid = onboard(feed_store, feed_config, ["Legal"], [], clock=clock(-1))
    usermap.pin(feed_store, feed_config, uid, nodes["Legal"], radius=0.1, clock=clock())
    stats = WeeklyStats()
    run_user(feed_store, feed_config, uid, NOW, stats)
    assert stats.profile_updates == 1


def test_profile_endpoints(world, feed_store, feed_config):
    reader, nodes, _, _, _ = world
    client = TestClient(create_app(feed_store, reader, feed_config,
                                   FeedService(feed_store, feed_config, FeedDataCache(feed_store, reader), seed=1)))
    uid = client.post("/onboarding", json={"industries": ["Legal"]}).json()["user_id"]
    client.post("/map/pin", json={"user_id": uid, "node_id": nodes["Legal > Contract work"], "radius": 0.1})
    [update] = client.get("/profile/niches/pending", params={"user_id": uid}).json()["updates"]
    assert update["path"] == ["Legal", "Contract work"] and update["action"] == "add"
    bad = client.post("/profile/niches/approve", json={"user_id": uid, "decisions": [{"update_id": 999, "approve": True}]})
    assert bad.status_code == 404
    ok = client.post("/profile/niches/approve",
                     json={"user_id": uid, "decisions": [{"update_id": update["update_id"], "approve": True}]})
    assert ok.json() == {"decided": 1}
    assert client.get("/profile/niches", params={"user_id": uid}).json()["areas"] == ["Legal"]
    exact = client.get("/profile/niches", params={"user_id": uid, "audience": "connection"}).json()
    assert exact["niches"][0]["label"] == "Contract work"
    assert client.post("/profile/niches/unpublish",
                       json={"user_id": uid, "node_id": nodes["Legal > Contract work"]}).json() == {"ok": True}
    assert client.post("/profile/niches/unpublish",
                       json={"user_id": uid, "node_id": nodes["Legal > Contract work"]}).status_code == 404
    assert client.post("/profile/auto-publish", json={"user_id": uid, "on": True}).json() == {"ok": True}
    assert client.get("/profile/niches/pending", params={"user_id": "ghost"}).status_code == 404
    with feed_store.session() as s:
        assert s.scalar(select(UserProfileNicheRow).where(UserProfileNicheRow.user_id == uid)) is None


def test_calibration_answers_are_not_profile_evidence(world, feed_store, feed_config):
    reader, nodes, _, raw, _ = world
    uid = onboard(feed_store, feed_config, ["Legal"], [], clock=clock(-1))
    events(feed_store, reader, feed_config, uid, post_ids(raw, "Legal", "Litigation support")[:6], "interested", 2.0)
    assert pending(feed_store, feed_config, uid) == []          # first impressions, not evidence
