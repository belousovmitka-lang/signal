"""Hacker News: Algolia HN Search API for keyword search, Firebase API for live lists.

Query modes (per sources.yaml query):
- keywords: each keyword is a separate phrase search on `search_by_date`, newest first.
  `params.tags` narrows the item types (default "(story,comment)"; e.g. "ask_hn").
- no keywords, `params.tags`: the newest items with those tags (e.g. every new Ask HN).
- `params.list`: a Firebase list ("askstories", "showstories", "jobstories", "newstories"),
  each item then read from /v0/item/<id>.json.

Every post links to its HN discussion page; the external link of a story is kept in raw.
"""

from __future__ import annotations

import html
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from ..base import BaseConnector
from ..models import RawPost, SourceQuery
from ..parsing import html_to_text
from ..privacy import hash_author
from . import register

ALGOLIA = "https://hn.algolia.com/api/v1/search_by_date"
FIREBASE = "https://hacker-news.firebaseio.com/v0"
ITEM_URL = "https://news.ycombinator.com/item?id={}"
NEWEST_FIRST = {"askstories", "showstories", "jobstories", "newstories"}
FIREBASE_LISTS = NEWEST_FIRST | {"topstories", "beststories"}
DEFAULT_TAGS = "(story,comment)"
PAGE_SIZE_MAX = 100


def _community(tags: list[str], item_type: str | None = None) -> str:
    if "ask_hn" in tags:
        return "Ask HN"
    if "show_hn" in tags:
        return "Show HN"
    if "job" in tags or item_type == "job":
        return "HN Jobs"
    return "Hacker News"


def _title_tags(title: str | None) -> list[str]:
    t = (title or "").lower()
    return ["ask_hn"] if t.startswith("ask hn") else ["show_hn"] if t.startswith("show hn") else []


@register
class HackerNewsConnector(BaseConnector):
    connector = "hackernews"
    route = "own_api"
    source_type = "community"
    default_cadence = "hourly"

    async def fetch(self, query: SourceQuery) -> AsyncIterator[RawPost]:
        if list_name := query.params.get("list"):
            async for post in self._firebase_list(list_name, query):
                yield post
            return
        tags = query.params.get("tags", DEFAULT_TAGS)
        phrases = query.keywords or [""]
        if phrases == [""] and tags == DEFAULT_TAGS:
            raise ValueError("hackernews query needs keywords, params.tags or params.list")
        seen: set[str] = set()
        for phrase in phrases:
            async for post in self._search(phrase, tags, query):
                if post.id not in seen:
                    seen.add(post.id)
                    yield post

    # --- Algolia ---------------------------------------------------------------

    async def _search(self, phrase: str, tags: str, query: SourceQuery) -> AsyncIterator[RawPost]:
        since = int(query.since.timestamp()) if query.since else 0
        search = f'"{phrase}"' if phrase and " " in phrase and not phrase.startswith('"') else phrase
        page, fetched = 0, 0
        while fetched < query.limit:
            data = await self.http.get_json(ALGOLIA, params={
                "query": search,
                "tags": tags,
                "numericFilters": f"created_at_i>{since}",
                "hitsPerPage": min(PAGE_SIZE_MAX, query.limit),
                "page": page,
            })
            for hit in data.get("hits", []):
                post = self._hit_to_post(hit, phrase or None, query)
                if post:
                    fetched += 1
                    yield post
            page += 1
            if page >= data.get("nbPages", 0):
                return

    def _hit_to_post(self, hit: dict[str, Any], phrase: str | None, query: SourceQuery) -> RawPost | None:
        tags = hit.get("_tags", [])
        is_comment = "comment" in tags
        body = hit.get("comment_text") if is_comment else hit.get("story_text")
        title = hit.get("story_title") if is_comment else hit.get("title")
        return self.make_post(
            url=ITEM_URL.format(hit["objectID"]),
            title=html.unescape(title) if title else None,
            text=html_to_text(body),
            is_snippet=False,
            author_hash=hash_author(hit.get("author"), "hn"),
            community=_community(tags),
            published_at=datetime.fromtimestamp(hit["created_at_i"], tz=timezone.utc),
            language="en",
            engagement={"points": hit.get("points"), "comments": hit.get("num_comments")},
            industry_hint=query.industry_hint,
            query=phrase,
            raw={
                "objectID": hit["objectID"],
                "type": "comment" if is_comment else "story",
                "tags": [t for t in tags if not t.startswith("author_")],
                "external_url": hit.get("url") or hit.get("story_url"),
                "story_id": hit.get("story_id"),
                "parent_id": hit.get("parent_id"),
            },
        )

    # --- Firebase --------------------------------------------------------------

    async def _firebase_list(self, name: str, query: SourceQuery) -> AsyncIterator[RawPost]:
        if name not in FIREBASE_LISTS:
            raise ValueError(f"unknown Hacker News list {name!r}")
        ids = await self.http.get_json(f"{FIREBASE}/{name}.json") or []
        fetched = 0
        for item_id in ids:
            if fetched >= query.limit:
                return
            item = await self.http.get_json(f"{FIREBASE}/item/{item_id}.json")
            if not item or item.get("deleted") or item.get("dead"):
                continue
            published = datetime.fromtimestamp(item["time"], tz=timezone.utc)
            if query.since and published < query.since:
                if name in NEWEST_FIRST:
                    return
                continue
            post = self._item_to_post(item, query)
            if post:
                fetched += 1
                yield post

    def _item_to_post(self, item: dict[str, Any], query: SourceQuery) -> RawPost | None:
        tags = [item.get("type", "story"), *_title_tags(item.get("title"))]
        return self.make_post(
            url=ITEM_URL.format(item["id"]),
            title=html.unescape(item["title"]) if item.get("title") else None,
            text=html_to_text(item.get("text")),
            is_snippet=False,
            author_hash=hash_author(item.get("by"), "hn"),
            community=_community(tags, item.get("type")),
            published_at=datetime.fromtimestamp(item["time"], tz=timezone.utc),
            language="en",
            engagement={"points": item.get("score"), "comments": item.get("descendants")},
            industry_hint=query.industry_hint,
            raw={"objectID": str(item["id"]), "type": item.get("type"), "tags": tags,
                 "external_url": item.get("url")},
        )
