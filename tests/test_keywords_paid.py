import httpx
import pytest

from signal_connectors.runner import run_source

CSV = ("Keyword;Search Volume;CPC;Competition;Keyword Difficulty Index\r\n"
       "how to automate legal billing;590;12.40;0.61;38\r\n"
       "is there software for law firm intake;210;9.10;0.44;22\r\n")


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv("SEMRUSH_KEY", "sem-test")


def src(budget=1.0, **opts):
    return {"sources": {"keywords_paid": {"route": "own_api", "source_type": "research", "monthly_budget_usd": budget,
                                          "options": {"usd_per_1000_units": 0.05, **opts},
                                          "queries": [{"keywords": ["legal billing software", "nothing here"],
                                                       "industry_hint": "legal", "limit": 10}]}}}


async def test_csv_rows_units_and_nothing_found(make_services):
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        if request.url.params["phrase"] == "nothing here":
            return httpx.Response(200, text="ERROR 50 :: NOTHING FOUND")
        return httpx.Response(200, text=CSV)

    async with make_services(src(), handler) as svc:
        out = await run_source(svc, svc.sources.get("keywords_paid"), collect=True)
        from datetime import timedelta
        assert svc.costs.units_used("keywords_paid", timedelta(days=1)) == 800    # 2 calls x 10 lines x 40
    assert out.result.status == "ok" and [p.title for p in out.posts] == [
        "how to automate legal billing", "is there software for law firm intake"]
    p = out.posts[0]
    assert p.engagement == {"search_volume": 590} and p.money == {"cpc_usd": 12.4}
    assert p.url == "https://www.semrush.com/analytics/keywordoverview?db=us&q=how%20to%20automate%20legal%20billing"
    assert seen[0]["type"] == "phrase_questions" and seen[0]["export_columns"] == "Ph,Nq,Cp,Co,Kd"


async def test_budget_refuses_before_calling(make_services):
    calls = []
    async with make_services(src(budget=0.001), lambda r: calls.append(r) or httpx.Response(200, text=CSV)) as svc:
        r = (await run_source(svc, svc.sources.get("keywords_paid"))).result
    assert not calls and "refusing" in r.errors[0]
