"""GDELT DOC 2.0 API: worldwide news articles matching a query (titles and links only).

GDELT asks for at most one request every 5 seconds; in practice it 429s well below that, so
sources.yaml spaces requests 20 seconds apart, and a 429 that survives one retry stops the run. Data is free for
commercial use; the app must cite the GDELT Project with a link (gdeltproject.org/about.html).

sources.yaml query: {keywords: ['("law firm" OR paralegal) (software OR automation)'], industry_hint: legal}
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from ..base import BaseConnector
from ..costs import QuotaExceeded
from ..models import RawPost, SourceQuery
from . import register

API = "https://api.gdeltproject.org/api/v2/doc/doc"
MAX_RECORDS = 250
LANGS = {"english": "en", "spanish": "es", "french": "fr", "german": "de", "portuguese": "pt", "italian": "it"}


def parse_seendate(value: str | None) -> datetime | None:
    try:
        return datetime.strptime(value or "", "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


@register
class GdeltConnector(BaseConnector):
    connector = "gdelt"
    route = "own_api"
    source_type = "news"

    async def fetch(self, query: SourceQuery) -> AsyncIterator[RawPost]:
        if not query.keywords:
            raise ValueError("gdelt query needs keywords")
        lang = query.params.get("sourcelang", "english")
        params: dict[str, Any] = {
            "query": f"{query.query_text} sourcelang:{lang}",
            "mode": "artlist", "format": "json", "sort": "DateDesc",
            "maxrecords": min(query.limit, MAX_RECORDS),
        }
        if query.since:
            params["startdatetime"] = query.since.strftime("%Y%m%d%H%M%S")
        else:
            params["timespan"] = "1w"
        response = await self.http.get(API, params=params, attempts=2)
        if response.status_code == 429:
            # Stop the whole run rather than keep knocking; the next run resumes from the cursors.
            raise QuotaExceeded("GDELT rate limit (429); stopping this run")
        if response.status_code != 200 or "json" not in response.headers.get("content-type", ""):
            raise RuntimeError(f"GDELT HTTP {response.status_code}: {response.text[:160]}")
        for article in response.json().get("articles", []):
            post = self.make_post(
                url=article["url"],
                title=article.get("title"),
                text="",
                is_snippet=True,
                community=article.get("domain"),
                published_at=parse_seendate(article.get("seendate")),
                language=LANGS.get((article.get("language") or "").lower()),
                industry_hint=query.industry_hint,
                query=query.query_text,
                raw={"domain": article.get("domain"), "sourcecountry": article.get("sourcecountry"),
                     "attribution": "GDELT Project (gdeltproject.org)"},
            )
            if post:
                yield post
