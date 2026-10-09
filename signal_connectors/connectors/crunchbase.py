"""Crunchbase API v4 funding rounds (paid enterprise license; flag off: ENABLE_CRUNCHBASE).

POST https://api.crunchbase.com/v4/data/searches/funding_rounds?user_key=... with field_ids,
AND-ed query predicates and order; pages with after_id = last uuid (limit up to 1000).
What may be stored, and for how long, comes from the license: confirm it before enabling.

sources.yaml query: {params: {categories: ["Legal Tech", "LegalTech"]}, industry_hint: legal}
(categories are matched client-side on funded_organization_categories; none = all rounds).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

from ..base import BaseConnector
from ..models import HealthStatus, RawPost, SourceQuery
from ..parsing import parse_date
from . import register

API = "https://api.crunchbase.com/v4/data/searches/funding_rounds"
ROUND_URL = "https://www.crunchbase.com/funding_round/{}"
FIELDS = ["identifier", "announced_on", "money_raised", "investment_type", "funded_organization_identifier",
          "funded_organization_categories", "short_description", "num_investors"]
PAGE = 1000


@register
class CrunchbaseConnector(BaseConnector):
    connector = "crunchbase"
    route = "own_api"
    source_type = "startup"
    day_granular = True
    text_dedupe = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.key = os.environ.get("CRUNCHBASE_KEY") or None

    async def healthcheck(self) -> HealthStatus:
        status = await super().healthcheck()
        status.auth_ok = status.ok = bool(self.key)
        status.message = None if self.key else "CRUNCHBASE_KEY is not set"
        return status

    def body(self, query: SourceQuery, after_id: str | None) -> dict[str, Any]:
        predicates = []
        if query.since:
            predicates.append({"type": "predicate", "field_id": "announced_on", "operator_id": "gte",
                               "values": [f"{query.since:%Y-%m-%d}"]})
        body: dict[str, Any] = {"field_ids": FIELDS, "query": predicates, "limit": min(PAGE, query.limit),
                                "order": [{"field_id": "announced_on", "sort": "desc"}]}
        if after_id:
            body["after_id"] = after_id
        return body

    async def fetch(self, query: SourceQuery) -> AsyncIterator[RawPost]:
        if not self.key:
            raise RuntimeError("CRUNCHBASE_KEY is not set")
        wanted = {c.lower() for c in query.params.get("categories", [])}
        after_id, fetched = None, 0
        while fetched < query.limit:
            with self.services.costs.metered(self.name, units=1, note="funding_rounds"):
                response = await self.http.request("POST", API, params={"user_key": self.key},
                                                   json=self.body(query, after_id))
            if response.status_code != 200:
                raise RuntimeError(f"Crunchbase HTTP {response.status_code}: {response.text[:160]}")
            entities = response.json().get("entities", [])
            for e in entities:
                post = self._to_post(e, wanted, query)
                if post:
                    fetched += 1
                    yield post
            if len(entities) < self.body(query, None)["limit"]:
                return
            after_id = entities[-1]["uuid"]

    def _to_post(self, e: dict[str, Any], wanted: set[str], query: SourceQuery) -> RawPost | None:
        p = e.get("properties") or {}
        categories = [c.get("value") for c in p.get("funded_organization_categories") or [] if c.get("value")]
        if wanted and not wanted & {c.lower() for c in categories}:
            return None
        org = (p.get("funded_organization_identifier") or {}).get("value")
        ident = p.get("identifier") or {}
        money = p.get("money_raised") or {}
        return self.make_post(
            url=ROUND_URL.format(ident.get("permalink") or e["uuid"]),
            title=ident.get("value") or f"{p.get('investment_type')} - {org}",
            text="\n".join(x for x in (p.get("short_description"),
                                        f"Categories: {', '.join(categories)}" if categories else "") if x),
            is_snippet=False,
            community=org,
            published_at=parse_date(p.get("announced_on")),
            language="en",
            money={"amount": money.get("value"), "currency": money.get("currency"), "usd": money.get("value_usd")}
                  if money else None,
            industry_hint=query.industry_hint,
            raw={"uuid": e["uuid"], "investment_type": p.get("investment_type"), "categories": categories,
                 "num_investors": p.get("num_investors"),
                 "org_permalink": (p.get("funded_organization_identifier") or {}).get("permalink")},
        )
