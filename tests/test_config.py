from pathlib import Path

import pytest
from pydantic import ValidationError

from signal_connectors.config import SourcesFile, load_sources

REPO = Path(__file__).resolve().parent.parent


def parse(sources):
    return SourcesFile.model_validate({"sources": sources})


def test_repo_sources_yaml_is_valid():
    load_sources(REPO / "sources.yaml")


def test_names_and_connector_default_to_key():
    s = parse({"hackernews": {"route": "own_api", "source_type": "community"}}).get("hackernews")
    assert (s.name, s.connector, s.enabled, s.monthly_budget_usd) == ("hackernews", "hackernews", False, 0)


def test_public_pages_needs_access_check_to_enable():
    with pytest.raises(ValidationError, match="rule 2"):
        parse({"f": {"route": "public_pages", "source_type": "community", "enabled": True}})
    ok = parse({"f": {"route": "public_pages", "source_type": "community", "enabled": True,
                      "access_check": {"robots": "allowed", "terms": "allowed", "decision": "full",
                                       "checked_at": "2026-10-09"}}})
    assert ok.get("f").blocked_reason() is None


def test_access_check_consistency():
    with pytest.raises(ValidationError, match="search_only"):
        parse({"f": {"route": "public_pages", "source_type": "community",
                     "access_check": {"robots": "disallowed", "decision": "full", "checked_at": "2026-10-09"}}})
    with pytest.raises(ValidationError, match="checked_at"):
        parse({"f": {"route": "public_pages", "source_type": "community", "access_check": {"decision": "full"}}})


def test_paid_sources_need_a_flag_and_stay_off_until_set(monkeypatch):
    with pytest.raises(ValidationError, match="feature_flag"):
        parse({"yelp": {"route": "own_api", "source_type": "review", "paid": True}})
    s = parse({"yelp": {"route": "own_api", "source_type": "review", "paid": True, "enabled": True,
                        "feature_flag": "ENABLE_YELP", "monthly_budget_usd": 229}}).get("yelp")
    monkeypatch.delenv("ENABLE_YELP", raising=False)
    assert "ENABLE_YELP" in s.blocked_reason()
    monkeypatch.setenv("ENABLE_YELP", "1")
    assert s.blocked_reason() is None


def test_blocked_cannot_be_enabled():
    with pytest.raises(ValidationError, match="blocked"):
        parse({"discord": {"route": "own_api", "source_type": "community", "status": "blocked", "enabled": True}})


def test_unknown_keys_are_rejected():
    with pytest.raises(ValidationError):
        parse({"x": {"route": "own_api", "source_type": "social", "budget": 5}})


def test_build_queries_uses_defaults():
    f = SourcesFile.model_validate({"defaults": {"limit": 25}, "sources": {"x": {
        "route": "own_api", "source_type": "social",
        "queries": [{"keywords": ["a"]}, {"keywords": ["b"], "limit": 5}]}}})
    qs = f.get("x").build_queries(f.defaults)
    assert [(q.keywords, q.limit) for q in qs] == [(["a"], 25), (["b"], 5)]


def test_industry_fed_source_without_entries_has_no_queries():
    from signal_connectors.config import Defaults, SourcesFile
    s = SourcesFile.model_validate({"sources": {
        "podcasts": {"route": "rss", "source_type": "media", "options": {"industry_podcasts": True}},
        "hn": {"route": "own_api", "source_type": "community"}}})
    assert s.get("podcasts").build_queries(Defaults()) == []
    assert len(s.get("hn").build_queries(Defaults())) == 1
