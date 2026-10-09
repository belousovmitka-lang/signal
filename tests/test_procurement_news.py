import json
from datetime import datetime, timezone

import httpx
import pytest

from signal_connectors.config import SourcesFile
from signal_connectors.connectors.gdelt import parse_seendate
from signal_connectors.connectors.ted import pick
from signal_connectors.runner import run_source
from signal_connectors.services import Services

from .conftest import signal_vcr

SINCE = datetime(2026, 10, 5, 15, 30, tzinfo=timezone.utc)   # mid-day: day-granular dates must survive


def src(name, *queries, **extra):
    return {"sources": {name: {"route": "own_api", "source_type": "government", "queries": list(queries), **extra}}}


def test_helpers():
    assert pick({"deu": "Hallo", "eng": "Hello"}) == "Hello"
    assert pick({"deu": ["Hallo"]}) == "Hallo" and pick(["x"]) == "x" and pick(None) is None
    assert parse_seendate("20261009T120000Z") == datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
    assert parse_seendate("bad") is None


async def test_ted_query_sorting_mapping_and_day_granular_since(make_services):
    bodies = []
    notices = [{"publication-number": "1-2026", "notice-title": {"deu": "X", "eng": "Germany – Software – CRM"},
                "description-lot": {"eng": ["A CRM for 40 municipal offices."]}, "buyer-name": {"deu": ["Stadt Köln"]},
                "publication-date": "2026-10-05+02:00", "estimated-value-lot": ["250000"],
                "estimated-value-cur-lot": ["EUR"], "classification-cpv": ["48000000", "48000000"],
                "notice-type": "cn-standard", "buyer-country": ["DEU"]}]

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"notices": notices})

    async with make_services(src("ted", {"params": {"cpv": ["48000000"], "query": "buyer-country=DEU"}}), handler) as svc:
        out = await run_source(svc, svc.sources.get("ted"), since=SINCE, collect=True)
    (p,) = out.posts   # dated 2026-10-05 00:00, kept although since is 15:30 that day
    assert bodies[0]["query"] == ("(classification-cpv IN (48000000)) AND (buyer-country=DEU) AND "
                                  "publication-date>=20261005 SORT BY publication-date DESC")
    assert p.url == "https://ted.europa.eu/en/notice/-/detail/1-2026" and p.title == "Germany – Software – CRM"
    assert p.money == {"estimated_value": 250000.0, "currency": "EUR"} and p.community == "Stadt Köln"
    assert p.raw["cpv"] == ["48000000"] and p.source_type == "government"


def ocds(rid, tags=("tender",), cpv="72000000", title="Case management system", date="2026-10-06T10:00:00Z"):
    return {"ocid": f"ocds-{rid}", "id": rid, "date": date, "tag": list(tags), "buyer": {"name": "Leeds City Council"},
            "tender": {"title": title, "description": "Replace spreadsheets.", "status": "active",
                       "value": {"amount": 120000, "currency": "GBP"},
                       "classification": {"scheme": "CPV", "id": cpv, "description": "IT services"}}}


async def test_uk_tenders_filters_tags_cpv_and_follows_cursor(make_services):
    cf_id = "d2301388-4ca6-48fc-805d-eb2e5793377e-917336"
    pages = {
        None: {"releases": [ocds(cf_id), ocds("aaaaaaaa-0000-0000-0000-000000000000-1", tags=["award"]),
                            ocds("bbbbbbbb-0000-0000-0000-000000000000-1", cpv="45000000")],
               "links": {"next": "https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search?cursor=2"}},
        "2": {"releases": [ocds("cccccccc-0000-0000-0000-000000000000-1", cpv="48100000")], "links": {}},
    }

    def handler(request):
        return httpx.Response(200, json=pages[request.url.params.get("cursor")])

    q = {"params": {"service": "contracts_finder", "cpv_prefixes": ["72", "48"]}}
    async with make_services(src("uk_tenders", q), handler) as svc:
        out = await run_source(svc, svc.sources.get("uk_tenders"), since=SINCE, collect=True)
    assert [p.url.rsplit("/", 1)[1] for p in out.posts] == [cf_id[:36], "cccccccc-0000-0000-0000-000000000000"]
    p = out.posts[0]
    assert p.source == "uk_tenders:contracts_finder" and p.money == {"amount": 120000, "currency": "GBP"}
    assert p.community == "Leeds City Council" and p.raw["cpv"] == "72000000"


async def test_gdelt_429_stops_the_whole_run(make_services):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(429, text="Please limit requests to one every 5 seconds")

    cfg = src("gdelt", {"keywords": ["a"]}, {"keywords": ["b"]},
              rate_limit={"requests_per_second": 1000, "domains": ["api.gdeltproject.org"]})
    async with make_services(cfg, handler) as svc:
        r = (await run_source(svc, svc.sources.get("gdelt"), since=SINCE)).result
    assert len(calls) == 2           # one retry, then stop: the second query is never sent
    assert "rate limit" in r.errors[0] and len(r.errors) == 1


async def test_gdelt_mapping(make_services):
    seen = {}

    def handler(request):
        seen.update(request.url.params)
        return httpx.Response(200, headers={"content-type": "application/json"}, json={"articles": [
            {"url": "https://news.example/a?utm_source=x", "title": "Farmers turn to software amid labor shortage",
             "seendate": "20261006T080000Z", "domain": "news.example", "language": "English", "sourcecountry": "US"}]})

    cfg = src("gdelt", {"keywords": ["(farmers) (software)"], "industry_hint": "agriculture"},
              rate_limit={"requests_per_second": 1000, "domains": ["api.gdeltproject.org"]})
    async with make_services(cfg, handler) as svc:
        out = await run_source(svc, svc.sources.get("gdelt"), since=SINCE, collect=True)
    (p,) = out.posts
    assert seen["query"] == "(farmers) (software) sourcelang:english" and seen["startdatetime"] == "20261005153000"
    assert p.url == "https://news.example/a" and p.is_snippet and p.text == "" and p.language == "en"
    assert p.raw["attribution"].startswith("GDELT") and p.industry_hint == "agriculture"


CASSETTE_SINCE = datetime(2026, 10, 7, tzinfo=timezone.utc)


@signal_vcr.use_cassette("ted.yaml")
async def test_cassette_ted(settings):
    sources = SourcesFile.model_validate(src("ted", {"params": {"cpv": ["72000000", "48000000"]}, "limit": 25}))
    async with Services.open(settings, sources) as svc:
        out = await run_source(svc, svc.sources.get("ted"), since=CASSETTE_SINCE, collect=True)
    assert out.result.status == "ok" and out.result.fetched >= 20 and out.result.invalid == 0
    for p in out.posts:
        assert p.url.startswith("https://ted.europa.eu/en/notice/-/detail/") and p.title
        assert p.published_at.date() >= CASSETTE_SINCE.date()


@signal_vcr.use_cassette("uk_tenders.yaml")
async def test_cassette_uk_tenders(settings):
    sources = SourcesFile.model_validate(src(
        "uk_tenders", {"params": {"service": "contracts_finder"}, "limit": 15},
        {"params": {"service": "find_a_tender"}, "limit": 15}))
    async with Services.open(settings, sources) as svc:
        out = await run_source(svc, svc.sources.get("uk_tenders"), since=CASSETTE_SINCE, collect=True)
    assert out.result.status == "ok" and out.result.fetched >= 20 and out.result.invalid == 0
    assert {p.source for p in out.posts} == {"uk_tenders:contracts_finder", "uk_tenders:find_a_tender"}
    assert all(p.title and "tender" in p.raw["tags"] for p in out.posts)
