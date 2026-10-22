"""X API v2 recent search, stored: evidence posts for the feed's inferred problems.

Called on demand through `signal_connectors.x_search.search_x`, never on a schedule (options.on_demand).
query: {params: {q: '<X search query>', tag: '<caller tag>'}, limit: 10}

- Billed per post returned ($0.005, deduplicated by X within a UTC day); metered under this source.
- No user lookups: author_id comes with the post and is stored only as a salted hash; the role is
  kept only when the post states it ("I'm a dispatcher").
- Deletions (Developer Policy, "Content compliance": stored content must follow deletes and edits,
  within 24 hours of a request): `deleted()` runs a Batch Compliance job over every stored post ID.
  Compliance jobs aren't billed (checked against /2/usage/tweets, 2026-10-16). Edited posts
  ("rehydrate") are purged rather than re-fetched; the next search stores the current version.
- Display: the app renders stored posts with X's embed widget (raw.embed), as the live lane does.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

from ..base import BaseConnector
from ..industries import load_industries
from ..models import RawPost, SourceQuery, utcnow
from ..parsing import parse_date
from ..privacy import extract_author_role, hash_author
from ..runner import RecheckPending
from . import register

log = logging.getLogger(__name__)

SEARCH = "https://api.x.com/2/tweets/search/recent"
JOBS = "https://api.x.com/2/compliance/jobs"
POST_USD = 0.005                         # per post returned (docs.x.com pricing, 2026-10-16)
FILTERS = "lang:en -is:retweet -is:reply -is:quote -from:grok"
JOB_PREFIX = "signal-x-recheck"
POLL_SECONDS = 30                        # X: poll with backoff, every 30-60 s
WAIT_SECONDS = 90                        # recent IDs "typically complete in well under an hour": don't block
FRESH_JOB = timedelta(hours=20)          # a job this recent already covers this day's re-check


class XSearchError(RuntimeError):
    """The X API refused the search (auth, credits, malformed query)."""


def post_url(post_id: str) -> str:
    return f"https://x.com/i/web/status/{post_id}"


def post_id_of(url: str) -> str | None:
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    return tail if tail.isdigit() else None


def _roles(industry: str | None) -> list[str]:
    maps = load_industries()
    chosen = [maps[industry]] if industry in maps else list(maps.values())
    return [r for m in chosen for r in m.vocabulary.roles]


@register
class XConnector(BaseConnector):
    connector = "x"
    route = "own_api"
    source_type = "social"
    default_cadence = "hourly"
    filter_since = False                 # the caller wants what X has now (7-day window)

    def _headers(self) -> dict[str, str]:
        token = os.environ.get("X_BEARER_TOKEN")
        if not token:
            raise XSearchError("X_BEARER_TOKEN is not set")
        return {"Authorization": f"Bearer {token}"}

    async def fetch(self, query: SourceQuery) -> AsyncIterator[RawPost]:
        q = (query.params.get("q") or query.query_text or "").strip()
        if not q:
            return
        cap = int(self.source.options.get("max_results", 10))
        n = max(10, min(query.limit, cap, 100))          # the endpoint's page is 10..100
        with self.services.costs.metered(self.name, usd=n * POST_USD, note="search/recent") as charge:
            r = await self.http.get(SEARCH, attempts=1, headers=self._headers(), params={
                "query": f"{q} {FILTERS}", "max_results": n, "sort_order": "relevancy",
                "tweet.fields": "created_at,author_id,public_metrics,lang,conversation_id,note_tweet"})
            data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            charge.usd = len(data.get("data") or []) * POST_USD
        if r.status_code >= 400:
            detail = data.get("detail") or data.get("title") or r.text[:200]
            raise XSearchError(f"X search HTTP {r.status_code}: {detail}")
        roles = _roles(query.industry_hint)
        for item in data.get("data") or []:
            post = self._to_post(item, q, query, roles)
            if post:
                yield post

    def _to_post(self, item: dict[str, Any], q: str, query: SourceQuery, roles: list[str]) -> RawPost | None:
        text = ((item.get("note_tweet") or {}).get("text") or item.get("text") or "").strip()
        m = item.get("public_metrics") or {}
        return self.make_post(
            url=post_url(item["id"]),
            title=None,
            text=text,
            is_snippet=False,
            author_hash=hash_author(item.get("author_id"), "x"),
            author_role=extract_author_role(text, roles),
            community=None,
            published_at=parse_date(item.get("created_at")),
            language=item.get("lang"),
            engagement={"likes": m.get("like_count"), "replies": m.get("reply_count"),
                        "reposts": m.get("retweet_count"), "quotes": m.get("quote_count")},
            industry_hint=query.industry_hint,
            query=q,
            raw={"x_id": item["id"], "conversation_id": item.get("conversation_id"),
                 "tag": query.params.get("tag"), "embed": f"https://twitter.com/x/status/{item['id']}"},
        )

    # --- deletions: Batch Compliance ------------------------------------------

    async def deleted(self, urls: list[str]) -> list[str]:
        """URLs whose posts X says to delete (deleted, protected, deactivated, suspended) or re-fetch (edited).

        A job usually takes longer than a run should wait (8 minutes wasn't enough, 2026-10-17..21), so a
        re-check starts a job and, if it hasn't finished within WAIT_SECONDS, raises RecheckPending: the
        scheduler comes back every 15 minutes and applies the job once it's complete. Finished jobs are
        remembered in seen_items, so a stale "edited" result can't purge a fresher copy later. A job created
        within FRESH_JOB counts as today's re-check (no new job). One active tweets job at a time (X: 409)."""
        by_id = {pid: u for u in urls if (pid := post_id_of(u))}
        if not by_id:
            return []
        headers = self._headers()
        listing = await self.http.get(JOBS, attempts=1, headers=headers, params={"type": "tweets"})
        jobs = [j for j in (listing.json().get("data") or []) if str(j.get("name", "")).startswith(JOB_PREFIX)]
        gone: set[str] = set()
        applied = self.services.store.seen_hashes(f"{self.name}:compliance", [str(j["id"]) for j in jobs])
        for job in jobs:
            if job.get("status") == "complete" and str(job["id"]) not in applied:
                gone |= await self._results(job, headers)
        found = [by_id[i] for i in gone if i in by_id]
        if any(j.get("status") in ("created", "in_progress") for j in jobs):
            raise RecheckPending(found, "compliance job still running; applied earlier results")
        fresh = [j for j in jobs if (at := parse_date(j.get("created_at"))) and utcnow() - at < FRESH_JOB
                 and j.get("status") == "complete"]
        if fresh:
            return found
        created = await self.http.request("POST", JOBS, attempts=1, headers=headers,
                                       json={"type": "tweets", "name": f"{JOB_PREFIX}-{len(by_id)}"})
        if created.status_code >= 400:
            raise XSearchError(f"compliance job HTTP {created.status_code}: {created.text[:200]}")
        job = created.json()["data"]
        up = await self.http.request("PUT", job["upload_url"], attempts=1, content="\n".join(by_id).encode(),
                                 headers={**headers, "Content-Type": "text/plain"})
        if up.status_code >= 400:
            raise XSearchError(f"compliance upload HTTP {up.status_code}")
        for _ in range(WAIT_SECONDS // 30):
            await asyncio.sleep(POLL_SECONDS)
            status = (await self.http.get(f"{JOBS}/{job['id']}", attempts=1, headers=headers)).json()["data"]
            if status.get("status") == "complete":
                gone |= await self._results(status, headers)
                return [by_id[i] for i in gone if i in by_id]
            if status.get("status") in ("failed", "expired"):
                raise XSearchError(f"compliance job {status.get('status')}")
        raise RecheckPending([by_id[i] for i in gone if i in by_id],
                             f"compliance job not finished after {WAIT_SECONDS}s; applied within 15 minutes")

    async def _results(self, job: dict[str, Any], headers: dict[str, str]) -> set[str]:
        r = await self.http.get(job["download_url"], attempts=1, headers=headers)
        if r.status_code >= 400:
            raise XSearchError(f"compliance download HTTP {r.status_code}")
        self.services.store.mark_seen(f"{self.name}:compliance", {str(job["id"]): None}, utcnow())
        out = set()
        for line in r.text.splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if event.get("action") in ("delete", "rehydrate"):
                out.add(str(event["id"]))
        return out
