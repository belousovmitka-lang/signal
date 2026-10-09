from datetime import datetime, timezone

import httpx

from signal_connectors.config import SourcesFile
from signal_connectors.runner import run_source
from signal_connectors.services import Services

from .conftest import signal_vcr

SINCE = datetime(2026, 10, 7, tzinfo=timezone.utc)


def complaint(cid):
    return {"_source": {"complaint_id": str(cid), "date_received": "2026-10-08T12:00:00.000Z",
                        "product": "Mortgage", "sub_product": "Conventional home mortgage",
                        "issue": "Trouble during payment process", "sub_issue": None, "company": "Acme Lending",
                        "state": "OH", "zip_code": "43004", "company_response": "In progress", "timely": "Y"}}


def src(*queries):
    return {"sources": {"cfpb": {"route": "own_api", "source_type": "government", "queries": list(queries)}}}


async def test_break_point_paging_identical_text_and_no_zip(make_services):
    seen = []

    def handler(request):
        params = dict(request.url.params)
        seen.append(params)
        if params.get("page") == "2":
            return httpx.Response(200, json={"hits": {"hits": [complaint(i) for i in range(100, 150)]},
                                             "_meta": {"break_points": {}}})
        return httpx.Response(200, json={"hits": {"hits": [complaint(i) for i in range(100)]},
                                         "_meta": {"break_points": {"2": [1791513790000, "99"]}}})

    async with make_services(src({"params": {"product": ["Mortgage"]}, "limit": 500}), handler) as svc:
        out = await run_source(svc, svc.sources.get("cfpb"), since=SINCE, collect=True)
    assert out.result.fetched == 150 and out.result.new == 150   # same text, distinct complaints
    assert seen[0]["product"] == "Mortgage" and seen[0]["date_received_min"] == "2026-10-07" and "page" not in seen[0]
    assert (seen[1]["page"], seen[1]["frm"], seen[1]["search_after"]) == ("2", "100", "1791513790000_99")
    p = out.posts[0]
    assert p.title == "Trouble during payment process (Acme Lending)" and p.community == "Acme Lending"
    assert "43004" not in p.model_dump_json() and p.raw["state"] == "OH"
    assert p.url == "https://www.consumerfinance.gov/data-research/consumer-complaints/search/detail/0"


@signal_vcr.use_cassette("cfpb.yaml")
async def test_cassette_cfpb(settings):
    sources = SourcesFile.model_validate(src({"limit": 150}))
    async with Services.open(settings, sources) as svc:
        out = await run_source(svc, svc.sources.get("cfpb"), since=SINCE, collect=True)
    assert out.result.status == "ok" and out.result.invalid == 0 and out.result.new == 150
    assert len({p.raw["complaint_id"] for p in out.posts}) == 150        # page 2 differs from page 1
    assert all(p.title and "Product:" in p.text and "zip" not in p.raw for p in out.posts)
