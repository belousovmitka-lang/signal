import json
import os
from datetime import datetime, timezone

import httpx
import pytest

from signal_connectors.config import SourcesFile
from signal_connectors.runner import run_source
from signal_connectors.services import Services

from .conftest import signal_vcr

SINCE = datetime(2026, 10, 8, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def token(monkeypatch):
    monkeypatch.setenv("PRODUCTHUNT_TOKEN", "ph-test")


def src(limit=10, comments=1):
    return {"sources": {"producthunt": {"route": "own_api", "source_type": "startup",
                                        "queries": [{"limit": limit, "params": {"comments_per_post": comments}}]}}}


def node(i):
    return {"id": str(i), "name": f"Tool{i}", "tagline": "Invoices from email", "description": "Parses PDFs.",
            "url": f"https://www.producthunt.com/products/tool{i}?utm_source=api", "votesCount": 9, "commentsCount": 1,
            "createdAt": "2026-10-09T07:00:00Z", "topics": {"edges": [{"node": {"name": "Fintech"}}]},
            "comments": {"edges": [{"node": {"id": f"c{i}", "body": "<p>Does it handle QuickBooks?</p>",
                                             "createdAt": "2026-10-09T08:00:00Z", "votesCount": 2,
                                             "url": f"https://www.producthunt.com/products/tool{i}#comment-c{i}",
                                             "user": {"id": "u1"}}}]}}


async def test_launches_comments_and_cursor(make_services):
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        assert request.headers["authorization"] == "Bearer ph-test"
        if body["variables"]["cursor"] is None:
            return httpx.Response(200, json={"data": {"posts": {"edges": [{"node": node(1)}],
                                                                "pageInfo": {"hasNextPage": True, "endCursor": "Mw"}}}})
        return httpx.Response(200, json={"data": {"posts": {"edges": [{"node": node(2)}],
                                                            "pageInfo": {"hasNextPage": False}}}})

    async with make_services(src(), handler) as svc:
        out = await run_source(svc, svc.sources.get("producthunt"), since=SINCE, collect=True)
    assert [p.source for p in out.posts] == ["producthunt", "producthunt:comments"] * 2
    launch, comment = out.posts[:2]
    assert launch.title == "Tool1: Invoices from email" and launch.url == "https://www.producthunt.com/products/tool1"
    assert launch.engagement == {"votes": 9, "comments": 1} and launch.raw["topics"] == ["Fintech"]
    assert comment.text == "Does it handle QuickBooks?" and len(comment.author_hash) == 64
    assert bodies[0]["variables"]["after"] == "2026-10-08T00:00:00Z" and bodies[1]["variables"]["cursor"] == "Mw"


async def test_rate_limit_stops_the_run(make_services):
    calls = []
    async with make_services(src(), lambda r: calls.append(r) or httpx.Response(429)) as svc:
        r = (await run_source(svc, svc.sources.get("producthunt"), since=SINCE)).result
    assert len(calls) == 2 and "rate limit" in r.errors[0]


@signal_vcr.use_cassette("producthunt.yaml")
async def test_cassette_producthunt(settings, monkeypatch):
    from dotenv import dotenv_values
    if os.environ.get("VCR_RECORD_MODE") == "once":
        monkeypatch.setenv("PRODUCTHUNT_TOKEN", dotenv_values(".env")["PRODUCTHUNT_TOKEN"])
    sources = SourcesFile.model_validate(src(limit=25, comments=0))
    async with Services.open(settings, sources) as svc:
        out = await run_source(svc, svc.sources.get("producthunt"), since=SINCE, collect=True)
    assert out.result.status == "ok" and out.result.fetched == 25 and out.result.invalid == 0
    assert all(p.url.startswith("https://www.producthunt.com/") and "utm_" not in p.url for p in out.posts)
