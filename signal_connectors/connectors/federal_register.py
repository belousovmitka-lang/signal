"""Federal Register API v1: US rules, proposed rules and notices (no key; public domain).

sources.yaml query: {params: {types: [RULE, PRORULE]}, keywords: ["small business"]}
`types` filters document types (RULE, PRORULE, NOTICE, PRESDOCU); keywords are optional.
Publication dates have no time of day, so the runner compares dates.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from ..base import BaseConnector
from ..models import RawPost, SourceQuery
from ..parsing import parse_date
from . import register

API = "https://www.federalregister.gov/api/v1/documents.json"
FIELDS = ["title", "abstract", "html_url", "publication_date", "type", "agencies", "document_number",
          "comments_close_on", "docket_ids", "regulation_id_numbers", "significant", "action"]
PAGE = 100


@register
class FederalRegisterConnector(BaseConnector):
    connector = "federal_register"
    route = "own_api"
    source_type = "government"
    day_granular = True

    def params(self, query: SourceQuery) -> list[tuple[str, Any]]:
        p: list[tuple[str, Any]] = [("per_page", min(PAGE, query.limit)), ("order", "newest")]
        p += [("fields[]", f) for f in FIELDS]
        p += [("conditions[type][]", t) for t in query.params.get("types", ["RULE", "PRORULE"])]
        if query.keywords:
            p.append(("conditions[term]", " | ".join(f'"{k}"' if " " in k else k for k in query.keywords)))
        if query.since:
            p.append(("conditions[publication_date][gte]", f"{query.since:%Y-%m-%d}"))
        return p

    async def fetch(self, query: SourceQuery) -> AsyncIterator[RawPost]:
        url: str | None = API
        params: list[tuple[str, Any]] | None = self.params(query)
        fetched = 0
        while url and fetched < query.limit:
            data = await self.http.get_json(url, params=params)
            for doc in data.get("results", []):
                post = self._to_post(doc, query)
                if post:
                    fetched += 1
                    yield post
                    if fetched >= query.limit:
                        return
            url, params = data.get("next_page_url"), None

    def _to_post(self, doc: dict[str, Any], query: SourceQuery) -> RawPost | None:
        agencies = [a.get("name") for a in doc.get("agencies") or [] if a.get("name")]
        return self.make_post(
            url=doc["html_url"],
            title=doc.get("title"),
            text=(doc.get("abstract") or "").strip(),
            is_snippet=False,
            community=", ".join(agencies[:3]) or None,
            published_at=parse_date(doc.get("publication_date")),
            language="en",
            industry_hint=query.industry_hint,
            query=query.query_text,
            raw={"document_number": doc.get("document_number"), "type": doc.get("type"), "action": doc.get("action"),
                 "comments_close_on": doc.get("comments_close_on"), "docket_ids": doc.get("docket_ids"),
                 "rin": doc.get("regulation_id_numbers"), "significant": doc.get("significant")},
        )
