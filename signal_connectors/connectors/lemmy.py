"""Lemmy: post search and community listings on a Lemmy instance's public API (no login).

sources.yaml query: `keywords` -> one /api/v3/search per keyword (Posts, newest first);
`communities` -> /api/v3/post/list per community. `params.instance` defaults to lemmy.world,
whose robots.txt asks for a 60-second crawl delay (applied automatically), so keep runs small.

Skipped: bot accounts, deleted or removed posts. Each post is stored under its origin URL
(ap_id), and `deleted()` re-checks it on that origin instance.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..base import BaseConnector
from ..models import RawPost, SourceQuery
from ..parsing import parse_date
from ..privacy import hash_author
from . import register

log = logging.getLogger(__name__)

DEFAULT_INSTANCE = "lemmy.world"
PAGE = 50
MAX_PAGES = 4
_POST_URL = re.compile(r"^/post/(\d+)$")


def post_api_url(ap_id: str) -> str | None:
    parts = urlsplit(ap_id)
    m = _POST_URL.match(parts.path)
    return f"https://{parts.hostname}/api/v3/post?id={m.group(1)}" if m and parts.hostname else None


@register
class LemmyConnector(BaseConnector):
    connector = "lemmy"
    route = "own_api"
    source_type = "community"

    async def fetch(self, query: SourceQuery) -> AsyncIterator[RawPost]:
        instance = query.params.get("instance", DEFAULT_INSTANCE)
        if not query.keywords and not query.communities:
            raise ValueError("lemmy query needs keywords or communities")
        seen: set[str] = set()
        jobs = [("search", {"q": k, "type_": "Posts", "listing_type": "All"}, k) for k in query.keywords]
        jobs += [("post/list", {"community_name": c}, None) for c in query.communities]
        for method, params, phrase in jobs:
            async for post in self._pages(instance, method, params, phrase, query):
                if post.id not in seen:
                    seen.add(post.id)
                    yield post

    async def _pages(self, instance: str, method: str, params: dict[str, Any], phrase: str | None,
                     query: SourceQuery) -> AsyncIterator[RawPost]:
        url = f"https://{instance}/api/v3/{method}"
        kept = 0
        for page in range(1, MAX_PAGES + 1):
            await self.services.robots.require(url)
            response = await self.http.get(url, params={**params, "sort": "New", "limit": PAGE, "page": page})
            response.raise_for_status()
            views = response.json().get("posts", [])
            if not views:
                return
            for view in views:
                published = parse_date(view.get("post", {}).get("published"))
                if query.since and published and published < query.since:
                    return  # newest first
                post = self._to_post(view, phrase, query)
                if post:
                    kept += 1
                    yield post
                    if kept >= query.limit:
                        return

    def _to_post(self, view: dict[str, Any], phrase: str | None, query: SourceQuery) -> RawPost | None:
        post, creator, community = view.get("post", {}), view.get("creator", {}), view.get("community", {})
        if post.get("deleted") or post.get("removed") or creator.get("bot_account"):
            return None
        ap_id = post.get("ap_id") or ""
        if not post_api_url(ap_id):
            return None
        counts = view.get("counts", {})
        host = urlsplit(community.get("actor_id") or ap_id).hostname or ""
        return self.make_post(
            url=ap_id,
            title=post.get("name"),
            text=(post.get("body") or "").strip(),
            is_snippet=False,
            author_hash=hash_author(creator.get("actor_id"), "fediverse"),
            community=f"!{community.get('name')}@{host}" if community.get("name") else None,
            published_at=parse_date(post.get("published")),
            language=None,
            engagement={"score": counts.get("score"), "comments": counts.get("comments"),
                        "upvotes": counts.get("upvotes")},
            industry_hint=query.industry_hint,
            query=phrase,
            raw={"link": post.get("url"), "nsfw": post.get("nsfw"), "community_id": community.get("id")},
        )

    async def deleted(self, urls: list[str]) -> list[str]:
        """Posts deleted or removed on their origin instance.

        An origin whose robots.txt forbids the check counts as gone (we can't honor deletions there);
        an unreachable one is skipped and checked again next run.
        """
        gone = []
        for url in urls:
            api = post_api_url(url)
            if not api:
                continue
            try:
                if not await self.services.robots.allowed(api):
                    if (await self.services.robots.rules_for(api)).unavailable:
                        log.info("%s: can't reach %s; will re-check later", self.name, api)
                    else:
                        gone.append(url)      # robots.txt explicitly forbids the check
                    continue
                response = await self.http.get(api)
            except httpx.HTTPError as exc:
                log.info("%s: can't reach %s (%s); will re-check later", self.name, api, type(exc).__name__)
                continue
            if response.status_code in (404, 410):
                gone.append(url)
            elif response.status_code == 200:
                p = response.json().get("post_view", {}).get("post", {})
                if p.get("deleted") or p.get("removed"):
                    gone.append(url)
        return gone
