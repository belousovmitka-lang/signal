"""G2: RSS and Atom feeds (trade press, newsletters, Medium tags, blogs, press wires).

Each query in sources.yaml is one feed:

    queries:
      - {industry_hint: legal, params: {feed: "https://abovethelaw.com/feed/", name: abovethelaw}}

so `limit` applies per feed and one broken feed never stops the others. Posts are stored
as source "rss:<name>". robots.txt is checked for every feed URL before it is read.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlsplit

import feedparser

from ..base import BaseConnector
from ..models import RawPost, SourceQuery, utcnow
from ..parsing import html_to_text, parse_date, struct_to_datetime
from ..privacy import hash_author
from . import register

log = logging.getLogger(__name__)

ACCEPT = "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5"


class FeedError(Exception):
    pass


def feed_name(url: str) -> str:
    host = (urlsplit(url).hostname or "feed").removeprefix("www.")
    return host.rsplit(".", 1)[0] if host.count(".") >= 1 else host


@register
class RssConnector(BaseConnector):
    connector = "rss"
    route = "rss"
    source_type = "news"
    parallel_queries = 8     # one feed per query, on different sites

    async def fetch(self, query: SourceQuery) -> AsyncIterator[RawPost]:
        url = query.params.get("feed")
        if not url:
            raise FeedError("rss query needs params.feed")
        name = query.params.get("name") or feed_name(url)

        await self.services.robots.require(url)
        response = await self.http.get(url, headers={"Accept": ACCEPT})
        response.raise_for_status()
        parsed = feedparser.parse(response.content)
        if not parsed.entries:
            if parsed.bozo:
                raise FeedError(f"{url}: unparseable feed ({parsed.get('bozo_exception')})")
            log.info("%s: feed %s has no entries", self.name, url)
            return

        community, language, domain = feed_meta(parsed, url, query.params.get("community") or None, name)
        for entry in parsed.entries:
            fields = entry_fields(entry, url, name, community, language, domain)
            if fields is None:
                self.invalid += 1
                continue
            post = self.make_post(industry_hint=query.industry_hint, **self.adapt(fields, entry, query))
            if post is not None:
                yield post

    def adapt(self, fields: dict[str, Any], entry: Any, query: SourceQuery) -> dict[str, Any]:
        """Hook for feed-based connectors that refine the fields (see classic_forum)."""
        return fields


def feed_meta(parsed: Any, feed_url: str, community: str | None, name: str) -> tuple[str, str | None, str]:
    """(community, language, domain) for a parsed feed."""
    meta = parsed.feed
    community = community or html_to_text(meta.get("title")) or name
    language = (meta.get("language") or "").split("-")[0].lower() or None
    domain = (urlsplit(feed_url).hostname or "").removeprefix("www.")
    return community, language, domain


def entry_fields(entry: Any, feed_url: str, name: str, community: str, language: str | None,
                 domain: str) -> dict[str, Any] | None:
    """RawPost fields for one feed entry (source "rss:<name>"), or None if it has no link.

    Shared with the search_api resolvers that find a post's entry in its author's feed.
    """
    link = entry.get("link") or (entry.get("id") if entry.get("guidislink") else None)
    if not link:
        return None

    content = entry.get("content") or []
    full_html = max((c.get("value", "") for c in content), key=len, default="")
    summary_html = entry.get("summary", "")
    text = html_to_text(full_html) or html_to_text(summary_html)

    # dict.get: feedparser's own .get silently maps "updated" to "published" (deprecated).
    published = (
        struct_to_datetime(dict.get(entry, "published_parsed"))
        or struct_to_datetime(dict.get(entry, "updated_parsed"))
        or parse_date(dict.get(entry, "published"))
        or parse_date(dict.get(entry, "updated"))
    )
    raw: dict[str, Any] = {
        "feed": feed_url,
        "guid": entry.get("id"),
        "tags": [t.get("term") for t in entry.get("tags", []) if t.get("term")],
    }
    if published is None:
        published = utcnow()
        raw["date_missing"] = True

    engagement = {}
    if (comments := entry.get("slash_comments")) and str(comments).isdigit():
        engagement["comments"] = int(comments)

    author = html_to_text(entry.get("author")) or None
    return {
        "source": f"rss:{name}",
        "route": "rss",
        "url": link,
        "title": html_to_text(entry.get("title")) or None,
        "text": text,
        "is_snippet": not full_html,
        "author_hash": hash_author(author, domain) if author else None,
        "community": community,
        "published_at": published,
        "language": language,
        "engagement": engagement,
        "raw": raw,
    }
