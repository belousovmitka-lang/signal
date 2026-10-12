"""Phase 6: surveys, value model, v2 models, Thompson sampling over post types, the filter-bubble guard,
hourly rescoring and the simulator."""

import uuid
from datetime import timedelta

import numpy as np
import pytest
from sqlalchemy import select

from signal_feed.db import EventRow, ImpressionRow, ModelRow, ProblemRow, SurveyRow
from signal_feed.feed.candidates import generate
from signal_feed.feed.data import FeedDataCache, load
from signal_feed.feed.ranker import RankerV1, RankerV2
from signal_feed.feed.service import FeedService
from signal_feed.feed.usermodel import load_state, onboard
from signal_feed.learning.datasets import FEATURES, impressions_dataset, survey_dataset
from signal_feed.learning.models import NotEnoughData, V2Model, ValueModel, train_v2, train_value_model
from signal_feed.learning.nightly import run_nightly
from signal_feed.learning.sim import SimBuilder, simulate
from signal_feed.problems.cluster import ProblemBuilder

from .test_feed import NOW, world  # noqa: F401  (pytest fixture)


def service(feed_store, reader, feed_config, seed=1, days=0):
    return FeedService(feed_store, feed_config, FeedDataCache(feed_store, reader),
                       clock=lambda: NOW + timedelta(days=days), seed=seed)


def past_calibration(feed_store, uid):
    with feed_store.session() as s, s.begin():
        s.add(ImpressionRow(user_id=uid, session_id="old", item_type="post", item_id="x", position=0,
                            shown_at=NOW - timedelta(days=40), config_version=1))


def answer(feed_store, uid, items, etype):
    """Store events (and survey rows) the way the API would."""
    rows = [EventRow(user_id=uid, client_event_id=uuid.uuid4().hex, session_id="s", item_type="post",
                     item_id=i.post_id, event_type=etype, weight=3.0 if etype == "useful_yes" else -3.0 if etype ==
                     "useful_no" else 4.0, impression_id=i.impression_id, at=NOW, received_at=NOW, config_version=1)
            for i in items]
    feed_store.add_events(rows)


def test_every_nth_item_asks_was_this_useful(world, feed_store, feed_config):
    reader, *_ = world
    # The synthetic world has ~50 showable posts, so ask every 7th item instead of every 30th.
    config = feed_config.model_copy(update={"ranking": feed_config.ranking.model_copy(update={"survey_every_n_items": 7})})
    uid = onboard(feed_store, config, ["Legal", "Healthcare"], [], clock=lambda: NOW)
    svc = service(feed_store, reader, config)
    flagged, shown = [], 0
    for k in range(3):
        page = svc.page(uid, f"s{k}")
        assert not (page.calibration and any(i.ask_useful for i in page.items))   # never on calibration cards
        flagged += [shown + j + 1 for j, i in enumerate(page.items) if i.ask_useful]
        shown += len(page.items)
    assert len(flagged) >= 3 and all(n % 7 == 0 for n in flagged)              # counted over the user's history


def test_value_model_trains_on_answers_and_blends_into_the_score(world, feed_store, feed_config):
    reader, *_ = world
    uid = onboard(feed_store, feed_config, ["Legal"], [], clock=lambda: NOW)
    past_calibration(feed_store, uid)
    page = service(feed_store, reader, feed_config).page(uid, "s1")
    with pytest.raises(NotEnoughData):
        train_value_model(feed_store, feed_config)
    # Useful = Legal; not useful = everything else.
    legal = [i for i in page.items if i.niche_path[0] == "Legal"]
    other = [i for i in page.items if i.niche_path[0] != "Legal"]
    assert legal and other
    answer(feed_store, uid, legal, "useful_yes")
    answer(feed_store, uid, other, "useful_no")
    x, y = survey_dataset(feed_store)
    assert x.shape == (len(page.items), len(FEATURES)) and y.sum() == len(legal)
    row = train_value_model(feed_store, feed_config, min_answers=5)
    assert row.status == "active" and row.metrics["answers"] == len(page.items)
    vm = ValueModel.load_active(feed_store)
    data, st = load(feed_store, reader), load_state(feed_store, feed_config, uid, NOW)
    cands = generate(data, st, feed_config, feed_store, NOW, np.random.default_rng(0))
    plain = {s.index: s for s in RankerV1(feed_config).score(data, st, cands, NOW)}
    blended = RankerV1(feed_config, vm).score(data, st, cands, NOW)
    b = blended[0].breakdown["value"]
    expected = 0.8 * plain[blended[0].index].score + 0.2 * b["points"]
    assert blended[0].score == pytest.approx(expected, abs=1e-3) and 0 <= b["p_useful"] <= 1


def test_v2_trains_per_action_and_is_used_only_once_active(world, feed_store, feed_config, tmp_path):
    reader, *_ = world
    config = feed_config.model_copy(update={"ranking": feed_config.ranking.model_copy(
        update={"models_dir": str(tmp_path / "models")})})
    uids = [onboard(feed_store, config, ["Legal"], [], clock=lambda: NOW) for _ in range(4)]
    for uid in uids:
        past_calibration(feed_store, uid)
        page = service(feed_store, reader, config).page(uid, "s1")
        answer(feed_store, uid, [i for i in page.items if i.niche_path[0] == "Legal"], "save")
    data = impressions_dataset(feed_store)
    assert data.x.shape[1] == len(FEATURES) and data.y["save"].sum() > 0
    with pytest.raises(NotEnoughData):
        train_v2(feed_store, config)                       # 50k impressions needed by default
    row = train_v2(feed_store, config, min_impressions=10)
    assert row.status == "candidate" and (tmp_path / "models").exists()
    svc = service(feed_store, reader, config)
    assert type(svc.current_ranker()) is RankerV1           # a candidate is never used
    with feed_store.session() as s, s.begin():
        s.get(ModelRow, row.id).status = "active"
    ranker = svc.current_ranker()
    assert isinstance(ranker, RankerV2)
    probs = ranker.probabilities([{f: 0.5 for f in FEATURES}])[0]
    assert set(probs) >= {"read", "save", "not_for_me"} and all(0 <= p <= 1 for p in probs.values())
    assert V2Model.load(feed_store, model_id=row.id) is not None


def test_thompson_draws_follow_type_evidence(world, feed_store, feed_config):
    reader, *_ = world
    uid = onboard(feed_store, feed_config, ["Legal"], [], clock=lambda: NOW)
    st = load_state(feed_store, feed_config, uid, NOW)
    st.type_evidence = {"pain": (40.0, 0.0), "paid_demand": (0.0, 40.0)}
    ranker = RankerV1(feed_config)
    rng = np.random.default_rng(0)
    draws = [ranker.type_draws(st, rng) for _ in range(200)]
    half = feed_config.ranking.type_thompson_prior / 2
    expected = 2 * (half + 40) / (2 * half + 40) - 1                          # mean of 2 * Beta - 1
    assert np.mean([d["pain"] for d in draws]) == pytest.approx(expected, abs=0.05)
    assert np.mean([d["paid_demand"] for d in draws]) == pytest.approx(-expected, abs=0.05)
    assert abs(np.mean([d["change"] for d in draws])) < 0.1                 # no evidence: neutral on average
    off = feed_config.model_copy(update={"ranking": feed_config.ranking.model_copy(update={"thompson_post_types": False})})
    assert RankerV1(off).type_draws(st, rng) is None


def test_filter_bubble_guard_raises_exploration(world, feed_store, feed_config):
    reader, nodes, _, raw, _ = world
    uid = onboard(feed_store, feed_config, ["Legal"], [], clock=lambda: NOW)
    lit = [r.id for r in raw[:12]]                                      # Legal > Litigation support pain
    with feed_store.session() as s, s.begin():
        for k in range(100):
            s.add(ImpressionRow(user_id=uid, session_id="old", item_type="post", item_id=lit[k % len(lit)],
                                position=k, shown_at=NOW - timedelta(days=31), config_version=1))
    page = service(feed_store, reader, feed_config).page(uid, "s1")
    assert all(i.score_breakdown.get("bubble_guard") for i in page.items)
    explore = sum(i.score_breakdown["source"] == "exploration" for i in page.items)
    assert explore >= int(feed_config.learning.bubble_exploration * len(page.items))


def test_rescore_refreshes_problem_scores(world, feed_store, feed_config):
    reader, _, _, _, prob_id = world
    with feed_store.session() as s, s.begin():
        s.get(ProblemRow, prob_id).score = 0.0
    assert ProblemBuilder(feed_store, reader, feed_config, clock=lambda: NOW).rescore() == 1
    with feed_store.session() as s:
        p = s.get(ProblemRow, prob_id)
    assert p.score > 0 and set(p.score_breakdown) >= {"frequency", "trend", "intensity"}


def test_simulated_builders_produce_trainable_data(world, feed_store, feed_config, tmp_path):
    reader, *_ = world
    config = feed_config.model_copy(update={"ranking": feed_config.ranking.model_copy(
        update={"models_dir": str(tmp_path / "models"), "survey_every_n_items": 5})})
    builders = [SimBuilder({"Legal": 0.9}, {"pain": 1.0, "tool_gap": 0.8}, ["Legal"]),
                SimBuilder({"Healthcare": 0.9}, {"pain": 1.0}, ["Healthcare"])]
    stats = simulate(feed_store, reader, config, builders, sessions=3, pages_per_session=1, start=NOW, seed=2)
    assert stats.pages >= 4 and stats.shown and stats.reads and stats.surveys
    with feed_store.session() as s:
        assert s.query(SurveyRow).count() == stats.surveys
        assert s.query(EventRow).filter(EventRow.event_type == "read").count() == stats.reads
    report = run_nightly(feed_store, config, min_surveys=3, min_impressions=10)
    assert "model_id" in report.v2 and ("model_id" in report.value or "skipped" in report.value)
    with feed_store.session() as s:
        assert s.scalars(select(ModelRow).where(ModelRow.kind == "v2")).first().status == "candidate"
