from datetime import datetime, timezone

import httpx
import pytest

from signal_connectors.runner import run_source

SINCE = datetime(2026, 10, 5, tzinfo=timezone.utc)


def src(budget=1.0):
    return {"sources": {"newsapi": {"route": "own_api", "source_type": "news", "paid": True,
                                    "feature_flag": "ENABLE_NEWSAPI", "enabled": True, "monthly_budget_usd": budget,
                                    "queries": [{"keywords": ["paralegal AND software"], "industry_hint": "legal", "limit": 200}]}}}


def article(i):
    return {"source": {"id": None, "name": "Legal Dive"}, "author": "Reporter Name", "title": f"Story {i}",
            "description": f"Firms struggle with intake, report number {i} of the series.", "url": f"https://news.example/{i}?utm_source=newsapi",
            "publishedAt": "2026-10-06T10:00:00Z", "content": "Firms are still doing intake by hand… [+1200 chars]"}


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv("NEWSAPI_KEY", "news-test")


async def test_paging_metering_and_mapping(make_services):
    seen = []

    def handler(request):
        seen.append(request)
        page = int(request.url.params["page"])
        arts = [article(i) for i in range(100)] if page == 1 else [article(100 + i) for i in range(30)]
        return httpx.Response(200, json={"status": "ok", "totalResults": 130, "articles": arts})

    async with make_services(src(), handler) as svc:
        out = await run_source(svc, svc.sources.get("newsapi"), since=SINCE, collect=True)
        assert svc.costs.spent_this_month("newsapi") == pytest.approx(0.0036)
    assert out.result.new == 130 and len(seen) == 2
    assert seen[0].headers["x-api-key"] == "news-test" and seen[0].url.params["from"] == "2026-10-05T00:00:00"
    p = out.posts[0]
    assert p.url == "https://news.example/0" and p.is_snippet and p.community == "Legal Dive"
    assert "Reporter Name" not in p.model_dump_json()


async def test_budget_and_plan_cap(make_services):
    def handler(request):
        return httpx.Response(426, json={"status": "error", "code": "maximumResultsReached", "message": "cap"})

    async with make_services(src(), handler) as svc:
        r = (await run_source(svc, svc.sources.get("newsapi"), since=SINCE)).result
    assert r.status == "ok" and r.fetched == 0
    async with make_services(src(budget=0.001), handler) as svc:
        r = (await run_source(svc, svc.sources.get("newsapi"), since=SINCE)).result
    assert "refusing" in r.errors[0]


def test_flag_off_by_default(monkeypatch):
    from signal_connectors.config import SourcesFile
    monkeypatch.delenv("ENABLE_NEWSAPI", raising=False)
    assert "ENABLE_NEWSAPI" in SourcesFile.model_validate(src()).get("newsapi").blocked_reason()
