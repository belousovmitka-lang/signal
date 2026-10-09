import json
from datetime import datetime, timezone

import httpx
import pytest

from signal_connectors.runner import run_source

SINCE = datetime(2026, 10, 1, tzinfo=timezone.utc)


def entity(i, cats=("Legal Tech",)):
    return {"uuid": f"u{i}", "properties": {
        "identifier": {"value": f"Seed Round - Docket{i}", "permalink": f"docket{i}-seed"},
        "announced_on": "2026-10-03", "investment_type": "seed",
        "money_raised": {"value": 2000000, "currency": "USD", "value_usd": 2000000},
        "funded_organization_identifier": {"value": f"Docket{i}", "permalink": f"docket{i}"},
        "funded_organization_categories": [{"value": c} for c in cats],
        "short_description": "Intake automation for small law firms.", "num_investors": 3}}


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv("CRUNCHBASE_KEY", "cb-test")


async def test_query_paging_and_category_filter(make_services):
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        assert request.url.params["user_key"] == "cb-test"
        n = body["limit"]
        if "after_id" not in body:
            return httpx.Response(200, json={"entities": [entity(i) for i in range(n - 1)] + [entity(99, ("Fintech",))]})
        return httpx.Response(200, json={"entities": [entity(100)]})

    cfg = {"sources": {"crunchbase": {"route": "own_api", "source_type": "startup", "queries": [
        {"industry_hint": "legal", "params": {"categories": ["legal tech"]}, "limit": 20}]}}}
    async with make_services(cfg, handler) as svc:
        out = await run_source(svc, svc.sources.get("crunchbase"), since=SINCE, collect=True)
    assert out.result.new == 20      # 19 legal + page 2's one; the Fintech round filtered out
    assert bodies[0]["query"][0] == {"type": "predicate", "field_id": "announced_on", "operator_id": "gte",
                                     "values": ["2026-10-01"]}
    assert bodies[0]["order"] == [{"field_id": "announced_on", "sort": "desc"}] and bodies[1]["after_id"] == "u99"
    p = out.posts[0]
    assert p.url == "https://www.crunchbase.com/funding_round/docket0-seed" and p.community == "Docket0"
    assert p.money == {"amount": 2000000, "currency": "USD", "usd": 2000000}
