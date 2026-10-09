"""CFPB Consumer Complaint Database API (no key; data licensed CC0).

As of 2026-10-09 the public API returns no consumer narratives (`complaint_what_happened` is
absent from every record, and the API spec no longer has has_narrative), so each record is
structured: product, issue, company, state, company response. ZIP codes are not stored (rule 4).

sources.yaml query: {params: {product: ["Mortgage"]}, keywords: ["optional search term"]}
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from ..base import BaseConnector
from ..models import RawPost, SourceQuery
from ..parsing import parse_date
from . import register

API = "https://www.consumerfinance.gov/data-research/consumer-complaints/search/api/v1/"
DETAIL = "https://www.consumerfinance.gov/data-research/consumer-complaints/search/detail/{}"
PAGE = 100
MAX_OFFSET = 10_000


@register
class CfpbConnector(BaseConnector):
    connector = "cfpb"
    route = "own_api"
    source_type = "government"
    text_dedupe = False

    def params(self, query: SourceQuery, page: int, search_after: str | None) -> list[tuple[str, Any]]:
        p: list[tuple[str, Any]] = [("size", PAGE), ("sort", "created_date_desc"),
                                    ("no_aggs", "true"), ("no_highlight", "true")]
        if page > 1 and search_after:
            # frm alone is ignored: pages come from _meta.break_points (cfpb.github.io issue #292)
            p += [("page", page), ("frm", PAGE), ("search_after", search_after)]
        if query.since:
            p.append(("date_received_min", f"{query.since:%Y-%m-%d}"))
        if query.keywords:
            p.append(("search_term", query.query_text))
        p += [("product", v) for v in query.params.get("product", [])]
        return p

    async def fetch(self, query: SourceQuery) -> AsyncIterator[RawPost]:
        page, fetched, search_after = 1, 0, None
        while fetched < query.limit and page * PAGE <= MAX_OFFSET:
            data = await self.http.get_json(API, params=self.params(query, page, search_after))
            hits = (data.get("hits") or {}).get("hits", [])
            if not hits:
                return
            for hit in hits:
                post = self._to_post(hit.get("_source") or {}, query)
                if post:
                    fetched += 1
                    yield post
                    if fetched >= query.limit:
                        return
            point = ((data.get("_meta") or {}).get("break_points") or {}).get(str(page + 1))
            if not point:
                return
            page, search_after = page + 1, f"{point[0]}_{point[1]}"

    def _to_post(self, c: dict[str, Any], query: SourceQuery) -> RawPost | None:
        cid = c.get("complaint_id")
        if not cid:
            return None
        product = " / ".join(x for x in (c.get("product"), c.get("sub_product")) if x)
        issue = " / ".join(x for x in (c.get("issue"), c.get("sub_issue")) if x)
        text = "\n".join(x for x in (c.get("complaint_what_happened"), f"Product: {product}", f"Issue: {issue}",
                                     f"Company response: {c.get('company_response') or 'n/a'}") if x)
        return self.make_post(
            url=DETAIL.format(cid),
            title=f"{c.get('issue') or 'Complaint'} ({c.get('company') or 'unknown company'})",
            text=text,
            is_snippet=False,
            community=c.get("company"),
            published_at=parse_date(c.get("date_received")),
            language="en",
            industry_hint=query.industry_hint,
            raw={"complaint_id": cid, "product": c.get("product"), "sub_product": c.get("sub_product"),
                 "issue": c.get("issue"), "sub_issue": c.get("sub_issue"), "company": c.get("company"),
                 "state": c.get("state"), "submitted_via": c.get("submitted_via"),
                 "company_response": c.get("company_response"), "timely": c.get("timely"), "tags": c.get("tags")},
        )
