from datetime import datetime, timedelta, timezone

import httpx
import pytest

from signal_connectors.config import SourcesFile
from signal_connectors.runner import run_source
from signal_connectors.services import Services

import os

import vcr

from .conftest import CASSETTES, _scrub_response

SINCE = datetime(2026, 10, 8, tzinfo=timezone.utc)
# postedTo is always "today": match the replay on method/host/path only (the key is scrubbed anyway).
dateless_vcr = vcr.VCR(cassette_library_dir=str(CASSETTES), record_mode=os.environ.get("VCR_RECORD_MODE", "none"),
                       match_on=["method", "scheme", "host", "path"], filter_query_parameters=["api_key"],
                       before_record_response=_scrub_response, decode_compressed_response=True)


def opp(i, **kw):
    o = {"noticeId": f"n{i}", "title": "Case management software", "solicitationNumber": f"S{i}",
         "fullParentPathName": "DEPT OF JUSTICE.EXECUTIVE OFFICE.OFFICE X", "postedDate": "2026-10-08",
         "type": "Solicitation", "naicsCode": "541511", "typeOfSetAsideDescription": "Total Small Business",
         "responseDeadLine": "2026-11-01T17:00:00-04:00", "uiLink": f"https://sam.gov/opp/n{i}/view",
         "pointOfContact": [{"fullName": "Pat Officer", "email": "pat@agency.gov", "phone": "555"}]}
    o.update(kw)
    return o


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv("SAM_GOV_API_KEY", "sam-test")


def src(**extra):
    return {"sources": {"sam_gov": {"route": "own_api", "source_type": "government",
                                    "queries": [{"params": {"ptype": "o,k"}, "limit": 2000}], **extra}}}


async def test_paging_quota_and_no_contacts(make_services):
    seen = []

    def handler(request):
        p = dict(request.url.params)
        seen.append(p)
        if p["offset"] == "0":
            return httpx.Response(200, json={"totalRecords": 1001, "opportunitiesData": [opp(i) for i in range(1000)]})
        return httpx.Response(200, json={"totalRecords": 1001, "opportunitiesData": [opp(1000)]})

    async with make_services(src(), handler) as svc:
        out = await run_source(svc, svc.sources.get("sam_gov"), since=SINCE, collect=True)
        assert svc.costs.units_used("sam_gov", timedelta(days=1)) == 2
    assert out.result.new == 1001 and [p["offset"] for p in seen] == ["0", "1000"]
    assert seen[0]["postedFrom"] == "10/08/2026" and seen[0]["ptype"] == "o,k" and seen[0]["api_key"] == "sam-test"
    p = out.posts[0]
    assert p.title == "Case management software" and p.community == "Dept Of Justice"
    assert "NAICS 541511" in p.text and "Total Small Business" in p.text and "2026-11-01" in p.text
    assert "Pat Officer" not in p.model_dump_json() and "pat@agency.gov" not in p.model_dump_json()


async def test_daily_quota_stops(make_services):
    calls = []
    async with make_services(src(quota={"units": 1, "window_days": 1}),
                             lambda r: calls.append(r) or httpx.Response(200, json={"totalRecords": 0})) as svc:
        svc.costs.record("sam_gov", units=1)
        r = (await run_source(svc, svc.sources.get("sam_gov"), since=SINCE)).result
    assert not calls and "units used" in r.errors[0]


@dateless_vcr.use_cassette("sam_gov.yaml")
async def test_cassette_sam_gov(settings, monkeypatch):
    from dotenv import dotenv_values
    real = dotenv_values(".env").get("SAM_GOV_API_KEY")
    if os.environ.get("VCR_RECORD_MODE") == "once" and real:
        monkeypatch.setenv("SAM_GOV_API_KEY", real)
    sources = SourcesFile.model_validate({"sources": {"sam_gov": {"route": "own_api", "source_type": "government",
                                                                  "queries": [{"params": {"ptype": "o,k"}, "limit": 25}]}}})
    async with Services.open(settings, sources) as svc:
        out = await run_source(svc, svc.sources.get("sam_gov"), since=SINCE, collect=True)
    assert out.result.status == "ok" and out.result.fetched == 25 and out.result.invalid == 0
    assert all(p.url.startswith("https://sam.gov/opp/") and p.title for p in out.posts)
