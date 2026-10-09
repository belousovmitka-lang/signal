"""SAM.gov Get Opportunities API v2: US federal contract opportunities (free key SAM_GOV_API_KEY).

GET /opportunities/v2/search?postedFrom=MM/dd/yyyy&postedTo=MM/dd/yyyy&limit<=1000&offset&ptype=...
Daily limits depend on the account role and aren't published; sources.yaml caps this source at
10 requests/day (the lowest tier), which is enough because one request returns 1,000 notices.
pointOfContact (names, emails, phones) is not stored. The description is a separate keyed link,
so records carry title, agency, NAICS code, set-aside and deadline.

sources.yaml query: {params: {ptype: "o,k,p,r"}, keywords: []}  (title filter optional)
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

from ..base import BaseConnector
from ..models import RawPost, SourceQuery, utcnow
from ..parsing import parse_date
from . import register

API = "https://api.sam.gov/opportunities/v2/search"
PAGE = 1000
DEFAULT_QUOTA = 10


@register
class SamGovConnector(BaseConnector):
    connector = "sam_gov"
    route = "own_api"
    source_type = "government"
    text_dedupe = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.key = os.environ.get("SAM_GOV_API_KEY") or None
        q = self.source.quota
        self.quota = (q.units, q.window) if q else (DEFAULT_QUOTA, timedelta(days=1))

    async def fetch(self, query: SourceQuery) -> AsyncIterator[RawPost]:
        if not self.key:
            raise RuntimeError("SAM_GOV_API_KEY is not set")
        since = query.since or utcnow() - timedelta(days=1)
        params: dict[str, Any] = {"api_key": self.key, "postedFrom": f"{since:%m/%d/%Y}",
                                  "postedTo": f"{utcnow():%m/%d/%Y}", "limit": min(PAGE, query.limit), "offset": 0}
        if ptype := query.params.get("ptype"):
            params["ptype"] = ptype
        if query.keywords:
            params["title"] = query.query_text
        if ncode := query.params.get("ncode"):
            params["ncode"] = ncode
        fetched = 0
        while fetched < query.limit:
            with self.services.costs.metered(self.name, units=1, quota=self.quota, note="opportunities"):
                response = await self.http.get(API, params=params)
            if response.status_code != 200:
                raise RuntimeError(f"SAM.gov HTTP {response.status_code}: {response.text[:160]}")
            data = response.json()
            items = data.get("opportunitiesData") or []
            for o in items:
                post = self._to_post(o, query)
                if post:
                    fetched += 1
                    yield post
            params["offset"] += len(items)
            if not items or params["offset"] >= int(data.get("totalRecords") or 0):
                return

    def _to_post(self, o: dict[str, Any], query: SourceQuery) -> RawPost | None:
        if not o.get("noticeId"):
            return None
        agency = (o.get("fullParentPathName") or "").split(".")
        set_aside = o.get("typeOfSetAsideDescription")
        award = o.get("award") or {}
        return self.make_post(
            # uiLink points into the logged-in workspace; this is the public notice page.
            url=f"https://sam.gov/opp/{o['noticeId']}/view",
            title=o.get("title"),
            text=" ".join(x for x in (
                f"{o.get('type') or 'Notice'} from {' / '.join(a.title() for a in agency if a)}.",
                f"NAICS {o['naicsCode']}." if o.get("naicsCode") else "",
                f"Set-aside: {set_aside}." if set_aside else "",
                f"Responses due {o['responseDeadLine'][:10]}." if o.get("responseDeadLine") else "") if x),
            is_snippet=True,
            community=agency[0].title() if agency and agency[0] else None,
            published_at=parse_date(o.get("postedDate")),
            language="en",
            money={"award_amount": award.get("amount")} if award.get("amount") else None,
            industry_hint=query.industry_hint,
            query=query.query_text,
            raw={"notice_id": o["noticeId"], "solicitation": o.get("solicitationNumber"), "type": o.get("type"),
                 "base_type": o.get("baseType"), "naics": o.get("naicsCode"), "psc": o.get("classificationCode"),
                 "set_aside": o.get("typeOfSetAside"), "deadline": o.get("responseDeadLine"),
                 "agency_path": o.get("fullParentPathName"), "active": o.get("active")},
        )
