from datetime import datetime, timezone

import httpx
import pytest

from signal_connectors.config import SourcesFile
from signal_connectors.runner import run_source
from signal_connectors.services import Services

from .conftest import signal_vcr

SINCE = datetime(2026, 10, 8, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv("REGULATIONS_GOV_API_KEY", "reg-test")


def src(limit=10):
    return {"sources": {"regulations_gov": {"route": "own_api", "source_type": "government",
                                            "queries": [{"params": {"agency": "SBA"}, "limit": limit,
                                                         "industry_hint": "accounting"}]}}}


async def test_listing_details_and_no_submitter_data(make_services):
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path == "/v4/comments":
            return httpx.Response(200, json={"data": [{"id": "SBA-2026-0001-0001", "attributes": {
                "title": "Comment Submitted by Jane Doe"}}]})
        return httpx.Response(200, json={"data": {"attributes": {
            "comment": "As a small lender, the new form doubles our paperwork.&nbsp;", "organization": "Acme Credit Union",
            "category": "Company", "agencyId": "SBA", "docketId": "SBA-2026-0001", "postedDate": "2026-10-08T04:00:00Z",
            "firstName": "Jane", "lastName": "Doe", "email": "jane@acme.example", "city": "Columbus", "zip": "43004"}}})

    async with make_services(src(), handler) as svc:
        out = await run_source(svc, svc.sources.get("regulations_gov"), since=SINCE, collect=True)
    (p,) = out.posts
    assert p.title == "Public comment to SBA on SBA-2026-0001 from Acme Credit Union"
    assert p.text == "As a small lender, the new form doubles our paperwork." and p.author_role == "Company"
    dumped = p.model_dump_json()
    assert "Jane" not in dumped and "Doe" not in dumped and "Columbus" not in dumped and "43004" not in dumped
    params = seen[0].url.params
    assert params["filter[agencyId]"] == "SBA" and params["filter[postedDate][ge]"] == "2026-10-08"
    assert seen[0].headers["x-api-key"] == "reg-test"


@signal_vcr.use_cassette("regulations_gov.yaml")
async def test_cassette_regulations_gov(settings, monkeypatch):
    import os
    from dotenv import dotenv_values
    if os.environ.get("VCR_RECORD_MODE") == "once":
        monkeypatch.setenv("REGULATIONS_GOV_API_KEY", dotenv_values(".env")["REGULATIONS_GOV_API_KEY"])
    sources = SourcesFile.model_validate(src(limit=20))
    async with Services.open(settings, sources) as svc:
        out = await run_source(svc, svc.sources.get("regulations_gov"), since=SINCE, collect=True)
    assert out.result.status == "ok" and out.result.fetched >= 15 and out.result.invalid == 0
    assert all(p.url.startswith("https://www.regulations.gov/comment/SBA-") and "Submitted by" not in p.title
               for p in out.posts)
