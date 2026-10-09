from datetime import datetime, timezone

import httpx

from signal_connectors.config import SourcesFile
from signal_connectors.runner import run_source
from signal_connectors.services import Services

from .conftest import signal_vcr


def src(**params):
    return {"sources": {"wikipedia": {"route": "own_api", "source_type": "research",
                                      "queries": [{"params": {"top": 3, **params}, "limit": 50}]}}}


async def test_top_articles_skip_special_pages(make_services):
    seen = []

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        seen.append(request.url.path)
        return httpx.Response(200, json={"items": [{"articles": [
            {"article": "Main_Page", "views": 9, "rank": 1}, {"article": "Special:Search", "views": 8, "rank": 2},
            {"article": "Prior_authorization", "views": 7000, "rank": 3}, {"article": "Too_low", "views": 1, "rank": 4}]}]})

    async with make_services(src(), handler) as svc:
        out = await run_source(svc, svc.sources.get("wikipedia"), since=datetime(2026, 10, 7, tzinfo=timezone.utc),
                               collect=True)
    assert [p.title for p in out.posts][0] == "Prior authorization"
    assert out.posts[0].url == "https://en.wikipedia.org/wiki/Prior_authorization"
    assert out.posts[0].engagement == {"views": 7000, "rank": 3}
    assert seen[0].endswith("/top/en.wikipedia/all-access/2026/10/07")


@signal_vcr.use_cassette("wikipedia.yaml")
async def test_cassette_wikipedia(settings):
    sources = SourcesFile.model_validate({"sources": {"wikipedia": {
        "route": "own_api", "source_type": "research", "queries": [{"params": {"top": 100}, "limit": 25}]}}})
    async with Services.open(settings, sources) as svc:
        out = await run_source(svc, svc.sources.get("wikipedia"), since=datetime(2026, 10, 7, tzinfo=timezone.utc),
                               collect=True)
    assert out.result.status == "ok" and out.result.fetched == 25
    assert all(not p.title.startswith(("Special:", "Main Page")) for p in out.posts)
