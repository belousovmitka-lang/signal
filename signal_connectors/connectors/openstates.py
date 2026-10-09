"""Open States API v3 (Plural Policy): US state bills (key OPENSTATES_API_KEY). Public domain data.

/bills?q=...&updated_since=YYYY-MM-DD&sort=latest_action_desc&include=abstracts, 20 per page.
Rate limits aren't published; sources.yaml paces ~9/minute and caps 400/day. A bill is stored
once (id = its Open States URL) when its latest action is newer than the cursor.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

from ..base import BaseConnector
from ..models import RawPost, SourceQuery
from ..parsing import parse_date
from . import register

API = "https://v3.openstates.org/bills"
PAGE = 20


@register
class OpenStatesConnector(BaseConnector):
    connector = "openstates"
    route = "own_api"
    source_type = "government"
    text_dedupe = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.key = os.environ.get("OPENSTATES_API_KEY") or None
        q = self.source.quota
        self.quota = (q.units, q.window) if q else (400, timedelta(days=1))

    async def fetch(self, query: SourceQuery) -> AsyncIterator[RawPost]:
        if not self.key:
            raise RuntimeError("OPENSTATES_API_KEY is not set")
        params: dict[str, Any] = {"sort": "latest_action_desc", "per_page": PAGE, "page": 1, "include": "abstracts"}
        if query.keywords:
            params["q"] = query.query_text
        if query.since:
            params["updated_since"] = f"{query.since:%Y-%m-%d}"
        if jurisdiction := query.params.get("jurisdiction"):
            params["jurisdiction"] = jurisdiction
        fetched = 0
        while fetched < query.limit:
            with self.services.costs.metered(self.name, units=1, quota=self.quota, note="bills"):
                response = await self.http.get(API, params=params, headers={"X-API-KEY": self.key}, timeout=60)
            if response.status_code != 200:
                raise RuntimeError(f"Open States HTTP {response.status_code}: {response.text[:160]}")
            data = response.json()
            for b in data.get("results", []):
                post = self._to_post(b, query)
                if post:
                    fetched += 1
                    yield post
                    if fetched >= query.limit:
                        return
            if params["page"] >= (data.get("pagination") or {}).get("max_page", 1):
                return
            params["page"] += 1

    def _to_post(self, b: dict[str, Any], query: SourceQuery) -> RawPost | None:
        if not b.get("openstates_url"):
            return None
        state = (b.get("jurisdiction") or {}).get("name")
        abstract = next((a.get("abstract") for a in b.get("abstracts") or [] if a.get("abstract")), "")
        action = b.get("latest_action_description")
        return self.make_post(
            url=b["openstates_url"],
            title=f"{state} {b.get('identifier')}: {b.get('title')}",
            text=" ".join(x for x in (abstract, f"Latest action: {action}." if action else "") if x),
            is_snippet=False,
            community=state,
            published_at=parse_date(b.get("latest_action_date")) or parse_date(b.get("updated_at")),
            language="en",
            industry_hint=query.industry_hint,
            query=query.query_text,
            raw={"bill_id": b.get("id"), "identifier": b.get("identifier"), "session": b.get("session"),
                 "classification": b.get("classification"), "subject": b.get("subject"),
                 "chamber": (b.get("from_organization") or {}).get("classification")},
        )
