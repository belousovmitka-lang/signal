from datetime import datetime, timezone

import httpx

from signal_connectors.config import SourcesFile
from signal_connectors.runner import run_source
from signal_connectors.services import Services

from .conftest import signal_vcr

SINCE = datetime(2026, 10, 2, tzinfo=timezone.utc)


def src(*queries):
    return {"sources": {"huggingface": {"route": "own_api", "source_type": "startup", "queries": list(queries)}}}


async def test_spaces_mapping_snapshot_and_id_dedupe(make_services):
    item = {"id": "acme/invoice-ocr", "likes": 40, "trendingScore": 12, "createdAt": "2026-08-01T00:00:00.000Z",
            "sdk": "gradio", "tags": ["gradio", "region:us", "ocr"],
            "cardData": {"title": "Invoice OCR", "short_description": "Turn PDFs into rows"}}
    seen = {}

    def handler(request):
        seen.update(request.url.params)
        return httpx.Response(200, json=[item, {**item, "id": "acme/invoice-ocr-2"}])

    async with make_services(src({"params": {"kind": "spaces"}, "limit": 10}), handler) as svc:
        out = await run_source(svc, svc.sources.get("huggingface"), since=SINCE, collect=True)
    assert out.result.new == 2      # older than since (a trending snapshot) and same text, both kept
    p = out.posts[0]
    assert p.url == "https://huggingface.co/spaces/acme/invoice-ocr" and p.title == "Invoice OCR"
    assert p.text == "Turn PDFs into rows\nTags: gradio, ocr" and p.engagement == {"likes": 40, "trending": 12}
    assert seen["sort"] == "trendingScore" and seen["direction"] == "-1"


@signal_vcr.use_cassette("huggingface.yaml")
async def test_cassette_huggingface(settings):
    sources = SourcesFile.model_validate(src({"params": {"kind": "spaces"}, "limit": 15},
                                             {"params": {"kind": "models"}, "limit": 15}))
    async with Services.open(settings, sources) as svc:
        out = await run_source(svc, svc.sources.get("huggingface"), since=SINCE, collect=True)
    assert out.result.status == "ok" and out.result.invalid == 0 and out.result.fetched == 30
    assert {p.source for p in out.posts} == {"huggingface:spaces", "huggingface:models"}
