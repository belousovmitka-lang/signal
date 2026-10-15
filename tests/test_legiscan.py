import os
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from dotenv import dotenv_values

from signal_connectors.config import SourcesFile
from signal_connectors.runner import run_source
from signal_connectors.services import Services

from .conftest import signal_vcr

SINCE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def src(limit=10, baseline=2, min_relevance=60):
    return {"sources": {"legiscan": {
        "route": "own_api", "source_type": "government", "options": {"baseline": baseline, "min_relevance": min_relevance},
        "queries": [{"industry_hint": "childcare", "keywords": ['"child care"'], "limit": limit}]}}}


def bill(i, status=1):
    return {"bill_id": i, "state": "FL", "bill_number": f"S{i}", "title": f"Child Care Bill {i}",
            "description": f"Requires child care facilities to file report {i}.", "url": f"https://legiscan.com/FL/bill/S{i}/2026",
            "state_link": f"https://flsenate.gov/{i}", "status": status, "status_date": "2026-06-12",
            "change_hash": f"h{i}", "history": [{"date": "2026-02-01", "action": "Filed"}, {"date": "2026-06-12", "action": "Chapter No. 140"}],
            "subjects": [{"subject_name": "Child Care"}], "sponsors": [{"name": "Sen. Someone"}], "session": {"session_name": "2026 Regular"}}


class Api:
    def __init__(self, results):
        self.results, self.calls = results, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        op = request.url.params["op"]
        self.calls.append((op, request.url.params.get("id")))
        assert request.url.params["key"] == "test-key"
        if op == "getSearchRaw":
            assert request.url.params["state"] == "ALL" and request.url.params["year"] == "2"
            return httpx.Response(200, json={"status": "OK", "searchresult": {"summary": {}, "results": self.results}})
        if op == "getBill":
            return httpx.Response(200, json={"status": "OK", "bill": bill(int(request.url.params["id"]), status=4)})
        return httpx.Response(200, json={"status": "ERROR", "alert": {"message": "bad op"}})


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv("LEGISCAN_API_KEY", "test-key")


async def test_first_run_reads_a_baseline_then_only_new_bills(make_services):
    results = [{"bill_id": i, "relevance": r, "change_hash": f"h{i}"} for i, r in [(1, 100), (2, 95), (3, 90), (4, 30)]]
    api = Api(results)
    async with make_services(src(), api) as svc:
        first = await run_source(svc, svc.sources.get("legiscan"), since=SINCE, collect=True)
        assert [p.raw["bill_id"] for p in first.posts] == [1, 2]          # baseline: the 2 most relevant
        api.results = results + [{"bill_id": 5, "relevance": 80, "change_hash": "h5"},
                                 {"bill_id": 6, "relevance": 20, "change_hash": "h6"}]
        api.calls.clear()
        second = await run_source(svc, svc.sources.get("legiscan"), since=SINCE, collect=True)
    assert [p.raw["bill_id"] for p in second.posts] == [5]              # 3 was marked seen; 6 isn't relevant
    assert api.calls == [("getSearchRaw", None), ("getBill", "5")]
    p = first.posts[0]
    assert p.title == "FL S1: Child Care Bill 1" and p.community == "FL" and p.source_type == "government"
    assert p.text == "Requires child care facilities to file report 1. Status: Passed. Latest action (2026-06-12): Chapter No. 140."
    assert p.published_at == datetime(2026, 6, 12, tzinfo=timezone.utc) and p.url == "https://legiscan.com/FL/bill/S1/2026"
    assert p.raw["subjects"] == ["Child Care"] and p.raw["introduced"] == "2026-02-01" and "CC BY 4.0" in p.raw["license"]
    assert "Someone" not in str(p.raw)                                       # sponsors not stored


async def test_api_error_fails_the_query(make_services):
    def broken(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, json={"status": "ERROR", "alert": {"message": "Invalid API key"}})

    async with make_services(src(), broken) as svc:
        out = await run_source(svc, svc.sources.get("legiscan"), since=SINCE)
    assert out.result.status == "failed" and "Invalid API key" in out.result.errors[0]


async def test_quota_counts_every_call(make_services):
    results = [{"bill_id": i, "relevance": 100, "change_hash": f"h{i}"} for i in range(1, 4)]
    async with make_services(src(baseline=3), Api(results)) as svc:
        await run_source(svc, svc.sources.get("legiscan"), since=SINCE)
        assert svc.costs.units_used("legiscan", timedelta(days=1)) == 4     # 1 search + 3 bills


@signal_vcr.use_cassette("legiscan.yaml")
async def test_cassette_legiscan(settings, monkeypatch):
    if os.environ.get("VCR_RECORD_MODE", "none") != "none":       # recording needs the real key; it is filtered out
        monkeypatch.setenv("LEGISCAN_API_KEY", dotenv_values(".env")["LEGISCAN_API_KEY"])
    sources = SourcesFile.model_validate(src(baseline=3))
    async with Services.open(settings, sources) as svc:
        out = await run_source(svc, svc.sources.get("legiscan"), since=SINCE, collect=True)
    assert out.result.status == "ok" and out.result.invalid == 0 and out.result.new == 3
    assert all(p.url.startswith("https://legiscan.com/") and p.title and p.raw["status"] for p in out.posts)
