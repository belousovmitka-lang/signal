"""openFDA device adverse events (MAUDE): /device/event.json, no key (CC0 data).

MAUDE lags 5-6 weeks behind (newest date_received 2026-08-31 on 2026-10-09, data updated
weekly) and date_added can't be sorted on, so each run looks back `params.lag_days` (default
60) before the cursor, newest first; ids dedupe the overlap. Run weekly.

Stored: device generic/brand name, manufacturer (a company), event type, product problems and
the redacted event narratives (mdr_text). Not stored: any contact names, phones or addresses.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

from ..base import BaseConnector
from ..models import RawPost, SourceQuery, utcnow
from ..parsing import parse_date
from . import register

API = "https://api.fda.gov/device/event.json"
REPORT_URL = "https://www.accessdata.fda.gov/scripts/cdrh/cfdocs/cfmaude/detail.cfm?mdrfoi__id={}"
PAGE = 100
MAX_SKIP = 25_000


@register
class OpenFdaConnector(BaseConnector):
    connector = "openfda"
    route = "own_api"
    source_type = "government"
    day_granular = True
    text_dedupe = False
    filter_since = False   # reads lag_days before the cursor on purpose

    def search(self, query: SourceQuery) -> str:
        lag = timedelta(days=int(query.params.get("lag_days", 60)))
        start = (query.since or utcnow() - timedelta(days=7)) - lag
        # Open-ended range (fixed end): requests stay identical day to day, so cassettes keep matching.
        clauses = [f"date_received:[{start:%Y%m%d} TO 20991231]", "_exists_:mdr_text"]
        if extra := query.params.get("search"):
            clauses.append(f"({extra})")
        return " AND ".join(clauses)

    async def fetch(self, query: SourceQuery) -> AsyncIterator[RawPost]:
        search, skip, fetched = self.search(query), 0, 0
        while fetched < query.limit and skip <= MAX_SKIP:
            response = await self.http.get(API, params={"search": search, "sort": "date_received:desc",
                                                        "limit": PAGE, "skip": skip})
            if response.status_code == 404:     # openFDA's "No matches found!"
                return
            response.raise_for_status()
            results = response.json().get("results", [])
            for r in results:
                post = self._to_post(r, query)
                if post:
                    fetched += 1
                    yield post
                    if fetched >= query.limit:
                        return
            if len(results) < PAGE:
                return
            skip += PAGE

    def _to_post(self, r: dict[str, Any], query: SourceQuery) -> RawPost | None:
        key = r.get("mdr_report_key")
        if not key:
            return None
        device = (r.get("device") or [{}])[0]
        name = device.get("generic_name") or device.get("brand_name") or "Medical device"
        texts = [t.get("text", "").strip() for t in r.get("mdr_text") or [] if t.get("text")]
        problems = r.get("product_problems") or []
        return self.make_post(
            url=REPORT_URL.format(key),
            title=f"{r.get('event_type') or 'Report'}: {name.title()}" + (f" ({device['brand_name']})" if device.get("brand_name") else ""),
            text="\n\n".join(texts + ([f"Product problems: {', '.join(problems)}"] if problems else [])),
            is_snippet=False,
            community=device.get("manufacturer_d_name") or r.get("manufacturer_name"),
            published_at=parse_date(r.get("date_received")),
            language="en",
            industry_hint=query.industry_hint or "healthcare",
            raw={"mdr_report_key": key, "report_number": r.get("report_number"), "event_type": r.get("event_type"),
                 "product_code": device.get("device_report_product_code"), "generic_name": device.get("generic_name"),
                 "brand_name": device.get("brand_name"), "product_problems": problems,
                 "source_type": r.get("source_type"), "date_of_event": r.get("date_of_event")},
        )
