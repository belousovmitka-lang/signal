"""USPTO patents granted, via the Open Data Portal (ODP) Patent File Wrapper search (key USPTO_API_KEY).

PatentsView's API was retired into the ODP in 2026 (search.patentsview.org is gone). The ODP search is
`POST api.uspto.gov/api/v1/patent/applications/search` with the key in an `X-API-KEY` header and a JSON
body: q (boolean query), filters [{name, value: [...]}], rangeFilters [{field, valueFrom, valueTo}],
sort [{field, order}], fields, pagination {offset, limit} (schema per the Microsoft Power Platform
connector reference for this API, read 2026-10-29).
Records come back in `patentFileWrapperDataBag`, each with `applicationNumberText` and
`applicationMetaData` (inventionTitle, filingDate, grantDate, patentNumber, applicationTypeCode, ...).
Field names checked 2026-10-28 against the open-source uspto_odp client; the key needs a USPTO.gov
account with MFA (data.uspto.gov/apis/getting-started), not yet issued, so this hasn't run live.

One post per newly granted patent whose title matches an industry's keywords. Stored: title, patent
number, dates, type, CPC codes. Not stored: inventors, examiners, correspondence addresses. US patent
data is public domain.

sources.yaml query: {industry_hint: dental, keywords: [dental, orthodontic], limit: 50}
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from typing import Any

from ..base import BaseConnector
from ..models import RawPost, SourceQuery
from ..parsing import parse_date
from . import register

API = "https://api.uspto.gov/api/v1/patent/applications/search"
LINK = "https://image-ppubs.uspto.gov/dirsearch-public/print/downloadPdf/{}"      # the granted patent's PDF
PAGE = 100
LOOKBACK = timedelta(days=14)
FIELDS = ["applicationNumberText", "applicationMetaData.inventionTitle", "applicationMetaData.patentNumber",
          "applicationMetaData.grantDate", "applicationMetaData.filingDate", "applicationMetaData.applicationTypeCode",
          "applicationMetaData.cpcClassificationBag"]


def title_query(keywords: list[str]) -> str:
    terms = [f'"{k}"' if " " in k else k for k in (k.strip().replace('"', "") for k in keywords) if k]
    return f"applicationMetaData.inventionTitle:({' OR '.join(terms)})" if terms else "*:*"


@register
class PatentsConnector(BaseConnector):
    connector = "patents"
    route = "own_api"
    source_type = "research"
    default_cadence = "weekly"
    day_granular = True

    async def fetch(self, query: SourceQuery) -> AsyncIterator[RawPost]:
        key = os.environ.get("USPTO_API_KEY")
        if not key:
            raise RuntimeError("USPTO_API_KEY is not set")
        now = datetime.now(timezone.utc)
        since = query.since or now - LOOKBACK
        body: dict[str, Any] = {
            "q": title_query(query.keywords),
            "filters": [{"name": "applicationMetaData.applicationTypeCode", "value": ["UTL"]}],
            "rangeFilters": [{"field": "applicationMetaData.grantDate", "valueFrom": f"{since:%Y-%m-%d}",
                              "valueTo": f"{now:%Y-%m-%d}"}],
            "sort": [{"field": "applicationMetaData.grantDate", "order": "desc"}],
            "fields": FIELDS,
        }
        offset = fetched = 0
        while fetched < query.limit:
            response = await self.http.request("POST", API, headers={"X-API-KEY": key},
                                               json={**body, "pagination": {"offset": offset, "limit": PAGE}})
            if response.status_code == 404:          # ODP answers 404 when nothing matches
                return
            response.raise_for_status()
            bag = response.json().get("patentFileWrapperDataBag") or []
            for record in bag:
                if (post := self._to_post(record, query)) and fetched < query.limit:
                    fetched += 1
                    yield post
            if len(bag) < PAGE:
                return
            offset += PAGE

    def _to_post(self, record: dict[str, Any], query: SourceQuery) -> RawPost | None:
        meta = record.get("applicationMetaData") or {}
        number, title = meta.get("patentNumber"), (meta.get("inventionTitle") or "").strip()
        if not number or not title:
            return None
        cpc = [c.strip() for c in meta.get("cpcClassificationBag") or [] if isinstance(c, str)][:10]
        text = f"US patent {number} granted {meta.get('grantDate')}: {title}." + (
            f" CPC: {', '.join(cpc)}." if cpc else "") + f" Filed {meta.get('filingDate')}."
        return self.make_post(
            url=LINK.format(number),
            title=title.title() if title.isupper() else title,
            text=text,
            is_snippet=False,
            community="USPTO grants",
            published_at=parse_date(meta.get("grantDate")),
            language="en",
            industry_hint=query.industry_hint,
            query=query.query_text,
            raw={"patent_number": number, "application_number": record.get("applicationNumberText"),
                 "grant_date": meta.get("grantDate"), "filing_date": meta.get("filingDate"),
                 "type": meta.get("applicationTypeCode"), "cpc": cpc},
        )
