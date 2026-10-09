"""EU TED (Tenders Electronic Daily) API v3: public procurement notices, no key needed.

sources.yaml query: {params: {cpv: ["72000000", "48000000"]}} (CPV codes; IT services and
software by default) plus optional `params.query`, an extra TED expert-query clause.
Reuse is authorised under Commission Decision 2011/833/EU with acknowledgement of the source.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from ..base import BaseConnector
from ..models import RawPost, SourceQuery
from ..parsing import parse_date
from . import register

API = "https://api.ted.europa.eu/v3/notices/search"
NOTICE_URL = "https://ted.europa.eu/en/notice/-/detail/{}"
FIELDS = ["publication-number", "notice-title", "buyer-name", "buyer-country", "publication-date",
          "estimated-value-lot", "estimated-value-cur-lot", "notice-type", "classification-cpv", "description-lot"]
PAGE = 100
DEFAULT_CPV = ["72000000", "48000000"]


def pick(value: Any, lang: str = "eng") -> str | None:
    """TED multilingual fields: {"eng": "..."} or {"eng": ["..."]}; English first, else any."""
    if isinstance(value, dict):
        v = value.get(lang) or next(iter(value.values()), None)
        return pick(v)
    if isinstance(value, list):
        return str(value[0]) if value else None
    return value


@register
class TedConnector(BaseConnector):
    connector = "ted"
    route = "own_api"
    source_type = "government"
    day_granular = True   # TED publication dates have no time of day
    text_dedupe = False

    def expert_query(self, query: SourceQuery) -> str:
        cpv = query.params.get("cpv") or DEFAULT_CPV
        parts = [f"(classification-cpv IN ({' '.join(cpv)}))"]
        if extra := query.params.get("query"):
            parts.append(f"({extra})")
        if query.since:
            parts.append(f"publication-date>={query.since:%Y%m%d}")
        return " AND ".join(parts) + " SORT BY publication-date DESC"

    async def fetch(self, query: SourceQuery) -> AsyncIterator[RawPost]:
        q = self.expert_query(query)
        fetched, page = 0, 1
        while fetched < query.limit:
            response = await self.http.request("POST", API, json={
                "query": q, "fields": FIELDS, "limit": min(PAGE, query.limit - fetched), "page": page,
                "scope": "ACTIVE" if query.params.get("active_only") else "ALL"})
            if response.status_code != 200:
                raise RuntimeError(f"TED HTTP {response.status_code}: {response.text[:200]}")
            notices = response.json().get("notices", [])
            for n in notices:
                post = self._to_post(n, query)
                if post:
                    fetched += 1
                    yield post
            if len(notices) < PAGE:
                return
            page += 1

    def _to_post(self, n: dict[str, Any], query: SourceQuery) -> RawPost | None:
        number = n.get("publication-number")
        value, currency = pick(n.get("estimated-value-lot")), pick(n.get("estimated-value-cur-lot"))
        money = None
        if value:
            try:
                money = {"estimated_value": float(value), "currency": currency}
            except ValueError:
                pass
        return self.make_post(
            url=NOTICE_URL.format(number),
            title=pick(n.get("notice-title")),
            text=pick(n.get("description-lot")) or "",
            is_snippet=False,
            community=pick(n.get("buyer-name")),
            published_at=parse_date((pick(n.get("publication-date")) or "")[:10]),
            language="en" if isinstance(n.get("notice-title"), dict) and "eng" in n["notice-title"] else None,
            money=money,
            industry_hint=query.industry_hint,
            raw={"publication_number": number, "notice_type": n.get("notice-type"),
                 "cpv": sorted(set(n.get("classification-cpv") or [])), "buyer_country": pick(n.get("buyer-country"))},
        )
