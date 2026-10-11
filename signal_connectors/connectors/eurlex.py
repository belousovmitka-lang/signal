"""EU legislation from the Publications Office CELLAR SPARQL endpoint (no key).

eur-lex.europa.eu answers bots with an HTTP 202 challenge (never bypassed), so this reads the
official SPARQL endpoint publications.europa.eu/webapi/rdf/sparql instead: new regulations,
directives and decisions with CELEX id, date and English title. Records link to the EUR-Lex page
for people to open. Reuse is authorised (Commission Decision 2011/833/EU).
Sanctions acts ("restrictive measures") are excluded by default: they dominate volume and carry
no business signal (params.exclude_title_terms).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

from ..base import BaseConnector
from ..models import RawPost, SourceQuery, utcnow
from ..parsing import parse_date
from . import register

ENDPOINT = "https://publications.europa.eu/webapi/rdf/sparql"
TYPE_URI = "http://publications.europa.eu/resource/authority/resource-type/{}"
DEFAULT_TYPES = ["REG", "REG_IMPL", "REG_DEL", "DIR", "DIR_IMPL", "DIR_DEL", "DEC"]
DEFAULT_EXCLUDE = ["restrictive measures"]
PAGE = 200

QUERY = """PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
SELECT DISTINCT ?celex ?date ?title ?type WHERE {{
  ?work cdm:resource_legal_id_celex ?celex ;
        cdm:work_date_document ?date ;
        cdm:work_has_resource-type ?type .
  FILTER(?date >= "{since}"^^xsd:date)
  FILTER(?type IN ({types}))
  ?expr cdm:expression_belongs_to_work ?work ;
        cdm:expression_uses_language <http://publications.europa.eu/resource/authority/language/ENG> ;
        cdm:expression_title ?title .
}} ORDER BY DESC(?date) ?celex LIMIT {limit} OFFSET {offset}"""


@register
class EurLexConnector(BaseConnector):
    connector = "eurlex"
    route = "own_api"
    source_type = "government"
    day_granular = True
    text_dedupe = False

    def sparql(self, query: SourceQuery, offset: int) -> str:
        since = (query.since or utcnow() - timedelta(days=7)).date()
        types = ", ".join(f"<{TYPE_URI.format(t)}>" for t in query.params.get("types", DEFAULT_TYPES))
        return QUERY.format(since=since.isoformat(), types=types, limit=PAGE, offset=offset)

    async def fetch(self, query: SourceQuery) -> AsyncIterator[RawPost]:
        exclude = [t.lower() for t in query.params.get("exclude_title_terms", DEFAULT_EXCLUDE)]
        offset, fetched = 0, 0
        while fetched < query.limit:
            await self.services.robots.require(ENDPOINT)
            response = await self.http.request("POST", ENDPOINT, data={"query": self.sparql(query, offset)},
                                               headers={"Accept": "application/sparql-results+json"}, timeout=90)
            response.raise_for_status()
            rows = response.json()["results"]["bindings"]
            seen: set[str] = set()
            for b in rows:
                celex = b["celex"]["value"]
                title = b["title"]["value"]
                if celex in seen or any(t in title.lower() for t in exclude):
                    continue
                seen.add(celex)
                post = self._to_post(celex, b, query)
                if post:
                    fetched += 1
                    yield post
                    if fetched >= query.limit:
                        return
            if len(rows) < PAGE:
                return
            offset += PAGE

    def _to_post(self, celex: str, b: dict[str, Any], query: SourceQuery) -> RawPost | None:
        kind = b["type"]["value"].rsplit("/", 1)[-1]
        title = " ".join(b["title"]["value"].split())
        return self.make_post(
            url=f"https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:{celex}",
            title=title[:300],
            text=title if len(title) > 300 else "",
            is_snippet=True,
            community="EUR-Lex",
            published_at=parse_date(b["date"]["value"]),
            language="en",
            industry_hint=query.industry_hint,
            raw={"celex": celex, "type": kind},
        )
