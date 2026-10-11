"""Product Hunt API v2 (GraphQL, developer token PRODUCTHUNT_TOKEN): new launches.

Docs (checked 2026-10-10): "The Product Hunt API must not be used for commercial purposes" without
permission from hello@producthunt.com; attribution linking back is requested. Keep this source
disabled in production until that permission exists. Rate limit: 6,250 complexity points per
15 minutes per application; a 429 stops the run.

sources.yaml query: {limit: 100, params: {comments_per_post: 0}}
Launches are stored as source "producthunt" (type startup). With comments_per_post > 0, each
launch's newest comments are stored as "producthunt:comments" (commenter names hashed).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

from ..base import BaseConnector
from ..costs import QuotaExceeded
from ..models import HealthStatus, RawPost, SourceQuery
from ..parsing import html_to_text, parse_date
from ..privacy import hash_author
from . import register

API = "https://api.producthunt.com/v2/api/graphql"
PAGE = 20

POSTS = """query($after: DateTime!, $cursor: String, $first: Int!, $comments: Int!) {
  posts(order: NEWEST, postedAfter: $after, after: $cursor, first: $first) {
    pageInfo { hasNextPage endCursor }
    edges { node {
      id name tagline description url votesCount commentsCount createdAt
      topics(first: 6) { edges { node { name } } }
      comments(first: $comments, order: NEWEST) {
        edges { node { id body createdAt votesCount url user { id } } } }
    } }
  }
}"""


@register
class ProductHuntConnector(BaseConnector):
    connector = "producthunt"
    route = "own_api"
    source_type = "startup"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.token = os.environ.get("PRODUCTHUNT_TOKEN") or None

    async def healthcheck(self) -> HealthStatus:
        status = await super().healthcheck()
        status.auth_ok = status.ok = bool(self.token)
        status.message = None if self.token else "PRODUCTHUNT_TOKEN is not set"
        return status

    async def fetch(self, query: SourceQuery) -> AsyncIterator[RawPost]:
        if not self.token:
            raise RuntimeError("PRODUCTHUNT_TOKEN is not set")
        n_comments = int(query.params.get("comments_per_post", 0))
        since = (query.since.strftime("%Y-%m-%dT%H:%M:%SZ") if query.since else "1970-01-01T00:00:00Z")
        cursor, fetched = None, 0
        while fetched < query.limit:
            response = await self.http.request(
                "POST", API, attempts=2, headers={"Authorization": f"Bearer {self.token}"},
                json={"query": POSTS, "variables": {"after": since, "cursor": cursor,
                                                    "first": min(PAGE, query.limit - fetched), "comments": n_comments}})
            if response.status_code == 429:
                raise QuotaExceeded("Product Hunt rate limit (6,250 points/15 min); stopping this run")
            data = response.json() if response.status_code == 200 else {}
            if response.status_code != 200 or data.get("errors"):
                raise RuntimeError(f"Product Hunt HTTP {response.status_code}: {str(data.get('errors') or response.text)[:200]}")
            posts = (data.get("data") or {}).get("posts") or {}
            for edge in posts.get("edges", []):
                node = edge["node"]
                post = self._launch(node, query)
                if post:
                    fetched += 1
                    yield post
                for c in ((node.get("comments") or {}).get("edges") or [])[:n_comments]:
                    comment = self._comment(c["node"], node, query)
                    if comment:
                        yield comment
                if fetched >= query.limit:
                    return
            info = posts.get("pageInfo") or {}
            if not info.get("hasNextPage"):
                return
            cursor = info.get("endCursor")

    def _launch(self, n: dict[str, Any], query: SourceQuery) -> RawPost | None:
        topics = [e["node"]["name"] for e in (n.get("topics") or {}).get("edges", [])]
        return self.make_post(
            url=n["url"],
            title=f"{n.get('name')}: {n.get('tagline')}" if n.get("tagline") else n.get("name"),
            text="\n".join(x for x in (n.get("description") or "", f"Topics: {', '.join(topics)}" if topics else "") if x),
            is_snippet=False,
            community="Product Hunt",
            published_at=parse_date(n.get("createdAt")),
            language="en",
            engagement={"votes": n.get("votesCount"), "comments": n.get("commentsCount")},
            industry_hint=query.industry_hint,
            raw={"ph_id": n.get("id"), "topics": topics, "attribution": "Product Hunt (producthunt.com)"},
        )

    def _comment(self, c: dict[str, Any], launch: dict[str, Any], query: SourceQuery) -> RawPost | None:
        if not c.get("body") or not c.get("url"):
            return None
        return self.make_post(
            source="producthunt:comments",
            source_type="community",
            url=c["url"],
            title=f"Comment on {launch.get('name')}",
            text=html_to_text(c["body"]),
            is_snippet=False,
            author_hash=hash_author((c.get("user") or {}).get("id"), "producthunt"),
            community=launch.get("name"),
            published_at=parse_date(c.get("createdAt")),
            language="en",
            engagement={"votes": c.get("votesCount")},
            industry_hint=query.industry_hint,
            raw={"ph_comment_id": c.get("id"), "launch_id": launch.get("id")},
        )
