"""Phase 3: user model, candidates, ranker, composition, the feed service and its API."""

import uuid
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from signal_connectors.models import RawPost
from signal_connectors.store import Store
from signal_feed.api import create_app
from signal_feed.db import (
    EnrichedPostRow,
    EventRow,
    ImpressionRow,
    NicheRow,
    PostNicheRow,
    ProblemPostRow,
    ProblemRow,
    UserDropInRow,
    UserNicheAffinityRow,
    UserRemovedRow,
    UserRow,
    UserSkillAffinityRow,
)
from signal_feed.feed.candidates import EXPLORE, PIN, generate
from signal_feed.feed.compose import collapse, compose
from signal_feed.feed.data import FeedDataCache, load
from signal_feed.feed.ranker import RankerV1
from signal_feed.feed.service import FeedService
from signal_feed.feed.usermodel import UnknownNodeError, apply_events, load_state, onboard
from signal_feed.niches.tree import seed_tree
from signal_feed.posts import PostsReader

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
DIM = 16

# (industry, branch, post_type, how many, sources)
WORLD = [
    ("Legal", "Litigation support", "pain", 12, ["hn", "rss:lawsites"]),
    ("Legal", "Litigation support", "paid_demand", 6, ["sam_gov", "ted"]),
    ("Legal", "Contract work", "tool_gap", 8, ["github", "hn"]),
    ("Legal", "Contract work", "change", 6, ["federal_register"]),
    ("Healthcare", "Pharmacy", "pain", 10, ["openfda", "hn"]),
    ("Healthcare", "Medical devices", "paid_demand", 6, ["ted", "uk_tenders"]),
    ("Agriculture", "Farm equipment", "pain", 8, ["rss:farm", "lemmy"]),
]


def unit(v):
    return (v / np.linalg.norm(v)).tolist()


@pytest.fixture
def world(tmp_path, feed_store):
    rng = np.random.default_rng(3)          # per fixture: the same world whatever ran before
    seed_tree(feed_store)
    with feed_store.session() as s:
        nodes = {(n.label if n.parent_id is None else f"{s.get(NicheRow, n.parent_id).label} > {n.label}"): n.id
                 for n in s.scalars(select(NicheRow).where(NicheRow.tree == "industry"))}
        skills = {n.label: n.id for n in s.scalars(select(NicheRow).where(NicheRow.tree == "skill"))}
    topics = {f"{i} > {b}": rng.normal(size=DIM) for i, b, *_ in WORLD}
    raw, rows, placed = [], [], []
    for g, (industry, branch, ptype, n, sources) in enumerate(WORLD):
        key = f"{industry} > {branch}"
        for k in range(n):
            p = RawPost(source=sources[k % len(sources)], source_type="community", route="rss",
                        url=f"https://x.example/{g}/{k}", title=f"{key} {ptype} {k}", text=f"{key} {ptype} {k}",
                        is_snippet=False, published_at=NOW - timedelta(hours=6 + 20 * k))
            raw.append(p)
            vec = unit(topics[key] + rng.normal(scale=0.1, size=DIM))
            rows.append(EnrichedPostRow(
                post_id=p.id, status="ok", post_type=ptype, industry_path=[industry, branch],
                skills_needed=["Document parsing"] if industry == "Legal" else ["AI agents"],
                pain_statement=f"{key} {ptype} {k}", intensity=0.6, buyer_likelihood=0.5, quality=0.7,
                spam_score=0.0, toxicity=0.0, embedding=vec, pain_embedding=vec, prompt_version="t",
                money_signal={"kind": "contract_value"} if ptype == "paid_demand" else None, enriched_at=NOW))
            placed.append(PostNicheRow(post_id=p.id, niche_id=nodes[key], tree="industry", similarity=0.9))
            placed.append(PostNicheRow(post_id=p.id, tree="skill", similarity=None,
                                       niche_id=skills["Document parsing" if industry == "Legal" else "AI agents"]))
    url = f"sqlite:///{tmp_path / 'signal.db'}"
    Store(url).save_posts(raw, text_dedupe=False)
    with feed_store.session() as s, s.begin():
        s.add_all(rows)
        s.add_all(placed)
        # One problem holding the first three Litigation support pain posts (a duplicate group).
        prob = ProblemRow(niche_id=nodes["Legal > Litigation support"], title="t", kind="pain", score=0.6,
                          confidence=0.5, post_count=3, score_breakdown={"trend": 0.8}, updated_at=NOW)
        s.add(prob)
        s.flush()
        s.add_all(ProblemPostRow(problem_id=prob.id, post_id=raw[k].id, similarity=0.9) for k in range(3))
    return PostsReader(url), nodes, skills, raw, prob.id


def event(user_id, item_id, etype, weight, **kw):
    return EventRow(user_id=user_id, client_event_id=uuid.uuid4().hex, session_id="s", item_type="post",
                    item_id=item_id, event_type=etype, weight=weight, at=NOW, received_at=NOW, config_version=1, **kw)


# --- user model ----------------------------------------------------------------

def test_onboarding_seeds_weak_priors_and_resolves_branches(world, feed_store, feed_config):
    _, nodes, skills, _, _ = world
    uid = onboard(feed_store, feed_config, ["Legal", "Healthcare > Pharmacy"], ["AI agents"], ["Document parsing"],
                  clock=lambda: NOW)
    with feed_store.session() as s:
        legal = s.get(UserNicheAffinityRow, (uid, nodes["Legal"]))
        assert (legal.alpha, legal.beta) == pytest.approx((1.2, 0.8))           # 0.6 with strength 2
        assert s.get(UserNicheAffinityRow, (uid, nodes["Healthcare > Pharmacy"])) is not None
        resume = s.get(UserSkillAffinityRow, (uid, skills["Document parsing"]))
        assert resume.alpha / (resume.alpha + resume.beta) == pytest.approx(0.5)
    with pytest.raises(UnknownNodeError):
        onboard(feed_store, feed_config, ["Legal > No such branch"], [])


def test_events_move_the_leaf_its_parents_and_skills(world, feed_store, feed_config):
    reader, nodes, skills, raw, _ = world
    uid = onboard(feed_store, feed_config, ["Legal"], [], clock=lambda: NOW)
    before = load_state(feed_store, feed_config, uid, NOW)
    apply_events(feed_store, reader, feed_config, [event(uid, raw[0].id, "save", 4.0)], clock=lambda: NOW)
    after = load_state(feed_store, feed_config, uid, NOW)
    leaf, root = nodes["Legal > Litigation support"], nodes["Legal"]
    assert after.niche[leaf] > feed_config.user_model.default_affinity
    with feed_store.session() as s:
        leaf_row = s.get(UserNicheAffinityRow, (uid, leaf))
        root_row = s.get(UserNicheAffinityRow, (uid, root))
    assert leaf_row.alpha == pytest.approx(0.2 + 1.0)                       # default prior + 4 x 0.25
    assert root_row.alpha == pytest.approx(1.2 + 0.4)                       # quiz prior + 40%
    assert after.niche[root] > before.niche[root]
    assert after.skill[skills["Document parsing"]] > feed_config.user_model.default_affinity
    assert after.type_pref["pain"] == pytest.approx(0.08) and after.source_pref["hn"] == pytest.approx(0.04)


def test_not_for_me_reasons_route_the_penalty(world, feed_store, feed_config):
    reader, nodes, skills, raw, prob_id = world
    uid = onboard(feed_store, feed_config, ["Legal"], ["Document parsing"], clock=lambda: NOW)
    leaf = nodes["Legal > Litigation support"]
    apply_events(feed_store, reader, feed_config, [event(uid, raw[0].id, "not_for_me", -6, reason="not_my_skills")],
                 clock=lambda: NOW)
    st = load_state(feed_store, feed_config, uid, NOW)
    assert leaf not in st.niche and st.skill[skills["Document parsing"]] < 0.6      # skills only
    apply_events(feed_store, reader, feed_config, [
        event(uid, raw[1].id, "not_for_me", -6, reason="already_exists"),
        event(uid, raw[5].id, "not_for_me", -6, reason="too_small"),
        event(uid, raw[7].id, "hide_source", -4)], clock=lambda: NOW)
    st = load_state(feed_store, feed_config, uid, NOW)
    assert st.served_problems == {prob_id} and st.min_problem_score == pytest.approx(0.05)
    assert st.hidden_sources == {raw[7].source} and leaf not in st.niche          # never on the niche


def test_evidence_decays_by_half_life(world, feed_store, feed_config):
    reader, nodes, _, raw, _ = world
    uid = onboard(feed_store, feed_config, ["Healthcare"], [], clock=lambda: NOW)
    apply_events(feed_store, reader, feed_config, [event(uid, raw[0].id, "save", 4.0)], clock=lambda: NOW)
    leaf = nodes["Legal > Litigation support"]
    now = load_state(feed_store, feed_config, uid, NOW).niche[leaf]
    later = load_state(feed_store, feed_config, uid, NOW + timedelta(days=21)).niche[leaf]
    # Evidence 1.0 on Beta(0.2, 1.8): 1.2/3.0 now; half of it after one half-life: 0.7/2.5.
    assert now == pytest.approx(1.2 / 3.0) and later == pytest.approx(0.7 / 2.5)


# --- candidates, ranker, composition --------------------------------------------

def test_candidates_respect_exclusions_and_explore_off_the_map(world, feed_store, feed_config):
    reader, nodes, _, raw, prob_id = world
    uid = onboard(feed_store, feed_config, ["Legal"], [], clock=lambda: NOW)
    data = load(feed_store, reader)
    with feed_store.session() as s, s.begin():
        s.get(UserRow, uid).feed_prefs = {**s.get(UserRow, uid).feed_prefs, "hidden_sources": ["github"],
                                          "served_problems": [prob_id]}
        s.add(UserRemovedRow(user_id=uid, node_id=nodes["Agriculture"], removed_at=NOW))
        s.add(ImpressionRow(user_id=uid, session_id="old", item_type="post", item_id=raw[4].id, position=0,
                            shown_at=NOW - timedelta(days=1), config_version=1))
    st = load_state(feed_store, feed_config, uid, NOW)
    cands = generate(data, st, feed_config, feed_store, NOW, np.random.default_rng(0))
    got = {data.items[c.index].post_id: c for c in cands}
    assert raw[4].id not in got                                              # seen recently
    assert not any(data.items[c.index].source == "github" for c in cands)    # hidden source
    assert not any(r.id in got for r in raw[:3])                             # served problem
    farm = {data.by_id[p].niche_id for p in got} & {nodes["Agriculture > Farm equipment"]}
    assert not farm                                                          # removed industry
    explored = {data.items[c.index].niche_id for c in cands if c.source == EXPLORE}
    assert explored and not explored & {nodes[k] for k in nodes if k.startswith("Legal")}   # Legal is the map


def test_ranker_prefers_the_map_and_explains_its_score(world, feed_store, feed_config):
    reader, nodes, _, _, _ = world
    uid = onboard(feed_store, feed_config, ["Legal"], [], clock=lambda: NOW)
    data = load(feed_store, reader)
    st = load_state(feed_store, feed_config, uid, NOW)
    cands = generate(data, st, feed_config, feed_store, NOW, np.random.default_rng(0))
    scored = RankerV1(feed_config).score(data, st, cands, NOW)
    legal = [s.score for s in scored if data.items[s.index].niche_id in data.industry.subtree(nodes["Legal"])
             and data.items[s.index].post_type == "pain"]
    health = [s.score for s in scored if data.items[s.index].niche_id == nodes["Healthcare > Pharmacy"]]
    assert np.mean(legal) > np.mean(health)
    b = scored[0].breakdown
    assert {"features", "probabilities", "terms", "raw", "quality_factor", "ring", "source"} <= set(b)
    assert scored[0].score == pytest.approx(b["raw"] * b["quality_factor"], abs=1e-3)


def test_composition_collapses_paces_quotas_and_keeps_diversity(world, feed_store, feed_config):
    reader, nodes, _, raw, prob_id = world
    uid = onboard(feed_store, feed_config, ["Legal", "Healthcare"], [], clock=lambda: NOW)
    data = load(feed_store, reader)
    st = load_state(feed_store, feed_config, uid, NOW)
    scored = RankerV1(feed_config).score(data, st, generate(data, st, feed_config, feed_store, NOW,
                                                            np.random.default_rng(0)), NOW)
    assert sum(data.items[s.index].problem_id == prob_id for s in collapse(scored, data)) == 1
    floor = feed_config.model_copy(update={"composition": feed_config.composition.model_copy(
        update={"caught_up_score_floor": -99})})
    page, caught_up = compose(scored, data, st, floor, NOW)
    types = [data.items[p.scored.index].post_type for p in page]
    sources = [data.items[p.scored.index].source for p in page]
    assert len(page) == 20 and not caught_up
    assert types.count("paid_demand") >= 3 and types.count("change") <= 4
    assert all(a != b for a, b in zip(sources, sources[1:]))                 # no source twice in a row
    assert not any(a == b == c for a, b, c in zip(types, types[1:], types[2:]))   # at most 2 of a type in a row
    assert all(p.explanation for p in page)
    high = feed_config.model_copy(update={"composition": feed_config.composition.model_copy(
        update={"caught_up_score_floor": 99})})
    assert compose(scored, data, st, high, NOW) == ([], True)              # nothing strong enough


def test_pins_get_guaranteed_slots(world, feed_store, feed_config):
    from signal_feed.db import UserPinRow
    reader, nodes, _, _, _ = world
    uid = onboard(feed_store, feed_config, ["Legal"], [], clock=lambda: NOW)
    with feed_store.session() as s, s.begin():
        s.add(UserPinRow(user_id=uid, node_id=nodes["Agriculture > Farm equipment"], affinity=0.7, pinned_at=NOW))
    data = load(feed_store, reader)
    st = load_state(feed_store, feed_config, uid, NOW)
    scored = RankerV1(feed_config).score(data, st, generate(data, st, feed_config, feed_store, NOW,
                                                            np.random.default_rng(0)), NOW)
    page, _ = compose(scored, data, st, feed_config, NOW)
    pinned = [p for p in page if p.scored.candidate.source == PIN]
    assert pinned and pinned[0].explanation.startswith("Pinned: Farm equipment")


# --- service and API --------------------------------------------------------------

def test_first_visit_is_calibration_then_the_feed(world, feed_store, feed_config):
    reader, nodes, _, _, _ = world
    client = TestClient(create_app(feed_store, reader, feed_config,
                                   FeedService(feed_store, feed_config, FeedDataCache(feed_store, reader),
                                               clock=lambda: NOW, seed=1)))
    options = client.get("/onboarding/options").json()
    assert any(i["name"] == "Legal" and "Litigation support" in i["branches"] for i in options["industries"])
    assert client.post("/onboarding", json={"industries": ["Nope"]}).status_code == 422
    uid = client.post("/onboarding", json={"industries": ["Legal"], "skills": ["Document parsing"]}).json()["user_id"]
    cal = client.get("/feed", params={"user_id": uid, "session_id": "s1"}).json()
    assert cal["calibration"] and len(cal["items"]) == feed_config.user_model.calibration_cards
    paths = [i["niche_path"][0] for i in cal["items"]]
    assert paths.count("Legal") == 8 and len(set(paths) - {"Legal"}) >= 1      # 2 wildcards elsewhere
    types = [i["post_type"] for i in cal["items"] if i["niche_path"][0] == "Legal"]
    assert types[:2] != [types[0]] * 2                                       # demand and non-demand alternate
    card = cal["items"][0]
    ok = client.post("/events", json={"user_id": uid, "events": [{
        "client_event_id": "c1", "session_id": "s1", "type": "interested", "item_id": card["post_id"],
        "at": datetime.now(timezone.utc).isoformat(), "impression_id": card["impression_id"]}]})   # real clock
    assert ok.json()["accepted"] == 1
    with feed_store.session() as s:
        moved = s.get(UserNicheAffinityRow, (uid, nodes[" > ".join(card["niche_path"])]))
    assert moved is not None and moved.alpha > 0.2
    page = client.get("/feed", params={"user_id": uid, "session_id": "s2"}).json()
    assert not page["calibration"] and page["items"] and page["cursor"] == str(len(page["items"]))
    shown = {i["post_id"] for i in cal["items"]}
    assert not shown & {i["post_id"] for i in page["items"]}                 # no repeats within 30 days
    first = page["items"][0]
    assert "explanation" not in first and "reason" not in first          # the app never says why
    assert first["score_breakdown"]["reason"] and first["score_breakdown"]["ranker"] == "v1" and first["summary"]
    with feed_store.session() as s:
        assert s.query(ImpressionRow).filter_by(user_id=uid).count() == len(cal["items"]) + len(page["items"])
    assert client.get("/feed", params={"user_id": "ghost", "session_id": "x"}).status_code == 404


def test_drop_ins_spend_their_exposure_budget(world, feed_store, feed_config):
    reader, nodes, _, _, _ = world
    uid = onboard(feed_store, feed_config, ["Legal"], [], clock=lambda: NOW)
    node = nodes["Agriculture > Farm equipment"]
    with feed_store.session() as s, s.begin():
        s.add(UserDropInRow(user_id=uid, node_id=node, exposures_left=2, added_at=NOW))
        s.add(ImpressionRow(user_id=uid, session_id="old", item_type="post", item_id="x", position=0,
                            shown_at=NOW - timedelta(days=40), config_version=1))   # past calibration
    service = FeedService(feed_store, feed_config, FeedDataCache(feed_store, reader), clock=lambda: NOW, seed=1)
    page = service.page(uid, "s1")
    assert any(i.score_breakdown["reason"].startswith("You added Farm equipment") for i in page.items)
    with feed_store.session() as s:
        assert s.get(UserDropInRow, (uid, node)).exposures_left == 1


def test_committed_mode_narrows_the_feed_to_the_problem(world, feed_store, feed_config):
    reader, nodes, _, raw, prob_id = world
    client = TestClient(create_app(feed_store, reader, feed_config,
                                   FeedService(feed_store, feed_config, FeedDataCache(feed_store, reader),
                                               clock=lambda: NOW, seed=1)))
    uid = client.post("/onboarding", json={"industries": ["Healthcare"]}).json()["user_id"]
    client.get("/feed", params={"user_id": uid, "session_id": "cal"})                # calibration first
    assert client.post("/mode", json={"user_id": uid, "mode": "committed"}).status_code == 422
    assert client.post("/mode", json={"user_id": uid, "mode": "committed", "problem_id": 99999}).status_code == 422
    with feed_store.session() as s, s.begin():
        s.get(ProblemRow, prob_id).competitors = ["clio"]
    assert client.post("/mode", json={"user_id": uid, "mode": "committed", "problem_id": prob_id}).json()["mode"] == "committed"
    page = client.get("/feed", params={"user_id": uid, "session_id": "focus"}).json()
    legal = set(nodes[k] for k in nodes if k.startswith("Legal"))
    data = load(feed_store, reader)
    outside = [i for i in page["items"] if data.by_id[i["post_id"]].niche_id not in legal
               and not i["score_breakdown"]["source"].startswith("committed")]
    assert page["items"] and len(outside) <= int(feed_config.user_model.committed_other_cap * 20)
    assert any(i["score_breakdown"]["source"] == "committed_evidence" for i in page["items"])
    assert any(i["post_type"] == "paid_demand" for i in page["items"])          # related demand is in scope
    assert len(page["items"]) >= 15 and min(i["score"] for i in page["items"][:5]) > 0.5   # focus ranks high
    assert not any(i["score_breakdown"].get("bubble_guard") for i in page["items"])
    assert client.post("/mode", json={"user_id": uid, "mode": "exploring"}).json()["mode"] == "exploring"
    with feed_store.session() as s:
        assert s.get(UserRow, uid).committed_problem_id is None
