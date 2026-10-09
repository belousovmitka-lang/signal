"""CourtListener REST API v4 search (Free Law Project; token COURTLISTENER_TOKEN).

/api/rest/v4/search/?type=o&q=...&order_by=dateFiled desc&filed_after=YYYY-MM-DD returns court
opinions (20 per page) with case name, court, docket number and an opinion snippet.
Free tier limits (2026-10-09): 5 requests/minute, 50/hour, 125/day; sources.yaml spaces requests
13 seconds apart and caps the source at 100/day. Commercial agreements exist: confirm with Free
Law Project before launch. Not stored: attorney, judge and panel fields.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

from ..base import BaseConnector
from ..models import RawPost, SourceQuery
from ..parsing import html_to_text, parse_date
from . import register

API = "https://www.courtlistener.com/api/rest/v4/search/"
SITE = "https://www.courtlistener.com"


@register
class CourtListenerConnector(BaseConnector):
    connector = "courtlistener"
    route = "own_api"
    source_type = "government"
    day_granular = True
    text_dedupe = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.token = os.environ.get("COURTLISTENER_TOKEN") or None
        q = self.source.quota
        self.quota = (q.units, q.window) if q else (100, timedelta(days=1))

    async def fetch(self, query: SourceQuery) -> AsyncIterator[RawPost]:
        if not self.token:
            raise RuntimeError("COURTLISTENER_TOKEN is not set")
        if not query.keywords:
            raise ValueError("courtlistener query needs keywords")
        params: dict[str, Any] | None = {"q": query.query_text, "type": query.params.get("type", "o"),
                                         "order_by": "dateFiled desc"}
        if query.since:
            params["filed_after"] = f"{query.since:%Y-%m-%d}"
        url: str | None = API
        fetched = 0
        while url and fetched < query.limit:
            with self.services.costs.metered(self.name, units=1, quota=self.quota, note="search"):
                response = await self.http.get(url, params=params, headers={"Authorization": f"Token {self.token}"})
            if response.status_code != 200:
                raise RuntimeError(f"CourtListener HTTP {response.status_code}: {response.text[:160]}")
            data = response.json()
            for r in data.get("results", []):
                post = self._to_post(r, query)
                if post:
                    fetched += 1
                    yield post
                    if fetched >= query.limit:
                        return
            url, params = data.get("next"), None

    def _to_post(self, r: dict[str, Any], query: SourceQuery) -> RawPost | None:
        if not r.get("absolute_url"):
            return None
        snippet = " ".join(html_to_text(o.get("snippet")) for o in r.get("opinions") or [] if o.get("snippet"))
        return self.make_post(
            url=SITE + r["absolute_url"],
            title=r.get("caseName"),
            text=" ".join(snippet.split())[:2000],
            is_snippet=True,
            community=r.get("court"),
            published_at=parse_date(r.get("dateFiled")),
            language="en",
            industry_hint=query.industry_hint,
            query=query.query_text,
            raw={"cluster_id": r.get("cluster_id"), "docket_number": r.get("docketNumber"),
                 "court_id": r.get("court_id"), "status": r.get("status"), "suit_nature": r.get("suitNature"),
                 "cite_count": r.get("citeCount")},
        )
