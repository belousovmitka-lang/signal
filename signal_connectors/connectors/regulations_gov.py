"""Regulations.gov API v4: public comments on federal rules (key REGULATIONS_GOV_API_KEY, api.data.gov).

/v4/comments?filter[postedDate][ge]=...&filter[agencyId]=...&sort=-postedDate lists comments;
each comment's text needs /v4/comments/{id}. Limit: 1,000 requests/hour per key. Comment titles
usually name the commenter ("Comment Submitted by ..."), so titles are rebuilt and no submitter
field is stored: not firstName/lastName, address, city, zip, email, phone or submitterRep.
Organization and category are kept (they identify businesses, not people). Form-letter
campaigns collapse through the normal text dedupe.
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

API = "https://api.regulations.gov/v4"
PAGE = 250


@register
class RegulationsGovConnector(BaseConnector):
    connector = "regulations_gov"
    route = "own_api"
    source_type = "government"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.key = os.environ.get("REGULATIONS_GOV_API_KEY") or None

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        with self.services.costs.metered(self.name, units=1, quota=(1000, timedelta(hours=1)),
                                         note=path.split("/")[1]):
            response = await self.http.get(f"{API}{path}", params=params, headers={"X-Api-Key": self.key or ""})
        if response.status_code != 200:
            raise RuntimeError(f"Regulations.gov HTTP {response.status_code}: {response.text[:160]}")
        return response.json()

    async def fetch(self, query: SourceQuery) -> AsyncIterator[RawPost]:
        if not self.key:
            raise RuntimeError("REGULATIONS_GOV_API_KEY is not set")
        params: dict[str, Any] = {"sort": "-postedDate", "page[size]": min(PAGE, max(5, query.limit))}
        if query.since:
            params["filter[postedDate][ge]"] = f"{query.since:%Y-%m-%d}"
        if agency := query.params.get("agency"):
            params["filter[agencyId]"] = agency
        if query.keywords:
            params["filter[searchTerm]"] = query.query_text
        listing = await self._get("/comments", params)
        for item in listing.get("data", [])[: query.limit]:
            detail = (await self._get(f"/comments/{item['id']}")).get("data") or {}
            post = self._to_post(item["id"], detail.get("attributes") or {}, query)
            if post:
                yield post

    def _to_post(self, cid: str, a: dict[str, Any], query: SourceQuery) -> RawPost | None:
        if a.get("withdrawn"):
            return None
        org = (a.get("organization") or "").strip() or None
        docket = a.get("docketId") or a.get("commentOnDocumentId")
        return self.make_post(
            url=f"https://www.regulations.gov/comment/{cid}",
            title=f"Public comment to {a.get('agencyId') or 'agency'}" + (f" on {docket}" if docket else "")
                  + (f" from {org}" if org else ""),
            text=html_to_text(a.get("comment") or ""),
            is_snippet=False,
            author_role=(a.get("category") or None),
            community=docket or a.get("agencyId"),
            published_at=parse_date(a.get("postedDate")),
            language="en",
            industry_hint=query.industry_hint,
            query=query.query_text,
            raw={"comment_id": cid, "agency": a.get("agencyId"), "docket": a.get("docketId"),
                 "comment_on": a.get("commentOnDocumentId"), "organization": org, "category": a.get("category"),
                 "attachments": bool(a.get("fileFormats"))},
        )
