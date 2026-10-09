"""NewsAPI.org /v2/everything (paid; feature-flagged off: ENABLE_NEWSAPI).

Pricing checked 2026-10-09: Developer plan is free for development only ("cannot be used in a
staging or production environment"); production needs Business ($449/mo) or Advanced
($1,749/mo), $0.0018 per extra request. Terms forbid building "a competing news database";
Signal keeps headlines internally to find problems and links out, so it doesn't offer one.
Articles carry title, description and the first 200 characters of content (is_snippet).

sources.yaml query: {keywords: ['("law firm" OR paralegal) AND software'], params: {language: en}}
Per-call cost for the meter: options.cost_per_call_usd (default 0.0018, the overage price).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

from ..base import BaseConnector
from ..models import HealthStatus, RawPost, SourceQuery
from ..parsing import parse_date
from . import register

API = "https://newsapi.org/v2/everything"
PAGE = 100


class NewsApiError(Exception):
    pass


@register
class NewsApiConnector(BaseConnector):
    connector = "newsapi"
    route = "own_api"
    source_type = "news"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.key = os.environ.get("NEWSAPI_KEY") or None
        self.cost = float(self.source.options.get("cost_per_call_usd", 0.0018))

    async def healthcheck(self) -> HealthStatus:
        status = await super().healthcheck()
        status.auth_ok = bool(self.key)
        status.ok = bool(self.key)
        status.message = None if self.key else "NEWSAPI_KEY is not set"
        return status

    async def fetch(self, query: SourceQuery) -> AsyncIterator[RawPost]:
        if not self.key:
            raise NewsApiError("NEWSAPI_KEY is not set")
        if not query.keywords:
            raise ValueError("newsapi query needs keywords")
        page, fetched = 1, 0
        while fetched < query.limit:
            params: dict[str, Any] = {"q": query.query_text, "sortBy": "publishedAt",
                                      "pageSize": min(PAGE, query.limit - fetched), "page": page,
                                      "language": query.params.get("language", "en")}
            if query.since:
                params["from"] = query.since.strftime("%Y-%m-%dT%H:%M:%S")
            if search_in := query.params.get("search_in"):
                params["searchIn"] = search_in
            with self.services.costs.metered(self.name, usd=self.cost, note="everything"):
                response = await self.http.get(API, params=params, headers={"X-Api-Key": self.key})
            data = response.json() if "json" in response.headers.get("content-type", "") else {}
            if response.status_code != 200 or data.get("status") != "ok":
                if data.get("code") == "maximumResultsReached":
                    return    # plan's result cap for this query
                raise NewsApiError(f"NewsAPI HTTP {response.status_code}: {data.get('code')} {data.get('message', '')}"[:200])
            articles = data.get("articles", [])
            for a in articles:
                post = self.make_post(
                    url=a["url"],
                    title=a.get("title"),
                    text="\n\n".join(x for x in (a.get("description"), a.get("content")) if x),
                    is_snippet=True,
                    community=(a.get("source") or {}).get("name"),
                    published_at=parse_date(a.get("publishedAt")),
                    language=params["language"],
                    industry_hint=query.industry_hint,
                    query=query.query_text,
                    raw={"source_id": (a.get("source") or {}).get("id")},
                )
                if post:
                    fetched += 1
                    yield post
            if len(articles) < params["pageSize"] or fetched >= data.get("totalResults", 0):
                return
            page += 1
