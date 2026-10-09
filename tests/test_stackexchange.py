import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx

from signal_connectors.config import SourcesFile
from signal_connectors.runner import run_source
from signal_connectors.services import Services

from .conftest import signal_vcr

SINCE = datetime(2026, 9, 25, tzinfo=timezone.utc)


def se_source(*queries):
    return {"sources": {"stackexchange": {
        "route": "own_api", "source_type": "community", "queries": list(queries),
        "rate_limit": {"requests_per_second": 100, "domains": ["api.stackexchange.com"]},
    }}}


@signal_vcr.use_cassette("stackexchange.yaml")
async def test_cassette_new_questions_and_keyword_search(settings, monkeypatch):
    monkeypatch.delenv("STACKEXCHANGE_KEY", raising=False)
    sources = SourcesFile.model_validate(se_source(
        {"communities": ["softwarerecs", "law"], "limit": 15},
        {"communities": ["stackoverflow"], "keywords": ["is there a tool"], "limit": 10},
    ))
    async with Services.open(settings, sources) as svc:
        out = await run_source(svc, svc.sources.get("stackexchange"), since=SINCE, collect=True)
        assert svc.costs.units_used("stackexchange", timedelta(days=1)) >= 3

    r = out.result
    assert r.status == "ok" and r.invalid == 0 and r.fetched >= 20
    for p in out.posts:
        assert p.title and p.text and "<p>" not in p.text and "&quot;" not in p.title
        assert p.published_at >= SINCE and p.raw["license"] == "CC BY-SA 4.0"
        assert "/questions/" in p.url and p.community.endswith((".stackexchange.com", "stackoverflow.com"))
        assert set(p.engagement) <= {"score", "answers", "views"}
    assert {p.raw["site"] for p in out.posts} == {"softwarerecs", "law", "stackoverflow"}
    assert all(p.query == "is there a tool" for p in out.posts if p.raw["site"] == "stackoverflow")


def item(qid, site="law", **kw):
    d = {"question_id": qid, "link": f"https://{site}.stackexchange.com/questions/{qid}/x",
         "title": "Can I &quot;automate&quot; this?", "body": "<p>I&#39;m a paralegal</p>",
         "creation_date": 1791000000 + qid, "score": 1, "answer_count": 0, "view_count": 9,
         "tags": ["software"], "owner": {"account_id": 42, "display_name": "Real Name"}}
    d.update(kw)
    return d


def api(responses):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        q = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        calls.append(q)
        status, body = responses[min(len(calls), len(responses)) - 1]
        return httpx.Response(status, json=body)

    return handler, calls


async def test_mapping_paging_key_and_no_author_name(make_services, monkeypatch):
    monkeypatch.setenv("STACKEXCHANGE_KEY", "k-123")
    handler, calls = api([
        (200, {"items": [item(1)], "has_more": True, "quota_remaining": 9999}),
        (200, {"items": [item(2)], "has_more": False, "quota_remaining": 9998}),
    ])
    async with make_services(se_source({"communities": ["law"]}), handler) as svc:
        out = await run_source(svc, svc.sources.get("stackexchange"), since=SINCE, collect=True)
    p = out.posts[0]
    assert p.title == 'Can I "automate" this?' and p.text == "I'm a paralegal"
    assert p.community == "law.stackexchange.com" and p.engagement == {"score": 1, "answers": 0, "views": 9}
    assert "Real Name" not in p.model_dump_json() and len(p.author_hash) == 64
    assert [c["page"] for c in calls] == ["1", "2"]
    assert calls[0]["key"] == "k-123" and calls[0]["fromdate"] == str(int(SINCE.timestamp()))
    assert calls[0]["filter"] == "withbody" and "q" not in calls[0]


async def test_backoff_field_defers_the_domain(make_services):
    handler, _ = api([(200, {"items": [item(1)], "has_more": False, "backoff": 7})])
    async with make_services(se_source({"communities": ["law"]}), handler) as svc:
        await run_source(svc, svc.sources.get("stackexchange"), since=SINCE)
        assert svc.http.limiter._next["api.stackexchange.com"] > time.monotonic() + 5


async def test_api_error_fails_that_query_with_its_message(make_services):
    handler, _ = api([(400, {"error_id": 502, "error_name": "throttle_violation",
                             "error_message": "too many requests from this IP"})])
    async with make_services(se_source({"communities": ["law"]}), handler) as svc:
        r = (await run_source(svc, svc.sources.get("stackexchange"), since=SINCE)).result
    assert r.status == "failed" and "throttle_violation" in r.errors[0]


async def test_daily_quota_stops_requests(make_services, monkeypatch):
    monkeypatch.delenv("STACKEXCHANGE_KEY", raising=False)
    handler, calls = api([(200, {"items": [], "has_more": False})])
    async with make_services(se_source({"communities": ["law", "money"]}), handler) as svc:
        svc.costs.record("stackexchange", units=299)
        r = (await run_source(svc, svc.sources.get("stackexchange"), since=SINCE)).result
    assert len(calls) == 1 and "units used" in r.errors[0]


async def test_query_needs_sites(make_services):
    handler, calls = api([(200, {"items": []})])
    async with make_services(se_source({"keywords": ["tool"]}), handler) as svc:
        r = (await run_source(svc, svc.sources.get("stackexchange"), since=SINCE)).result
    assert r.status == "failed" and "needs communities" in r.errors[0] and not calls
