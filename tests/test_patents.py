import json
from datetime import datetime, timezone

import httpx

from signal_connectors.connectors.patents import title_query
from signal_connectors.runner import run_source

SINCE = datetime(2026, 10, 15, tzinfo=timezone.utc)


def record(n, title="SYSTEM AND METHOD FOR DENTAL SCHEDULING"):
    return {"applicationNumberText": f"18{n:06d}",
            "applicationMetaData": {"inventionTitle": title, "patentNumber": f"12{n:06d}", "grantDate": "2026-10-20",
                                    "filingDate": "2024-03-01", "applicationTypeCode": "UTL",
                                    "cpcClassificationBag": ["A61C 7/00", "G16H 40/20"],
                                    "inventorBag": [{"inventorNameText": "Ann Inventor"}],
                                    "examinerNameText": "Ed Examiner", "firstInventorName": "Ann Inventor"}}


def src():
    return {"sources": {"patents": {"route": "own_api", "source_type": "research", "env": ["USPTO_API_KEY"],
                                    "queries": [{"industry_hint": "dental", "keywords": ["dental", "tooth aligner"],
                                                 "limit": 150}]}}}


def test_title_query():
    assert title_query(["dental", "tooth aligner"]) == 'applicationMetaData.inventionTitle:(dental OR "tooth aligner")'


async def test_posts_new_grants_without_people(make_services, monkeypatch):
    monkeypatch.setenv("USPTO_API_KEY", "test-key")
    bodies = []

    def handler(request):
        assert request.method == "POST" and request.headers["X-API-KEY"] == "test-key"
        body = json.loads(request.content)
        bodies.append(body)
        offset = body["pagination"]["offset"]
        bag = [record(i) for i in range(offset, offset + 100)] if offset == 0 else [record(i) for i in range(100, 130)]
        return httpx.Response(200, json={"count": 130, "patentFileWrapperDataBag": bag})

    async with make_services(src(), handler) as svc:
        out = await run_source(svc, svc.sources.get("patents"), since=SINCE, collect=True)
    assert out.result.status == "ok" and out.result.new == 130 and len(bodies) == 2
    b = bodies[0]
    assert b["q"] == 'applicationMetaData.inventionTitle:(dental OR "tooth aligner")'
    assert b["rangeFilters"][0] == {"field": "applicationMetaData.grantDate", "valueFrom": "2026-10-15",
                                    "valueTo": b["rangeFilters"][0]["valueTo"]}
    assert b["filters"] == [{"name": "applicationMetaData.applicationTypeCode", "value": ["UTL"]}]
    assert bodies[1]["pagination"] == {"offset": 100, "limit": 100}
    p = out.posts[0]
    assert p.title == "System And Method For Dental Scheduling" and "CPC: A61C 7/00, G16H 40/20" in p.text
    assert p.url == "https://image-ppubs.uspto.gov/dirsearch-public/print/downloadPdf/12000000"
    assert "Inventor" not in p.model_dump_json() and "Examiner" not in p.model_dump_json()


async def test_no_matches_is_not_an_error(make_services, monkeypatch):
    monkeypatch.setenv("USPTO_API_KEY", "test-key")
    async with make_services(src(), lambda r: httpx.Response(404, json={"error": "No matching records"})) as svc:
        out = await run_source(svc, svc.sources.get("patents"), since=SINCE, collect=True)
    assert out.result.status == "ok" and out.result.new == 0


async def test_missing_key_fails_loudly(make_services, monkeypatch):
    monkeypatch.delenv("USPTO_API_KEY", raising=False)
    async with make_services(src(), lambda r: httpx.Response(500)) as svc:
        out = await run_source(svc, svc.sources.get("patents"), since=SINCE, collect=True)
    assert out.result.status == "failed" and "USPTO_API_KEY" in " ".join(out.result.errors)
