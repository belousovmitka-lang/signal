import json
import os
from datetime import datetime, timezone

import httpx
import pytest
from dotenv import dotenv_values

from signal_connectors.config import SourcesFile
from signal_connectors.connectors.osha import standard_code
from signal_connectors.runner import run_source
from signal_connectors.services import Services

from .conftest import signal_vcr

SINCE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def src(days=30):
    return {"sources": {"osha": {"route": "own_api", "source_type": "government",
                                 "rate_limit": {"requests_per_second": 0.25, "domains": ["apiprod.dol.gov"]},
                                 "queries": [{"params": {"days": days}, "limit": 50}]}}}


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv("DOL_API_KEY", "test-key")


def test_standard_codes():
    assert standard_code("19260501 B10") == "1926.501" and standard_code("19100147 C06 I") == "1910.147"
    assert standard_code("19030019 C01") == "1903.19" and standard_code("5A0001") == "5A.1"


VIOLATIONS = [
    {"activity_nr": 1, "standard": "19100037 A03", "viol_type": "S", "initial_penalty": 4000.0, "current_penalty": 2000.0},
    {"activity_nr": 1, "standard": "19101200 E01", "viol_type": "O", "initial_penalty": 0.0, "current_penalty": 0.0},
    {"activity_nr": 2, "standard": "19100037 B02", "viol_type": "R", "initial_penalty": 9000.0, "current_penalty": 9000.0},
    {"activity_nr": 3, "standard": "19260501 B10", "viol_type": "S", "initial_penalty": 16000.0, "current_penalty": 16000.0},
    {"activity_nr": 4, "standard": "19100037 A03", "viol_type": "S", "initial_penalty": 1.0, "current_penalty": 1.0,
     "delete_flag": "X"},
]
INSPECTIONS = {1: "722511", 2: "722513", 3: "238160", 4: "722511"}


async def test_aggregates_per_industry_without_names(make_services):
    calls = []

    def api(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(403)
        params = dict(request.url.params)
        assert params["X-API-KEY"] == "test-key"
        calls.append((request.url.path, params.get("fields")))
        if request.url.path.endswith("/violation/json"):
            if params.get("limit") == "1":
                return httpx.Response(200, json={"data": [{"issuance_date": "2026-09-11T00:00:00"}]})
            assert json.loads(params["filter_object"]) == {"field": "issuance_date", "operator": "gt", "value": "2026-08-12"}
            return httpx.Response(200, json={"data": VIOLATIONS})
        ids = json.loads(params["filter_object"])["value"]
        return httpx.Response(200, json={"data": [{"activity_nr": i, "naics_code": INSPECTIONS[i]} for i in ids]})

    async with make_services(src(), api) as svc:
        out = await run_source(svc, svc.sources.get("osha"), since=SINCE, collect=True)
    assert out.result.status == "ok" and {p.industry_hint for p in out.posts} == {"restaurants", "construction"}
    food = next(p for p in out.posts if p.industry_hint == "restaurants")
    assert food.text == ("OSHA citations issued Aug 12 to Sep 11, 2026 in NAICS 722: 3 violations (1 serious, "
                         "1 other-than-serious, 1 repeat) from 2 inspections; initial penalties $13,000, current $11,000. "
                         "Most cited standards: 1910.37 exit routes (2); 1910.1200 hazard communication (1).")
    assert food.money == {"initial_penalties_usd": 13000, "current_penalties_usd": 11000}
    assert food.published_at == datetime(2026, 9, 11, tzinfo=timezone.utc)
    assert food.url == "https://www.osha.gov/data?industry=restaurants&through=2026-09-11"
    assert "estab" not in str(food.raw) and "activity" not in str(food.raw)           # aggregates only
    assert all("estab_name" not in (f or "") for _, f in calls)                       # names never requested


@pytest.mark.skip(reason="cassette not recorded yet: DOL API answered 429 on 2026-10-15; record with VCR_RECORD_MODE=once")
@signal_vcr.use_cassette("osha.yaml")
async def test_cassette_osha(settings, monkeypatch):
    if os.environ.get("VCR_RECORD_MODE", "none") != "none":
        monkeypatch.setenv("DOL_API_KEY", dotenv_values(".env")["DOL_API_KEY"])
    async with Services.open(settings, SourcesFile.model_validate(src(days=2))) as svc:
        out = await run_source(svc, svc.sources.get("osha"), since=SINCE, collect=True)
    assert out.result.status == "ok" and out.result.invalid == 0 and out.result.new >= 3
    assert all(p.raw["violations"] > 0 and p.raw["top_standards"] for p in out.posts)
