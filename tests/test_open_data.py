from datetime import datetime, timezone

import httpx

from signal_connectors.config import SourcesFile
from signal_connectors.connectors.open_data import render
from signal_connectors.runner import run_source
from signal_connectors.services import Services

from .conftest import signal_vcr

SINCE = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)
PERMITS = {"kind": "socrata", "domain": "data.cityofchicago.org", "dataset": "ydr8-5enu", "date_field": "issue_date",
           "id_fields": ["id"], "title": "{work_type}: {work_description}", "text": "{permit_type}. Cost ${reported_cost}.",
           "community": "Chicago permits / {work_type}", "keep": ["permit_type", "reported_cost"]}


def src(*queries):
    return {"sources": {"open_data": {"route": "own_api", "source_type": "government", "queries": list(queries)}}}


def test_render_tolerates_missing_fields():
    assert render("{a}: {b}", {"a": "Electrical", "b": None}) == "Electrical"
    assert render("{a} cost ${c}.", {"a": "Roof"}) == "Roof cost $"
    assert render(None, {}) == ""


async def test_socrata_stores_only_named_fields(make_services):
    seen = []
    row = {"id": "N1", "issue_date": "2026-10-06T00:00:00.000", "work_type": "Electrical Work",
           "work_description": "ADD 6 CIRCUITS", "permit_type": "EXPRESS", "reported_cost": "8000",
           "contact_2_name": "ZIBRIDA JENNIFER", "street_name": "111TH ST"}

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        seen.append(dict(request.url.params))
        return httpx.Response(200, json=[row, {**row, "id": "N2"}, {**row, "id": None}])

    async with make_services(src({"industry_hint": "construction", "params": PERMITS, "limit": 10}), handler) as svc:
        out = await run_source(svc, svc.sources.get("open_data"), since=SINCE, collect=True)
    assert [p.url for p in out.posts] == ["https://data.cityofchicago.org/resource/ydr8-5enu.json?id=N1",
                                          "https://data.cityofchicago.org/resource/ydr8-5enu.json?id=N2"]
    assert out.result.new == 2      # identical text, distinct rows
    p = out.posts[0]
    assert p.title == "Electrical Work: ADD 6 CIRCUITS" and p.text == "EXPRESS. Cost $8000"
    assert p.raw == {"dataset": "ydr8-5enu", "domain": "data.cityofchicago.org", "permit_type": "EXPRESS",
                     "reported_cost": "8000"}
    assert "JENNIFER" not in p.model_dump_json() and "111TH" not in p.model_dump_json()
    assert seen[0]["$where"] == "issue_date >= '2026-10-06T00:00:00'" and seen[0]["$order"] == "issue_date DESC"


@signal_vcr.use_cassette("open_data.yaml")
async def test_cassette_socrata_and_ckan(settings):
    sources = SourcesFile.model_validate(src(
        {"industry_hint": "construction", "params": PERMITS, "limit": 15},
        {"params": {"kind": "ckan", "base": "https://open.canada.ca/data"}, "limit": 10}))
    async with Services.open(settings, sources) as svc:
        # Replay needn't honor open.canada.ca's 20s crawl delay (tested in test_robots).
        svc.http.limiter.set_rate = svc.http.limiter.defer = lambda *a: None
        svc.http.limiter.default_rps = 1000
        out = await run_source(svc, svc.sources.get("open_data"), since=SINCE, collect=True)
    assert out.result.status == "ok" and out.result.invalid == 0 and out.result.fetched >= 20
    permits = [p for p in out.posts if p.source == "open_data:data.cityofchicago.org:ydr8-5enu"]
    datasets = [p for p in out.posts if p.source == "open_data:open.canada.ca/data"]
    assert len(permits) == 15 and len(datasets) == 10
    assert all(set(p.raw) <= {"dataset", "domain", "permit_type", "reported_cost"} for p in permits)
    assert all(p.url.startswith("https://open.canada.ca/data/dataset/") and p.title for p in datasets)


async def test_socrata_counts_posts_one_count_per_group_with_change(make_services):
    seen = []

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /browse?*&q=\n")
        params = dict(request.url.params)
        seen.append(params)
        previous = len(seen) == 2
        return httpx.Response(200, json=[
            {"issue_type": "Internet", "issue": "Billing", "n": "300" if previous else "450"},
            {"issue_type": "Phone", "issue": "Junk Faxes", "n": "5"},
        ] + ([] if previous else [{"issue_type": "Internet", "issue": "Speed", "n": "60"}]))

    sources = {"sources": {"open_data": {"route": "own_api", "source_type": "government", "queries": [
        {"limit": 10, "params": {"kind": "socrata_counts", "domain": "opendata.fcc.gov", "dataset": "3xyp-aqkj",
                                 "date_field": "date_created", "group": ["issue_type", "issue"], "window_days": 7,
                                 "min_count": 20, "unit": "complaints", "title": "FCC complaints: {issue_type} / {issue}"}}]}}}
    async with make_services(sources, handler) as svc:
        out = await run_source(svc, svc.sources.get("open_data"), collect=True)
    assert seen[0]["$group"] == "issue_type, issue" and "count(*) AS n" in seen[0]["$select"]
    assert out.result.new == 2                                       # Junk Faxes is under min_count
    billing, speed = out.posts
    assert billing.title == "FCC complaints: Internet / Billing" and "450 complaints" in billing.text
    assert "+50% vs the 7 days before (300)" in billing.text and billing.raw["previous_count"] == 300
    assert "new this window" in speed.text
    async with make_services(sources, handler) as svc:                 # same window again: no duplicates
        seen.clear()
        again = await run_source(svc, svc.sources.get("open_data"), collect=True)
    assert again.result.new == 0
