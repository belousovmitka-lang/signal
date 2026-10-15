"""LegiScan Pull API: state and US Congress bills by full-text search (key LEGISCAN_API_KEY). CC BY 4.0.

Per query (one industry's keywords): one getSearchRaw call (state ALL, current sessions) lists every
matching bill as {bill_id, relevance, change_hash}, up to 2,000. Bills not seen before, with relevance
>= `min_relevance`, are read with getBill (one query each) and stored once. The first run of a query
records all matches as seen and reads only the `baseline` most relevant, so a new query never spends
hundreds of calls at once. Change hashes of known bills are kept for later status tracking.

Free public tier: 10,000 queries/month; sources.yaml caps 300/day. Search syntax: plain keywords
(an AND inside the query is taken as a word). Sponsors' names are not stored. CC BY 4.0: the app must
credit LegiScan and link to the bill (`url`); `raw.license` says so.

Access (2026-10-11): the key was granted for this use case; api.legiscan.com/robots.txt redirects to
a Cloudflare challenge on legiscan.com (403, RFC 9309: allow). The website and the API manual PDF
answer bots with that challenge, so docs were cross-checked from Microsoft's LegiScan connector reference.
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

API = "https://api.legiscan.com/"
STATUS = {1: "Introduced", 2: "Engrossed", 3: "Enrolled", 4: "Passed", 5: "Vetoed", 6: "Failed"}


class LegiScanError(RuntimeError):
    pass


@register
class LegiScanConnector(BaseConnector):
    connector = "legiscan"
    route = "own_api"
    source_type = "government"
    text_dedupe = False      # companion bills share descriptions
    filter_since = False     # a newly matched bill may have an older last action; ids dedupe

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.key = os.environ.get("LEGISCAN_API_KEY") or None
        q = self.source.quota
        self.quota = (q.units, q.window) if q else (300, timedelta(days=1))
        self.min_relevance = int(self.source.options.get("min_relevance", 60))
        self.baseline = int(self.source.options.get("baseline", 10))

    async def _call(self, op: str, note: str, **params: Any) -> dict[str, Any]:
        with self.services.costs.metered(self.name, units=1, quota=self.quota, note=op):
            response = await self.http.get(API, params={"key": self.key, "op": op, **params}, timeout=60)
        if response.status_code != 200:
            raise LegiScanError(f"LegiScan {op} HTTP {response.status_code}")
        data = response.json()
        if data.get("status") != "OK":
            raise LegiScanError(f"LegiScan {op} {note}: {str(data.get('alert') or data)[:160]}")
        return data

    async def fetch(self, query: SourceQuery) -> AsyncIterator[RawPost]:
        if not self.key:
            raise LegiScanError("LEGISCAN_API_KEY is not set")
        if not query.keywords:
            raise LegiScanError("legiscan query needs keywords")
        await self.services.robots.require(API)
        data = await self._call("getSearchRaw", query.query_text, state=query.params.get("state", "ALL"),
                                query=query.query_text, year=query.params.get("year", 2))
        results = data["searchresult"].get("results") or []
        hashes = {str(r["bill_id"]): r.get("change_hash") for r in results if r.get("bill_id")}
        store = self.services.store
        seen = store.seen_hashes(self.name, list(hashes))
        relevant = [str(r["bill_id"]) for r in results if (r.get("relevance") or 0) >= self.min_relevance]
        if not seen:                                  # first run of this query: baseline only
            wanted = relevant[: min(self.baseline, query.limit)]
            store.mark_seen(self.name, {b: h for b, h in hashes.items() if b not in wanted}, utcnow())
        else:
            wanted = [b for b in relevant if b not in seen][: query.limit]
            changed = {b: h for b, h in hashes.items() if b in seen and seen[b] != h}
            if changed:
                store.mark_seen(self.name, changed, utcnow())
        for bill_id in wanted:
            bill = (await self._call("getBill", bill_id, id=bill_id))["bill"]
            post = self._to_post(bill, query)
            store.mark_seen(self.name, {bill_id: bill.get("change_hash") or hashes.get(bill_id)}, utcnow())
            if post:
                yield post

    def _to_post(self, b: dict[str, Any], query: SourceQuery) -> RawPost | None:
        if not b.get("url"):
            return None
        history = b.get("history") or []
        last = history[-1] if history else {}
        status = STATUS.get(b.get("status") or 0, "Introduced")
        description = (b.get("description") or "").strip()
        action = f"Status: {status}. Latest action ({last.get('date')}): {last.get('action')}." if last else f"Status: {status}."
        subjects = [s.get("subject_name") for s in b.get("subjects") or [] if s.get("subject_name")]
        return self.make_post(
            url=b["url"],
            title=f"{b.get('state')} {b.get('bill_number')}: {b.get('title')}",
            text=" ".join(x for x in (description if description != b.get("title") else "", action) if x),
            is_snippet=False,
            community=b.get("state"),
            published_at=parse_date(last.get("date")) or parse_date(b.get("status_date")) or utcnow(),
            language="en",
            industry_hint=query.industry_hint,
            query=query.query_text,
            raw={"bill_id": b.get("bill_id"), "bill_number": b.get("bill_number"), "status": status,
                 "status_date": b.get("status_date"), "state_link": b.get("state_link"),
                 "change_hash": b.get("change_hash"), "subjects": subjects,
                 "session": (b.get("session") or {}).get("session_name"), "introduced": history[0].get("date") if history else None,
                 "license": "LegiScan, CC BY 4.0: credit LegiScan and link to url"},
        )
