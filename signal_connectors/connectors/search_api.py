"""G1: find posts with a search API, then read each one from its own platform.

Search results are only pointers (see discovery.py): every URL is handed to the resolver
for its platform, which fetches the post through that platform's allowed route. URLs no
resolver accepts are dropped, unstored.

Resolvers:
- Bluesky: AT Protocol public AppView (app.bsky.feed.getPosts, 25 per call, no login).
  Authors who hide their posts from logged-out viewers (!no-unauthenticated) are skipped.
- Medium: the post's entry in its author's or publication's RSS feed.
- Substack: the post's entry in its publication's RSS feed (<pub>.substack.com/feed).
A post that is no longer in its feed is skipped.

sources.yaml query:
    - keywords: ["paralegals frustrated with manual intake"]
      domains: [bsky.app]
      params: {provider: exa}
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import AsyncIterator, Iterable
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlsplit

import feedparser

from ..base import BaseConnector, HttpClient
from ..discovery import Discovery
from ..models import HealthStatus, RawPost, SourceQuery
from ..parsing import parse_date
from ..privacy import hash_author
from ..robots import RobotsChecker
from . import register
from .rss import entry_fields, feed_meta

log = logging.getLogger(__name__)

BSKY_API = "https://public.api.bsky.app/xrpc"
BSKY_BATCH = 25
NO_UNAUTHENTICATED = "!no-unauthenticated"
_BSKY_POST = re.compile(r"^/profile/([^/]+)/post/([A-Za-z0-9]+)/?$")
_MEDIUM_ID = re.compile(r"-([0-9a-f]{10,16})$")


class Resolver:
    name: str

    def __init__(self, http: HttpClient, robots: RobotsChecker) -> None:
        self.http = http
        self.robots = robots

    def accepts(self, url: str) -> bool:
        raise NotImplementedError

    def resolve(self, urls: list[str]) -> AsyncIterator[dict[str, Any]]:
        """Yield RawPost fields for the URLs it can read."""
        raise NotImplementedError


class BlueskyResolver(Resolver):
    name = "bluesky"

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self._dids: dict[str, str | None] = {}

    @staticmethod
    def parse(url: str) -> tuple[str, str] | None:
        parts = urlsplit(url)
        if (parts.hostname or "").removeprefix("www.") != "bsky.app":
            return None
        m = _BSKY_POST.match(parts.path)
        return (m.group(1), m.group(2)) if m else None

    def accepts(self, url: str) -> bool:
        return self.parse(url) is not None

    async def did(self, actor: str) -> str | None:
        if actor.startswith("did:"):
            return actor
        if actor not in self._dids:
            r = await self.http.get(f"{BSKY_API}/com.atproto.identity.resolveHandle", params={"handle": actor})
            self._dids[actor] = r.json().get("did") if r.status_code == 200 else None
        return self._dids[actor]

    async def at_uris(self, urls: Iterable[str]) -> list[str]:
        uris = []
        for url in urls:
            parsed = self.parse(url)
            if parsed and (did := await self.did(parsed[0])):
                uris.append(f"at://{did}/app.bsky.feed.post/{parsed[1]}")
        return list(dict.fromkeys(uris))

    async def get_posts(self, uris: list[str]) -> list[dict[str, Any]]:
        posts: list[dict[str, Any]] = []
        for i in range(0, len(uris), BSKY_BATCH):
            batch = uris[i:i + BSKY_BATCH]
            await self.robots.require(f"{BSKY_API}/app.bsky.feed.getPosts")
            data = await self.http.get_json(f"{BSKY_API}/app.bsky.feed.getPosts", params=[("uris", u) for u in batch])
            posts += data.get("posts", [])
        return posts

    @staticmethod
    def hidden(post: dict[str, Any]) -> bool:
        labels = (post.get("author") or {}).get("labels", []) + post.get("labels", [])
        return any(label.get("val") == NO_UNAUTHENTICATED for label in labels)

    @staticmethod
    def web_url(uri: str) -> str:
        did, _, rkey = uri.removeprefix("at://").partition("/app.bsky.feed.post/")
        return f"https://bsky.app/profile/{did}/post/{rkey}"

    async def resolve(self, urls: list[str]) -> AsyncIterator[dict[str, Any]]:
        for post in await self.get_posts(await self.at_uris(urls)):
            if self.hidden(post):
                continue
            record = post.get("record") or {}
            author = post.get("author") or {}
            langs = record.get("langs") or []
            yield {
                "source": "bluesky",
                "route": "own_api",
                "source_type": "social",
                "url": self.web_url(post["uri"]),
                "title": None,
                "text": record.get("text", ""),
                "is_snippet": False,
                "author_hash": hash_author(author.get("did"), "bluesky"),
                "community": "Bluesky",
                "published_at": parse_date(record.get("createdAt")) or parse_date(post.get("indexedAt")),
                "language": langs[0].split("-")[0] if langs else None,
                "engagement": {k: post.get(f"{k}Count") for k in ("like", "reply", "repost", "quote")},
                "raw": {"uri": post["uri"], "cid": post.get("cid"),
                        "embed": (record.get("embed") or {}).get("$type")},
            }

    async def deleted(self, urls: list[str]) -> list[str]:
        """URLs whose posts were deleted or hidden from logged-out viewers since we stored them."""
        by_uri = {}
        for url in urls:
            parsed = self.parse(url)
            if parsed and parsed[0].startswith("did:"):
                by_uri[f"at://{parsed[0]}/app.bsky.feed.post/{parsed[1]}"] = url
        alive = {p["uri"] for p in await self.get_posts(list(by_uri)) if not self.hidden(p)}
        return [url for uri, url in by_uri.items() if uri not in alive]


class FeedResolver(Resolver):
    """Find each post's entry in a feed derived from its URL (Medium, Substack)."""

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self._feeds: dict[str, Any] = {}

    def feed_url(self, url: str) -> str | None:
        raise NotImplementedError

    def source_name(self, url: str) -> str:
        raise NotImplementedError

    def same_post(self, entry_link: str, url: str) -> bool:
        return urlsplit(entry_link).path.rstrip("/") == urlsplit(url).path.rstrip("/")

    def accepts(self, url: str) -> bool:
        return self.feed_url(url) is not None

    async def feed(self, feed_url: str) -> Any | None:
        if feed_url not in self._feeds:
            parsed = None
            if await self.robots.allowed(feed_url):
                r = await self.http.get(feed_url)
                if r.status_code == 200:
                    parsed = feedparser.parse(r.content)
            self._feeds[feed_url] = parsed
        return self._feeds[feed_url]

    async def resolve(self, urls: list[str]) -> AsyncIterator[dict[str, Any]]:
        for url in urls:
            feed_url = self.feed_url(url)
            parsed = await self.feed(feed_url) if feed_url else None
            if not parsed or not parsed.entries:
                continue
            name = self.source_name(url)
            community, language, domain = feed_meta(parsed, feed_url, None, name)
            for entry in parsed.entries:
                link = entry.get("link") or ""
                if link and self.same_post(link, url):
                    fields = entry_fields(entry, feed_url, name, community, language, domain)
                    if fields:
                        yield fields
                    break


class MediumResolver(FeedResolver):
    name = "medium"

    def feed_url(self, url: str) -> str | None:
        parts = urlsplit(url)
        host = (parts.hostname or "").removeprefix("www.")
        segments = [s for s in parts.path.split("/") if s]
        if not segments or not _MEDIUM_ID.search(segments[-1]):
            return None
        if host == "medium.com" and len(segments) == 2 and segments[0] != "p":
            return f"https://medium.com/feed/{quote(segments[0], safe='@')}"       # @user or publication
        if host.endswith(".medium.com") and len(segments) == 1:
            return f"https://{host}/feed"
        return None

    def source_name(self, url: str) -> str:
        return "medium"

    def same_post(self, entry_link: str, url: str) -> bool:
        a = _MEDIUM_ID.search(urlsplit(entry_link).path.rstrip("/"))
        b = _MEDIUM_ID.search(urlsplit(url).path.rstrip("/"))
        return bool(a and b and a.group(1) == b.group(1))


class SubstackResolver(FeedResolver):
    name = "substack"

    def feed_url(self, url: str) -> str | None:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if host.endswith(".substack.com") and parts.path.startswith("/p/"):
            return f"https://{host}/feed"
        return None

    def source_name(self, url: str) -> str:
        return (urlsplit(url).hostname or "").removesuffix(".substack.com") + ".substack"


RESOLVERS = (BlueskyResolver, MediumResolver, SubstackResolver)


@register
class SearchApiConnector(BaseConnector):
    connector = "search_api"
    route = "search_api"
    source_type = "community"
    default_cadence = "weekly"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.discovery = Discovery(self.http, self.services.costs, self.name)
        self.resolvers = [cls(self.http, self.services.robots) for cls in RESOLVERS]
        self.dropped: Counter[str] = Counter()   # domains of URLs no resolver accepts (counts only)

    async def healthcheck(self) -> HealthStatus:
        status = await super().healthcheck()
        costs = self.services.costs
        left = {p: round(costs.provider_remaining(p), 4) for p in ("brave", "exa")}
        keys = {"brave": bool(self.discovery.brave_key), "exa": bool(self.discovery.exa_key)}
        status.auth_ok = any(keys.values())
        status.ok = status.auth_ok
        status.message = f"provider budget left {left}; keys {keys}"
        return status

    async def fetch(self, query: SourceQuery) -> AsyncIterator[RawPost]:
        urls = await self.discovery.urls(query.params.get("provider", "exa"), query.query_text or "",
                                         query.domains, query.since, query.limit, query.params.get("lang", "en"))
        groups: dict[Resolver, list[str]] = {}
        for url in urls:
            resolver = next((r for r in self.resolvers if r.accepts(url)), None)
            if resolver is None:
                self.dropped[(urlsplit(url).hostname or "?").removeprefix("www.")] += 1
                continue
            groups.setdefault(resolver, []).append(url)
        if self.dropped:
            log.info("%s: dropped %d URLs no resolver can read: %s", self.name, sum(self.dropped.values()),
                     dict(self.dropped.most_common(8)))

        for resolver, group in groups.items():
            async for fields in resolver.resolve(group):
                post = self.make_post(industry_hint=query.industry_hint, query=query.query_text,
                                      **{k: v for k, v in fields.items() if v is not None or k == "title"})
                if post:
                    yield post

    async def deleted(self, urls: list[str]) -> list[str]:
        """Stored URLs that their platform no longer serves (used by the deletion re-check)."""
        bluesky = next(r for r in self.resolvers if isinstance(r, BlueskyResolver))
        return await bluesky.deleted([u for u in urls if bluesky.accepts(u)])
