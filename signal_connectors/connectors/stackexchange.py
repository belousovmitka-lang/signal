"""Stack Exchange network (Stack Overflow, Law, Workplace, Money, Software Recommendations, ...).

Each sources.yaml query lists sites in `communities` (API site names: "law", "workplace",
"softwarerecs") and optional `keywords`. Every site x keyword pair is one search on
/2.3/search/advanced, newest first, from `since`. No keywords means every new question.
`params.tagged` limits to tags ("excel;automation").

Quota: 300 requests/day per IP without a key, 10,000 with STACKEXCHANGE_KEY. Every request
is metered against that, and the API's `backoff` field is obeyed.

Content is CC BY-SA 4.0: the app must credit Stack Exchange and link to the question.
"""

from __future__ import annotations

import html
import logging
import os
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

from ..base import BaseConnector
from ..models import HealthStatus, RawPost, SourceQuery
from ..parsing import html_to_text
from ..privacy import hash_author
from . import register

log = logging.getLogger(__name__)

API = "https://api.stackexchange.com/2.3"
DOMAIN = "api.stackexchange.com"
PAGE_SIZE_MAX = 100
QUOTA_NO_KEY = 300
QUOTA_WITH_KEY = 10_000
LICENSE = "CC BY-SA 4.0"


class StackExchangeError(Exception):
    pass


@register
class StackExchangeConnector(BaseConnector):
    connector = "stackexchange"
    route = "own_api"
    source_type = "community"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.key = os.environ.get("STACKEXCHANGE_KEY") or None
        self.daily_quota = QUOTA_WITH_KEY if self.key else QUOTA_NO_KEY
        self.quota_remaining: int | None = None

    async def healthcheck(self) -> HealthStatus:
        status = await super().healthcheck()
        used = self.services.costs.units_used(self.name, timedelta(days=1))
        status.quota_left = self.daily_quota - used
        status.message = f"{'key' if self.key else 'no key'}: {self.daily_quota} requests/day"
        return status

    async def _call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        params = {k: v for k, v in params.items() if v not in (None, "")}
        if self.key:
            params["key"] = self.key
        with self.services.costs.metered(self.name, units=1, note=method,
                                         quota=(self.daily_quota, timedelta(days=1))):
            response = await self.http.get(f"{API}/{method}", params=params)
        data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
        if "error_id" in data or response.status_code >= 400:
            raise StackExchangeError(
                f"{method} {params.get('site')}: HTTP {response.status_code} "
                f"{data.get('error_name', '')} {data.get('error_message', '')}".strip())
        if backoff := data.get("backoff"):
            log.info("%s: API asked for %ss backoff", self.name, backoff)
            self.http.limiter.defer(DOMAIN, float(backoff))
        self.quota_remaining = data.get("quota_remaining", self.quota_remaining)
        return data

    async def fetch(self, query: SourceQuery) -> AsyncIterator[RawPost]:
        if not query.communities:
            raise ValueError("stackexchange query needs communities (API site names)")
        seen: set[str] = set()
        for site in query.communities:
            for phrase in query.keywords or [None]:
                async for post in self._search(site, phrase, query):
                    if post.id not in seen:
                        seen.add(post.id)
                        yield post

    async def _search(self, site: str, phrase: str | None, query: SourceQuery) -> AsyncIterator[RawPost]:
        page, fetched = 1, 0
        while fetched < query.limit:
            data = await self._call("search/advanced", {
                "site": site,
                "q": phrase,
                "tagged": query.params.get("tagged"),
                "fromdate": int(query.since.timestamp()) if query.since else None,
                "sort": "creation",
                "order": "desc",
                "pagesize": min(PAGE_SIZE_MAX, query.limit),
                "page": page,
                "filter": "withbody",
            })
            for item in data.get("items", []):
                post = self._to_post(item, site, phrase, query)
                if post:
                    fetched += 1
                    yield post
            if not data.get("has_more"):
                return
            page += 1

    def _to_post(self, item: dict[str, Any], site: str, phrase: str | None, query: SourceQuery) -> RawPost | None:
        owner = item.get("owner") or {}
        account = owner.get("account_id") or owner.get("user_id")
        return self.make_post(
            url=item["link"],
            title=html.unescape(item.get("title", "")) or None,
            text=html_to_text(item.get("body")),
            is_snippet=False,
            author_hash=hash_author(str(account), "stackexchange") if account else None,
            community=urlsplit(item["link"]).hostname,
            published_at=datetime.fromtimestamp(item["creation_date"], tz=timezone.utc),
            language="en",
            engagement={"score": item.get("score"), "answers": item.get("answer_count"),
                        "views": item.get("view_count")},
            industry_hint=query.industry_hint,
            query=phrase,
            raw={
                "site": site,
                "question_id": item.get("question_id"),
                "tags": item.get("tags", []),
                "is_answered": item.get("is_answered"),
                "accepted_answer_id": item.get("accepted_answer_id"),
                "closed_reason": item.get("closed_reason"),
                "license": LICENSE,
            },
        )
