from datetime import datetime, timezone

import httpx

from signal_connectors.config import SourcesFile
from signal_connectors.runner import run_source
from signal_connectors.services import Services

from .conftest import signal_vcr

SINCE = datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc)


def src(*queries):
    return {"sources": {"federal_register": {"route": "own_api", "source_type": "government", "queries": list(queries)}}}


async def test_params_paging_and_mapping(make_services):
    seen = []
    page2 = "https://www.federalregister.gov/api/v1/documents.json?page=2"

    def handler(request):
        seen.append(request.url)
        doc = {"title": "Small Business Size Standards", "abstract": "SBA proposes to revise...",
               "html_url": "https://www.federalregister.gov/documents/2026/10/06/2026-1/small-business-size-standards",
               "publication_date": "2026-10-06", "type": "Proposed Rule", "document_number": "2026-1",
               "agencies": [{"name": "Small Business Administration"}], "comments_close_on": "2026-12-05",
               "docket_ids": ["SBA-2026-0001"], "regulation_id_numbers": ["3245-AH00"], "significant": False,
               "action": "Proposed rule."}
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json={"results": [{**doc, "document_number": "2026-2",
                                                          "html_url": doc["html_url"] + "-2"}]})
        return httpx.Response(200, json={"results": [doc], "next_page_url": page2})

    q = {"params": {"types": ["PRORULE"]}, "keywords": ["small business", "SBA"]}
    async with make_services(src(q), handler) as svc:
        out = await run_source(svc, svc.sources.get("federal_register"), since=SINCE, collect=True)
    assert len(out.posts) == 2   # dated at midnight on the cursor's day, kept (day-granular)
    params = seen[0].params
    assert params.get_list("conditions[type][]") == ["PRORULE"]
    assert params["conditions[term]"] == '"small business" | SBA'
    assert params["conditions[publication_date][gte]"] == "2026-10-06" and params["order"] == "newest"
    p = out.posts[0]
    assert p.community == "Small Business Administration" and p.raw["comments_close_on"] == "2026-12-05"
    assert p.text == "SBA proposes to revise..." and p.source_type == "government"


@signal_vcr.use_cassette("federal_register.yaml")
async def test_cassette_federal_register(settings):
    sources = SourcesFile.model_validate(src({"params": {"types": ["RULE", "PRORULE"]}, "limit": 30}))
    async with Services.open(settings, sources) as svc:
        out = await run_source(svc, svc.sources.get("federal_register"), since=SINCE, collect=True)
    assert out.result.status == "ok" and out.result.invalid == 0 and out.result.fetched >= 20
    for p in out.posts:
        assert p.url.startswith("https://www.federalregister.gov/documents/") and p.title
        assert p.raw["type"] in {"Rule", "Proposed Rule"} and p.published_at.date() >= SINCE.date()
