from datetime import datetime, timezone
from urllib.parse import parse_qs

import httpx

from signal_connectors.config import SourcesFile
from signal_connectors.runner import run_source
from signal_connectors.services import Services

from .conftest import signal_vcr

SINCE = datetime(2026, 10, 1, tzinfo=timezone.utc)


def b(celex, title, kind="REG_IMPL", date="2026-10-08"):
    return {"celex": {"value": celex}, "title": {"value": title}, "date": {"value": date},
            "type": {"value": f"http://publications.europa.eu/resource/authority/resource-type/{kind}"}}


def src(**q):
    return {"sources": {"eurlex": {"route": "own_api", "source_type": "government", "queries": [{"limit": 50, **q}]}}}


async def test_sparql_query_sanctions_excluded_and_mapping(make_services):
    bodies = []

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        bodies.append(parse_qs(request.content.decode())["query"][0])
        return httpx.Response(200, json={"results": {"bindings": [
            b("32026R2241", "Commission Implementing Regulation (EU) 2026/2241 on electronic invoicing"),
            b("32026R2241", "Commission Implementing Regulation (EU) 2026/2241 on electronic invoicing"),   # duplicate row
            b("32026D2261", "Council Decision (CFSP) 2026/2261 concerning restrictive measures", "DEC")]}})

    async with make_services(src(), handler) as svc:
        out = await run_source(svc, svc.sources.get("eurlex"), since=SINCE, collect=True)
    (p,) = out.posts
    assert p.url == "https://eur-lex.europa.eu/legal-content/EN/TXT?uri=CELEX:32026R2241"
    assert p.raw == {"celex": "32026R2241", "type": "REG_IMPL"} and p.source_type == "government"
    assert '"2026-10-01"^^xsd:date' in bodies[0] and "resource-type/DIR>" in bodies[0]


@signal_vcr.use_cassette("eurlex.yaml")
async def test_cassette_eurlex(settings):
    sources = SourcesFile.model_validate(src(limit=40))
    async with Services.open(settings, sources) as svc:
        out = await run_source(svc, svc.sources.get("eurlex"), since=SINCE, collect=True)
    assert out.result.status == "ok" and out.result.fetched >= 20 and out.result.invalid == 0
    assert all(p.url.startswith("https://eur-lex.europa.eu/legal-content/EN/TXT?uri=CELEX:") for p in out.posts)
    assert not any("restrictive measures" in (p.title or "").lower() for p in out.posts)
