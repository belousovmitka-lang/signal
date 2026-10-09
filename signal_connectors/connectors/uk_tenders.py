"""UK public procurement: Contracts Finder and Find a Tender (OCDS APIs, no key needed).

sources.yaml query: {params: {service: contracts_finder | find_a_tender, cpv_prefixes: ["72", "48"],
tags: [tender]}}. Releases are kept when their tag matches (default: open tenders) and, if
cpv_prefixes is set, their CPV code starts with one of them. Open Government Licence v3.0.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from ..base import BaseConnector
from ..models import RawPost, SourceQuery
from ..parsing import parse_date
from . import register

SERVICES = {
    "contracts_finder": {
        "api": "https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search",
        "since_param": "publishedFrom", "size_param": "size", "since_fmt": "%Y-%m-%dT%H:%M:%SZ",
        "notice": "https://www.contractsfinder.service.gov.uk/notice/{}",
    },
    "find_a_tender": {
        "api": "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages",
        "since_param": "updatedFrom", "size_param": "limit", "since_fmt": "%Y-%m-%dT%H:%M:%S",
        "notice": "https://www.find-tender.service.gov.uk/Notice/{}",
    },
}
PAGE = 100
MAX_PAGES = 30


@register
class UkTendersConnector(BaseConnector):
    connector = "uk_tenders"
    route = "own_api"
    source_type = "government"
    text_dedupe = False

    async def fetch(self, query: SourceQuery) -> AsyncIterator[RawPost]:
        name = query.params.get("service", "contracts_finder")
        svc = SERVICES[name]
        params: dict[str, Any] | None = {svc["size_param"]: PAGE}
        if query.since:
            params[svc["since_param"]] = query.since.strftime(svc["since_fmt"])
        url: str | None = svc["api"]
        kept = 0
        for _ in range(MAX_PAGES):
            if not url or kept >= query.limit:
                return
            response = await self.http.get(url, params=params)
            if response.status_code != 200:
                raise RuntimeError(f"{name} HTTP {response.status_code}: {response.text[:160]}")
            data = response.json()
            for release in data.get("releases", []):
                post = self._to_post(release, name, svc, query)
                if post:
                    kept += 1
                    yield post
                    if kept >= query.limit:
                        return
            url, params = (data.get("links") or {}).get("next"), None

    def _to_post(self, release: dict[str, Any], name: str, svc: dict[str, str], query: SourceQuery) -> RawPost | None:
        tags = set(release.get("tag") or [])
        if not tags & set(query.params.get("tags", ["tender"])):
            return None
        tender = release.get("tender") or {}
        cpv = (tender.get("classification") or {}).get("id") or ""
        prefixes = query.params.get("cpv_prefixes")
        if prefixes and not cpv.startswith(tuple(prefixes)):
            return None
        notice_id = release["id"][:36] if name == "contracts_finder" else release["id"]
        value = tender.get("value") or {}
        return self.make_post(
            source=f"uk_tenders:{name}",
            url=svc["notice"].format(notice_id),
            title=tender.get("title"),
            text=(tender.get("description") or "").strip(),
            is_snippet=False,
            community=(release.get("buyer") or {}).get("name"),
            published_at=parse_date(release.get("date")),
            language="en",
            money={"amount": value["amount"], "currency": value.get("currency")} if value.get("amount") else None,
            industry_hint=query.industry_hint,
            raw={"ocid": release.get("ocid"), "tags": sorted(tags), "cpv": cpv,
                 "cpv_description": (tender.get("classification") or {}).get("description"),
                 "status": tender.get("status"),
                 "deadline": (tender.get("tenderPeriod") or {}).get("endDate")},
        )
