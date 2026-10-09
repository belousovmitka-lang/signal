"""Hugging Face Hub API: trending Spaces (apps people are building) and models, no key.

sources.yaml query: {params: {kind: spaces | models, sort: trendingScore}, limit: 100}
Trending lists include older items, so this reads a snapshot (filter_since = False); ids dedupe
repeats across runs. robots.txt allows all; the ToS has no clause on automated or API access.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from ..base import BaseConnector
from ..models import RawPost, SourceQuery
from ..parsing import parse_date
from . import register

API = "https://huggingface.co/api/{kind}"
WEB = {"spaces": "https://huggingface.co/spaces/{id}", "models": "https://huggingface.co/{id}"}
PAGE = 100


@register
class HuggingFaceConnector(BaseConnector):
    connector = "huggingface"
    route = "own_api"
    source_type = "startup"
    filter_since = False
    text_dedupe = False   # distinct Spaces often share the same generated text (tags only)

    async def fetch(self, query: SourceQuery) -> AsyncIterator[RawPost]:
        kind = query.params.get("kind", "spaces")
        if kind not in WEB:
            raise ValueError("huggingface kind must be spaces or models")
        params: dict[str, Any] = {"sort": query.params.get("sort", "trendingScore"), "direction": -1,
                                  "limit": min(PAGE, query.limit), "full": "true"}
        if query.keywords:
            params["search"] = query.query_text
        items = await self.http.get_json(API.format(kind=kind), params=params)
        for item in items[: query.limit]:
            post = self._to_post(item, kind, query)
            if post:
                yield post

    def _to_post(self, item: dict[str, Any], kind: str, query: SourceQuery) -> RawPost | None:
        card = item.get("cardData") or {}
        tags = [t for t in item.get("tags") or [] if not t.startswith(("region:", "license:", "arxiv:"))]
        description = card.get("short_description") or ""
        text = "\n".join(x for x in (description, f"Tags: {', '.join(tags[:15])}" if tags else "",
                                      f"Task: {item['pipeline_tag']}" if item.get("pipeline_tag") else "") if x)
        return self.make_post(
            source=f"huggingface:{kind}",
            url=WEB[kind].format(id=item["id"]),
            title=card.get("title") or item["id"],
            text=text,
            is_snippet=False,
            community=f"Hugging Face {kind}",
            published_at=parse_date(item.get("createdAt")),
            language=None,
            engagement={"likes": item.get("likes"), "trending": item.get("trendingScore"),
                        "downloads": item.get("downloads")},
            industry_hint=query.industry_hint,
            query=query.query_text,
            raw={"hf_id": item["id"], "sdk": item.get("sdk"), "pipeline_tag": item.get("pipeline_tag"),
                 "tags": tags[:30], "last_modified": item.get("lastModified")},
        )
