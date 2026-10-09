"""G3 Reddit, own API (paid/approval; feature-flagged off: ENABLE_REDDIT_API).

Data API Terms (checked 2026-10-09): commercial use needs "a separate agreement with Reddit";
no training ML/AI on User Content without rightsholders' permission; "immediately delete any
data not required" for the approved use case; delete everything on termination. Until the
agreement exists this connector must stay off; reddit.com robots.txt disallows our bot and
search results can't be stored, so there is no other route.

Auth: app-only OAuth (client credentials), User-Agent "<platform>:<app id>:<version> (by /u/<user>)"
from options.user_agent. Free-tier limit: 100 queries per minute per client id.

Queries: `communities` = subreddits (options.industry_subreddits adds one query per industry
map), `keywords` -> /r/<sub>/search?restrict_sr=1&sort=new, else /r/<sub>/new.
Deletions: deleted() asks /api/info for up to 100 posts at a time; [deleted]/[removed] -> purge.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from ..base import BaseConnector
from ..models import HealthStatus, RawPost, SourceQuery
from ..privacy import extract_author_role, hash_author
from . import register

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API = "https://oauth.reddit.com"
PAGE = 100
GONE = {"[deleted]", "[removed]"}
_POST_ID = re.compile(r"/comments/([a-z0-9]+)", re.I)


class RedditAuthError(Exception):
    pass


@register
class RedditConnector(BaseConnector):
    connector = "reddit"
    route = "own_api"
    source_type = "community"
    default_cadence = "hourly"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.client_id = os.environ.get("REDDIT_CLIENT_ID") or None
        self.secret = os.environ.get("REDDIT_CLIENT_SECRET") or None
        self.user_agent = self.source.options.get("user_agent") or self.http.user_agent
        self.roles: list[str] = self.source.options.get("roles", [])
        self._token: tuple[str, float] | None = None

    async def healthcheck(self) -> HealthStatus:
        status = await super().healthcheck()
        status.auth_ok = bool(self.client_id and self.secret)
        status.ok = status.auth_ok
        status.message = None if status.auth_ok else "REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET not set"
        return status

    async def token(self) -> str:
        if self._token and self._token[1] > time.monotonic() + 60:
            return self._token[0]
        if not (self.client_id and self.secret):
            raise RedditAuthError("REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET not set")
        response = await self.http.request("POST", TOKEN_URL, data={"grant_type": "client_credentials"},
                                           auth=(self.client_id, self.secret), headers={"User-Agent": self.user_agent})
        if response.status_code != 200:
            raise RedditAuthError(f"Reddit token HTTP {response.status_code}")
        data = response.json()
        self._token = (data["access_token"], time.monotonic() + float(data.get("expires_in", 3600)))
        return self._token[0]

    async def get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {await self.token()}", "User-Agent": self.user_agent}
        response = await self.http.get(f"{API}{path}", params={**params, "raw_json": 1}, headers=headers)
        response.raise_for_status()
        return response.json()

    async def fetch(self, query: SourceQuery) -> AsyncIterator[RawPost]:
        if not query.communities:
            raise ValueError("reddit query needs subreddits in communities")
        for sub in query.communities:
            path = f"/r/{sub}/search" if query.keywords else f"/r/{sub}/new"
            params: dict[str, Any] = {"limit": PAGE}
            if query.keywords:
                params |= {"q": query.query_text, "restrict_sr": 1, "sort": "new", "t": "week"}
            after, kept = None, 0
            while kept < query.limit:
                data = await self.get(path, {**params, **({"after": after} if after else {})})
                children = (data.get("data") or {}).get("children", [])
                for child in children:
                    post = self._to_post(child.get("data") or {}, query)
                    if post is None:
                        continue
                    if query.since and post.published_at < query.since:
                        after = None
                        break     # newest first
                    kept += 1
                    yield post
                else:
                    after = (data.get("data") or {}).get("after")
                if not after or not children:
                    break

    def _to_post(self, d: dict[str, Any], query: SourceQuery) -> RawPost | None:
        if not d.get("id") or d.get("author") in GONE or d.get("selftext") in GONE or d.get("removed_by_category"):
            return None
        text = d.get("selftext") or ""
        return self.make_post(
            url=f"https://www.reddit.com{d.get('permalink') or ''}",
            title=d.get("title"),
            text=text,
            is_snippet=False,
            author_hash=hash_author(d.get("author"), "reddit"),
            author_role=extract_author_role(f"{d.get('title', '')} {text}", self.roles),
            community=f"r/{d.get('subreddit')}" if d.get("subreddit") else None,
            published_at=datetime.fromtimestamp(float(d.get("created_utc", 0)), tz=timezone.utc),
            language=None,
            engagement={"score": d.get("score"), "comments": d.get("num_comments")},
            industry_hint=query.industry_hint,
            query=query.query_text,
            raw={"fullname": d.get("name"), "flair": d.get("link_flair_text"), "over_18": d.get("over_18"),
                 "external_url": None if d.get("is_self") else d.get("url")},
        )

    async def deleted(self, urls: list[str]) -> list[str]:
        """Reddit terms: purge posts deleted or removed since we stored them."""
        by_name = {f"t3_{m.group(1)}": u for u in urls if (m := _POST_ID.search(u))}
        names, gone = list(by_name), []
        for i in range(0, len(names), 100):
            batch = names[i:i + 100]
            data = await self.get("/api/info", {"id": ",".join(batch)})
            alive = {c["data"]["name"] for c in (data.get("data") or {}).get("children", [])
                     if c["data"].get("author") not in GONE and c["data"].get("selftext") not in GONE
                     and not c["data"].get("removed_by_category")}
            gone += [by_name[n] for n in batch if n not in alive]
        return gone
