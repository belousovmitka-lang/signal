"""Find URLs with a search API, then forget the search results.

Brave's API terms (3(b)(i)) forbid storing search results beyond transient storage, and
Exa's (4.2(a)) forbid copying what the API returns without written permission. So discovery
returns only a list of URLs, held in memory for the length of one fetch; nothing from a
result (title, snippet, rank, score, id) is kept. The content we store is fetched from the
original platform through its own allowed route (see connectors/search_api.py).

Exa is asked for no contents, so a search costs the base price.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Literal

from .base import HttpClient
from .costs import CostMeter
from .models import utcnow

BRAVE_WEB = "https://api.search.brave.com/res/v1/web/search"
EXA_SEARCH = "https://api.exa.ai/search"

BRAVE_USD = 0.005            # per request, Search plan (verified 2026-10-09)
EXA_USD = 0.007              # per Auto search, up to 10 results, no contents (verified 2026-10-09)
BRAVE_PAGE = 20
BRAVE_MAX_OFFSET = 9
BRAVE_MAX_QUERY = 400
EXA_MAX_BASE = 10            # results beyond 10 cost extra

Provider = Literal["brave", "exa"]


class DiscoveryError(Exception):
    pass


def site_filter(domains: list[str]) -> str:
    sites = [f"site:{d}" for d in domains]
    if len(sites) <= 1:
        return sites[0] if sites else ""
    return "(" + " OR ".join(sites) + ")"


def brave_queries(text: str, domains: list[str]) -> list[str]:
    """One query per chunk of site filters, split until each fits Brave's length limit."""
    chunks: list[list[str]] = [list(domains)] if domains else [[]]
    out: list[str] = []
    while chunks:
        chunk = chunks.pop(0)
        q = f"{site_filter(chunk)} {text}".strip()
        if len(q) > BRAVE_MAX_QUERY and len(chunk) > 1:
            mid = len(chunk) // 2
            chunks[:0] = [chunk[:mid], chunk[mid:]]
        else:
            out.append(q[:BRAVE_MAX_QUERY])
    return out


class Discovery:
    def __init__(self, http: HttpClient, costs: CostMeter, source: str) -> None:
        self.http = http
        self.costs = costs
        self.source = source
        self.brave_key = os.environ.get("BRAVE_API_KEY") or None
        self.exa_key = os.environ.get("EXA_API_KEY") or None

    async def urls(self, provider: Provider, text: str, domains: list[str], since: datetime | None,
                   limit: int, lang: str = "en") -> list[str]:
        if provider == "brave":
            found = await self._brave(text, domains, since, limit, lang)
        elif provider == "exa":
            found = await self._exa(text, domains, since, limit)
        else:
            raise DiscoveryError(f"unknown provider {provider!r}")
        return list(dict.fromkeys(found))

    async def _brave(self, text: str, domains: list[str], since: datetime | None, limit: int,
                     lang: str) -> list[str]:
        if not self.brave_key:
            raise DiscoveryError("BRAVE_API_KEY is not set")
        now = utcnow()
        freshness = f"{since:%Y-%m-%d}to{now:%Y-%m-%d}" if since else "pw"
        headers = {"X-Subscription-Token": self.brave_key, "Accept": "application/json"}
        urls: list[str] = []
        for q in brave_queries(text, domains):
            offset, found = 0, 0
            while found < limit and offset <= BRAVE_MAX_OFFSET:
                with self.costs.metered(self.source, usd=BRAVE_USD, provider="brave", note="discovery"):
                    response = await self.http.get(BRAVE_WEB, headers=headers, params={
                        "q": q, "count": BRAVE_PAGE, "offset": offset, "freshness": freshness,
                        "search_lang": lang, "result_filter": "web",
                    })
                if response.status_code >= 400:
                    raise DiscoveryError(f"Brave HTTP {response.status_code}")
                data = response.json()
                results = data.get("web", {}).get("results", [])
                page = [r["url"] for r in results if r.get("url")]
                urls += page
                found += len(page)
                if not data.get("query", {}).get("more_results_available") or len(results) < BRAVE_PAGE:
                    break
                offset += 1
        return urls

    async def _exa(self, text: str, domains: list[str], since: datetime | None, limit: int) -> list[str]:
        if not self.exa_key:
            raise DiscoveryError("EXA_API_KEY is not set")
        body: dict[str, object] = {"query": text, "type": "auto", "numResults": min(limit, EXA_MAX_BASE)}
        if domains:
            body["includeDomains"] = domains
        if since:
            body["startPublishedDate"] = since.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        with self.costs.metered(self.source, usd=EXA_USD, provider="exa", note="discovery") as charge:
            response = await self.http.request("POST", EXA_SEARCH, json=body, headers={"x-api-key": self.exa_key})
            is_json = response.headers.get("content-type", "").startswith("application/json")
            data = response.json() if is_json else {}
            if total := (data.get("costDollars") or {}).get("total"):
                charge.usd = float(total)
        if response.status_code >= 400:
            raise DiscoveryError(f"Exa HTTP {response.status_code}: {data.get('error', '')}"[:200])
        return [r["url"] for r in data.get("results", []) if r.get("url")]
