import asyncio
import json
import httpx
import pytest

from signal_connectors.connectors import x as xmod
from signal_connectors.connectors.x import XConnector
from signal_connectors.runner import recheck_deletions
from signal_connectors.scheduler import plan
from signal_connectors.x_search import (DRY_RUN_RESPONSE, XBudgetReached, XSearchDisabled, XSearchError,
                                        asearch_x, search_x)


def src(enabled=True, budget=20.0):
    return {"sources": {"x": {"enabled": enabled, "status": "done", "route": "own_api", "source_type": "social",
                              "monthly_budget_usd": budget, "deletion_recheck_days": 1,
                              "deletion_recheck_limit": 1000000,
                              "options": {"on_demand": True, "max_results": 10, "recheck_window_days": 3650}}}}


@pytest.fixture(autouse=True)
def token(monkeypatch):
    monkeypatch.setenv("X_BEARER_TOKEN", "test-token")


def search_api(calls):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["authorization"] == "Bearer test-token"
        return httpx.Response(200, json=DRY_RUN_RESPONSE)
    return handler


async def test_stores_and_returns_posts_without_user_lookups(make_services):
    calls = []
    async with make_services(src(), search_api(calls)) as svc:
        posts = await asearch_x('"load confirmations" "by hand"', industry_hint="logistics", tag="inferred:abc",
                                services=svc)
        assert svc.store.count_posts("x") == 2
        again = await asearch_x('"load confirmations" "by hand"', services=svc)     # already stored: returned anyway
        assert svc.store.count_posts("x") == 2 and [p.id for p in again] == [p.id for p in posts]
        assert svc.costs.spent_this_month("x") == pytest.approx(0.02)                # 2 posts x $0.005, twice
        assert svc.store.last_run_status("x") == "ok"
    params = dict(calls[0].url.params)
    assert params["query"] == '"load confirmations" "by hand" lang:en -is:retweet -is:reply -is:quote -from:grok'
    assert params["max_results"] == "10" and "expansions" not in params and "user.fields" not in params
    first = posts[0]
    assert (first.source, first.source_type, first.route) == ("x", "social", "own_api")
    assert first.url == "https://x.com/i/web/status/1900000000000000001" and first.industry_hint == "logistics"
    assert first.query == '"load confirmations" "by hand"' and first.raw["tag"] == "inferred:abc"
    assert first.raw["embed"] == "https://twitter.com/x/status/1900000000000000001"
    assert first.author_hash and "100001" not in json.dumps(first.model_dump(mode="json"))
    assert first.author_role == "dispatcher" and posts[1].author_role is None
    assert first.engagement == {"likes": 4, "replies": 2, "reposts": 0, "quotes": 0}


async def test_refuses_before_any_paid_call(make_services):
    calls = []
    async with make_services(src(enabled=False), search_api(calls)) as svc:
        with pytest.raises(XSearchDisabled):
            await asearch_x("anything", services=svc)
    async with make_services(src(budget=0.04), search_api(calls)) as svc:      # a 10-post call is estimated $0.05
        with pytest.raises(XBudgetReached):
            await asearch_x("anything", services=svc)
        assert svc.store.last_run_status("x") == "failed"
    assert calls == []


async def test_x_errors_raise(make_services):
    def handler(request):
        return httpx.Response(402, json={"title": "CreditsDepleted", "detail": "Your account has no credits"})
    async with make_services(src(), handler) as svc:
        with pytest.raises(XSearchError, match="402.*no credits"):
            await asearch_x("anything", services=svc)
        assert svc.store.count_posts() == 0 and svc.costs.spent_this_month("x") == 0


def test_dry_run_needs_no_network_and_writes_nothing(monkeypatch, settings):
    monkeypatch.delenv("X_BEARER_TOKEN")
    posts = search_x("anything", dry_run=True, tag="t")     # real sources.yaml, where x is still off
    assert len(posts) == 2 and all(p.source == "x" for p in posts)


async def test_sync_wrapper_works_inside_a_running_loop():
    posts = search_x("anything", dry_run=True)
    assert len(posts) == 2
    await asyncio.sleep(0)


async def test_compliance_purges_deleted_and_edited_posts(make_services, monkeypatch):
    monkeypatch.setattr(xmod, "POLL_SECONDS", 0)
    seen = {"uploaded": None, "downloads": []}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/2/tweets/search"):
            return httpx.Response(200, json=DRY_RUN_RESPONSE)
        assert request.headers["authorization"] == "Bearer test-token"      # upload/download need it too
        if request.method == "GET" and path == "/2/compliance/jobs":
            return httpx.Response(200, json={"data": [
                {"id": "7", "name": "signal-x-recheck-2", "status": "complete",
                 "download_url": "https://api.x.com/2/compliance/jobs/7/download?token=t7"},
                {"id": "5", "name": "someone-else", "status": "complete", "download_url": "https://api.x.com/x"}]})
        if request.method == "POST":
            return httpx.Response(200, json={"data": {
                "id": "8", "upload_url": "https://api.x.com/2/compliance/jobs/8/upload?token=u",
                "download_url": "https://api.x.com/2/compliance/jobs/8/download?token=d"}})
        if request.method == "PUT":
            seen["uploaded"] = request.content.decode()
            return httpx.Response(200)
        if path == "/2/compliance/jobs/8":
            return httpx.Response(200, json={"data": {"id": "8", "status": "complete",
                                                      "download_url": "https://api.x.com/2/compliance/jobs/8/download?token=d"}})
        if path.endswith("/download"):
            seen["downloads"].append(path)
            body = ({"id": "1900000000000000001", "action": "delete", "reason": "deleted"} if "/7/" in path
                    else {"id": "1900000000000000002", "action": "rehydrate", "reason": "tweet_edited"})
            return httpx.Response(200, text=json.dumps(body) + "\n" + json.dumps({"id": "x", "error": "invalid_id"}))
        raise AssertionError(f"unexpected {request.method} {path}")

    async with make_services(src(), handler) as svc:
        await asearch_x("anything", services=svc)
        assert await recheck_deletions(svc, svc.sources.get("x")) == 2
        assert svc.store.count_posts("x") == 0
        assert sorted(seen["uploaded"].split("\n")) == ["1900000000000000001", "1900000000000000002"]
        # Job 7 is now applied: a later re-check doesn't download it again.
        await asearch_x("anything", services=svc)
        seen["downloads"].clear()
        await XConnector(svc.sources.get("x"), svc).deleted(["https://x.com/i/web/status/1900000000000000001"])
        assert all("/7/" not in d for d in seen["downloads"])


async def test_slow_compliance_job_is_applied_by_a_15_minute_retry_without_a_new_job(make_services, monkeypatch):
    from datetime import timedelta

    from signal_connectors.models import utcnow
    from signal_connectors.scheduler import _recheck
    monkeypatch.setattr(xmod, "POLL_SECONDS", 0)
    state = {"job": None, "posts": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/2/tweets/search"):
            return httpx.Response(200, json=DRY_RUN_RESPONSE)
        if request.method == "GET" and path == "/2/compliance/jobs":
            return httpx.Response(200, json={"data": [state["job"]] if state["job"] else []})
        if request.method == "POST":
            state["posts"] += 1
            state["job"] = {"id": "9", "name": "signal-x-recheck-2", "status": "in_progress",
                            "created_at": utcnow().isoformat().replace("+00:00", "Z"),
                            "upload_url": "https://api.x.com/2/compliance/jobs/9/upload?token=u",
                            "download_url": "https://api.x.com/2/compliance/jobs/9/download?token=d"}
            return httpx.Response(200, json={"data": state["job"]})
        if request.method == "PUT":
            return httpx.Response(200)
        if path == "/2/compliance/jobs/9":
            return httpx.Response(200, json={"data": state["job"]})
        if path.endswith("/download"):
            return httpx.Response(200, text=json.dumps({"id": "1900000000000000001", "action": "delete"}))
        raise AssertionError(f"unexpected {request.method} {path}")

    async with make_services(src(), handler) as svc:
        await asearch_x("anything", services=svc)
        first = await _recheck(svc, svc.sources.get("x"))              # job still running at X
        assert first.status == "partial" and "pending" in first.errors[0] and svc.store.count_posts("x") == 2
        now = utcnow()
        assert plan(svc, now + timedelta(minutes=9)).rechecks == []
        assert [s.name for s in plan(svc, now + timedelta(minutes=15)).rechecks] == ["x"]
        state["job"]["status"] = "complete"
        second = await _recheck(svc, svc.sources.get("x"))
        assert second.status == "ok" and second.duplicates == 1 and svc.store.count_posts("x") == 1
        assert state["posts"] == 1                                        # the finished job counts for today
        assert plan(svc, now + timedelta(minutes=30)).rechecks == []      # back to once a day


async def test_on_demand_source_is_never_scheduled_but_is_rechecked_even_when_off(make_services):
    async with make_services(src(enabled=False)) as svc:
        p = plan(svc)
        assert [s.name for s in p.runs] == [] and [s.name for s in p.rechecks] == ["x"]
    async with make_services(src()) as svc:
        p = plan(svc)
        assert p.runs == [] and [s.name for s in p.rechecks] == ["x"]
        assert svc.sources.get("x").build_queries(svc.sources.defaults) == []


def test_sources_yaml_entry():
    from signal_connectors.config import load_sources
    from signal_connectors.settings import Settings
    x = load_sources(Settings.from_env().sources_file).get("x")
    assert x.monthly_budget_usd == 20 and x.deletion_recheck_days == 1 and x.options["on_demand"] is True
    assert x.connector == "x"
