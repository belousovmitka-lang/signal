"""Mastodon: public hashtag timelines (/api/v1/timelines/tag/<tag>), no login.

Each sources.yaml query lists hashtags in `communities`; `params.instance` picks the server
(default mastodon.social, whose terms bind registered users only). Full-text search needs a
login, so hashtags are the route.

Consent, the fediverse way. A post is kept only if its author:
- opted in to search (`indexable: true`),
- has not opted out of search engines (`noindex`),
- is not a bot, and has no #nobot / #noindex in their bio.
Only public posts whose URL is in Mastodon's own format are kept, so each one can be re-checked
on its home server and purged when deleted (`deleted()`).
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from ..base import BaseConnector
from ..models import RawPost, SourceQuery
from ..parsing import html_to_text, parse_date
from ..privacy import hash_author
from . import register

log = logging.getLogger(__name__)

DEFAULT_INSTANCE = "mastodon.social"
PAGE = 40
_OPT_OUT = re.compile(r"#no(bot|index)\b", re.I)
_STATUS_URL = re.compile(r"^/@[^/]+/(\d+)$")


def consents(account: dict[str, Any]) -> bool:
    if account.get("bot") or account.get("noindex"):
        return False
    if account.get("indexable") is not True:
        return False
    return not _OPT_OUT.search(html_to_text(account.get("note")))


def status_api_url(url: str) -> str | None:
    """https://host/@user/123 -> https://host/api/v1/statuses/123 (Mastodon-format URLs only)."""
    parts = urlsplit(url)
    m = _STATUS_URL.match(parts.path)
    return f"https://{parts.hostname}/api/v1/statuses/{m.group(1)}" if m and parts.hostname else None


@register
class MastodonConnector(BaseConnector):
    connector = "mastodon"
    route = "own_api"
    source_type = "social"

    async def fetch(self, query: SourceQuery) -> AsyncIterator[RawPost]:
        if not query.communities:
            raise ValueError("mastodon query needs hashtags in communities")
        instance = query.params.get("instance", DEFAULT_INSTANCE)
        seen: set[str] = set()
        for tag in query.communities:
            async for post in self._tag(instance, tag.lstrip("#"), query):
                if post.id not in seen:
                    seen.add(post.id)
                    yield post

    async def _tag(self, instance: str, tag: str, query: SourceQuery) -> AsyncIterator[RawPost]:
        url: str | None = f"https://{instance}/api/v1/timelines/tag/{quote(tag)}"
        params: dict[str, Any] | None = {"limit": PAGE}
        kept = 0
        while url and kept < query.limit:
            await self.services.robots.require(url)
            response = await self.http.get(url, params=params)
            response.raise_for_status()
            statuses = response.json()
            if not statuses:
                return
            for status in statuses:
                published = parse_date(status.get("created_at"))
                if query.since and published and published < query.since:
                    return  # newest first
                post = self._to_post(status, instance, tag, query)
                if post:
                    kept += 1
                    yield post
            url, params = response.links.get("next", {}).get("url"), None

    def _to_post(self, status: dict[str, Any], instance: str, tag: str, query: SourceQuery) -> RawPost | None:
        if status.get("reblog") or status.get("visibility") != "public":
            return None
        account = status.get("account") or {}
        url = status.get("url") or ""
        if not consents(account) or not status_api_url(url):
            return None
        return self.make_post(
            url=url,
            title=None,
            text=html_to_text(status.get("content")),
            is_snippet=False,
            author_hash=hash_author(account.get("acct", "") + ("" if "@" in account.get("acct", "") else f"@{instance}"),
                                    "fediverse"),
            community=f"#{tag}",
            published_at=parse_date(status.get("created_at")),
            language=status.get("language"),
            engagement={"favourites": status.get("favourites_count"), "reblogs": status.get("reblogs_count"),
                        "replies": status.get("replies_count")},
            industry_hint=query.industry_hint,
            query=f"#{tag}",
            raw={"uri": status.get("uri"), "instance": instance,
                 "tags": [t.get("name") for t in status.get("tags", [])],
                 "spoiler_text": status.get("spoiler_text") or None},
        )

    async def deleted(self, urls: list[str]) -> list[str]:
        """Posts gone from their home server, or whose author withdrew consent.

        A home server whose robots.txt now forbids the check counts as gone (we can't honor deletions
        there); an unreachable one is skipped and checked again next run.
        """
        gone = []
        for url in urls:
            api = status_api_url(url)
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
            elif response.status_code == 200 and not consents(response.json().get("account") or {}):
                gone.append(url)
        return gone
