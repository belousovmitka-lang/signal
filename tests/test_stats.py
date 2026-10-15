import os
from datetime import datetime, timezone

import httpx
import pytest
from dotenv import dotenv_values

from signal_connectors.config import SourcesFile
from signal_connectors.runner import run_source
from signal_connectors.services import Services

from .conftest import signal_vcr

SINCE = datetime(2026, 1, 1, tzinfo=timezone.utc)
SRC = {"sources": {"census_bfs": {"connector": "stats", "route": "own_api", "source_type": "research",
                                  "queries": [{"params": {"months": 2}, "limit": 100}]}}}
HEAD = ["cell_value", "data_type_code", "category_code", "seasonally_adj", "time_slot_id", "time", "us"]


def row(value, kind, code, month, adj="yes"):
    return [str(value), kind, code, adj, "0", month, "1"]


ROWS = [HEAD,
        row(1000, "BA_BA", "NAICS72", "2025-09"), row(1100, "BA_BA", "NAICS72", "2026-08"),
        row(1210, "BA_BA", "NAICS72", "2026-09"), row(999, "BA_BA", "NAICS72", "2026-09", adj="no"),
        row(300, "BA_HBA", "NAICS72", "2026-09"), row(250, "BA_HBA", "NAICS72", "2026-08"),
        row(77, "BA_CBA", "NAICS72", "2026-09"), row("(S)", "BA_BA", "NAICS22", "2026-09")]


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv("CENSUS_API_KEY", "test-key")


async def test_sector_months_with_changes(make_services):
    seen = {}

    def api(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        seen.update(request.url.params)
        return httpx.Response(200, json=ROWS)

    async with make_services(SRC, api) as svc:
        out = await run_source(svc, svc.sources.get("census_bfs"), since=SINCE, collect=True)
    assert out.result.status == "ok" and out.result.new == 2            # NAICS72: Aug + Sep; suppressed cell skipped
    sep = next(p for p in out.posts if p.raw["month"] == "2026-09")
    assert sep.title == "New business applications, Accommodation and food services: September 2026"
    assert sep.industry_hint == "restaurants" and sep.source_type == "research"
    assert sep.text.startswith("Business applications in Accommodation and food services (72), United States, "
                               "September 2026: 1,210 seasonally adjusted (+10.0% vs the previous month, +21.0% vs a year earlier).")
    assert "High-propensity applications (likely employers): 300 (+20.0% m/m, n/a y/y)." in sep.text
    assert sep.url == "https://www.census.gov/econ/bfs/index.html?month=2026-09&sector=NAICS72"
    assert "not endorsed or certified" in sep.raw["notice"]
    assert seen["for"] == "us:*" and seen["time"].startswith("from ") and seen["key"] == "test-key"


@signal_vcr.use_cassette("census_bfs.yaml")
async def test_cassette_census_bfs(settings, monkeypatch):
    if os.environ.get("VCR_RECORD_MODE", "none") != "none":
        monkeypatch.setenv("CENSUS_API_KEY", dotenv_values(".env")["CENSUS_API_KEY"])
    async with Services.open(settings, SourcesFile.model_validate(SRC)) as svc:
        out = await run_source(svc, svc.sources.get("census_bfs"), since=SINCE, collect=True)
    assert out.result.status == "ok" and out.result.invalid == 0 and out.result.new >= 30   # ~20 sectors x 2 months
    total = [p for p in out.posts if p.raw["sector"] == "TOTAL"]
    assert len(total) == 2 and all(p.raw["applications"] > 100_000 for p in total)
