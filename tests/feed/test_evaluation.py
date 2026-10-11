"""Phase 7: metrics, offline replay with promotion, and A/B experiments."""

import math
from datetime import timedelta

import numpy as np
import pytest
from sqlalchemy import select

from signal_feed.config import Experiment
from signal_feed.db import ExperimentAssignmentRow, ModelRow
from signal_feed.evaluation.experiments import ab_report, apply_overrides, arm_settings, assign, bucket
from signal_feed.evaluation.metrics import weekly_metrics
from signal_feed.evaluation.replay import compare, ndcg, promote_if_better
from signal_feed.feed.data import FeedDataCache
from signal_feed.feed.service import FeedService
from signal_feed.feed.usermodel import onboard
from signal_feed.learning.models import train_v2
from signal_feed.learning.sim import SimBuilder, simulate

from .test_feed import NOW, world  # noqa: F401  (pytest fixture)

EXPERIMENT = {"name": "explore-more", "arms": {"control": {"share": 0.5},
                                               "more": {"share": 0.5, "overrides": {"candidates.shares.exploration": 0.2}}}}


def with_experiment(config, *experiments):
    return config.model_copy(update={"evaluation": config.evaluation.model_copy(
        update={"experiments": [Experiment.model_validate(e) for e in experiments]})})


def builders():
    return [SimBuilder({"Legal": 0.95}, {"pain": 1.0, "tool_gap": 0.9, "paid_demand": 0.4}, ["Legal"]),
            SimBuilder({"Healthcare": 0.95}, {"pain": 1.0, "paid_demand": 0.7}, ["Healthcare"]),
            SimBuilder({"Legal": 0.9, "Agriculture": 0.6}, {"pain": 0.9, "change": 0.8}, ["Legal"]),
            SimBuilder({"Healthcare": 0.9, "Legal": 0.5}, {"pain": 0.9, "tool_gap": 0.8}, ["Healthcare"])]


def test_ndcg_matches_hand_computation():
    assert ndcg(np.array([3.0, 2.0, 1.0]), np.array([1.0, 0.0, 0.0]), 20) == 1.0
    worst = ndcg(np.array([3.0, 2.0, 1.0]), np.array([0.0, 0.0, 1.0]), 20)
    assert worst == pytest.approx(1 / math.log2(4))                      # the hit is ranked third
    assert ndcg(np.array([1.0, 2.0]), np.array([0.0, 0.0]), 20) == 0.0
    assert ndcg(np.array([3.0, 2.0, 1.0]), np.array([0.0, 0.0, 1.0]), 2) == 0.0      # cut off at k


def test_assignment_is_sticky_and_follows_shares(world, feed_store, feed_config):
    config = with_experiment(feed_config, EXPERIMENT)
    uids = [onboard(feed_store, config, ["Legal"], [], clock=lambda: NOW) for _ in range(60)]
    arms = [assign(feed_store, config, u)["explore-more"] for u in uids]
    assert 15 <= arms.count("more") <= 45                                # ~50/50
    # Changing the shares later never moves anyone already assigned.
    skewed = dict(EXPERIMENT, arms={"control": {"share": 0.9}, "more": {"share": 0.1, "overrides": {}}})
    assert [assign(feed_store, with_experiment(feed_config, skewed), u)["explore-more"] for u in uids] == arms
    assert bucket("x", "u1") == bucket("x", "u1") != bucket("y", "u1")
    with feed_store.session() as s:
        assert s.query(ExperimentAssignmentRow).count() == 60
    with pytest.raises(ValueError):
        Experiment.model_validate({"name": "bad", "arms": {"a": {"share": 0.5}, "b": {"share": 0.4}}})


def test_overrides_apply_and_typos_fail(feed_config):
    changed = apply_overrides(feed_config, {"candidates.shares.exploration": 0.2, "ranking.value_model_blend": 0.3})
    assert changed.candidates.shares.exploration == 0.2 and changed.ranking.value_model_blend == 0.3
    assert feed_config.candidates.shares.exploration != 0.2                 # the original is untouched
    with pytest.raises(ValueError):
        apply_overrides(feed_config, {"candidates.shares.exploraton": 0.2})
    with pytest.raises(ValueError):
        apply_overrides(feed_config, {"candidates.shares.exploration": 7})   # fails validation
    config = with_experiment(feed_config, EXPERIMENT)
    cfg, ranker = arm_settings(config, {"explore-more": "more"})
    assert cfg.candidates.shares.exploration == 0.2 and ranker == (None, None)


def test_feed_uses_the_arm_config_and_logs_it(world, feed_store, feed_config):
    reader, *_ = world
    config = with_experiment(feed_config, EXPERIMENT)
    svc = FeedService(feed_store, config, FeedDataCache(feed_store, reader), clock=lambda: NOW, seed=1)
    uid = onboard(feed_store, config, ["Legal"], [], clock=lambda: NOW)
    arm = assign(feed_store, config, uid)["explore-more"]
    page = svc.page(uid, "s1")
    assert page.items and all(i.score_breakdown["experiments"] == {"explore-more": arm} for i in page.items)


def test_metrics_and_ab_report_from_simulated_builders(world, feed_store, feed_config):
    reader, *_ = world
    config = with_experiment(feed_config, EXPERIMENT)
    stats = simulate(feed_store, reader, config, builders(), sessions=3, pages_per_session=1, start=NOW, seed=3)
    m = weekly_metrics(feed_store, reader, NOW + timedelta(days=3))
    from signal_feed.db import ImpressionRow
    with feed_store.session() as s:
        calibration_cards = s.query(ImpressionRow).filter(ImpressionRow.candidate_source == "calibration").count()
    assert m["window"]["users"] == 4 and m["window"]["items_shown"] == stats.shown - calibration_cards > 0
    assert m["product"]["saves_per_100_items"] == pytest.approx(100 * stats.saves / m["window"]["items_shown"], abs=0.01)
    assert 0 < m["feed_health"]["distinct_niches_per_20"] and 0 < m["feed_health"]["distinct_sources_per_20"]
    assert m["guardrails"]["scroll_depth_items"] > 0 and 0 <= m["feed_health"]["coverage_30d"] <= 1
    report = ab_report(feed_store, reader, config, "explore-more", NOW + timedelta(days=3))
    assert set(report["arms"]) == {"control", "more"}
    assert sum(a["users"] for a in report["arms"].values()) == 4
    assert report["arms"]["more"]["verdict"] == "don't ship"                 # 4 users can't be significant


def test_replay_promotes_v2_only_when_it_beats_v1(world, feed_store, feed_config, tmp_path):
    reader, *_ = world
    config = feed_config.model_copy(update={"ranking": feed_config.ranking.model_copy(
        update={"models_dir": str(tmp_path / "models")})})
    simulate(feed_store, reader, config, builders() * 3, sessions=3, pages_per_session=1, start=NOW, seed=4)
    assert compare(feed_store, config)["verdict"] == "no v2 candidate"
    row = train_v2(feed_store, config, min_impressions=50)
    report = promote_if_better(feed_store, config)
    assert report["candidate_id"] == row.id and report["held_out_users"] >= 1
    assert {"save", "idea"} <= set(report["v1"]) and report["v1"]["save"]["sessions"] >= 0
    assert report["verdict"] == "keep v1" and "promoted" not in report           # too few held-out sessions
    with feed_store.session() as s:
        assert s.get(ModelRow, row.id).status == "candidate"
    # With the bar lowered to what this data can show, the decision follows the numbers.
    lenient = config.model_copy(update={"evaluation": config.evaluation.model_copy(
        update={"min_sessions": 1, "min_gain": -1.0, "idea_tolerance": 1.0})})
    report = promote_if_better(feed_store, lenient, row.id)
    assert report["verdict"] == "promote" and report["promoted"] == row.id
    with feed_store.session() as s:
        assert s.get(ModelRow, row.id).status == "active" and "offline" in s.get(ModelRow, row.id).metrics
        assert s.scalars(select(ModelRow).where(ModelRow.kind == "v2", ModelRow.status == "active")).one().id == row.id
