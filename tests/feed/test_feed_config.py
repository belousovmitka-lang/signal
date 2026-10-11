import pytest
import yaml
from pydantic import ValidationError

from signal_feed.config import EVENT_TYPES, FeedConfig
from signal_feed.settings import REPO_ROOT


def raw_config() -> dict:
    return yaml.safe_load((REPO_ROOT / "config" / "feed.yaml").read_text())


def test_shipped_config_loads_with_spec_values(feed_config):
    assert feed_config.events.weights["outreach"] == 12
    assert feed_config.events.weights["report"] == -20
    assert feed_config.user_model.propagation.parent == 0.40
    assert feed_config.problems.cluster_cosine_threshold == 0.82
    assert set(feed_config.events.weights) == set(EVENT_TYPES)


def test_every_event_type_needs_a_weight():
    data = raw_config()
    del data["events"]["weights"]["save"]
    with pytest.raises(ValidationError, match="missing: save"):
        FeedConfig.model_validate(data)


def test_unknown_keys_are_rejected():
    data = raw_config()
    data["ranking"]["typo_weight"] = 1
    with pytest.raises(ValidationError):
        FeedConfig.model_validate(data)


def test_problem_score_weights_sum_to_one():
    data = raw_config()
    data["problems"]["score_weights"]["trend"] = 0.5
    with pytest.raises(ValidationError, match="sum to 1"):
        FeedConfig.model_validate(data)


def test_rings_must_be_ordered():
    data = raw_config()
    data["user_model"]["rings"]["regular_min"] = 0.7
    with pytest.raises(ValidationError, match="regular_min"):
        FeedConfig.model_validate(data)
