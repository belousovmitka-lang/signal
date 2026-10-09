"""A configurable in-memory connector for exercising the runner and smoke CLI."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta

from signal_connectors.base import BaseConnector
from signal_connectors.connectors import register
from signal_connectors.models import RawPost, SourceQuery, utcnow


@register
class FakeConnector(BaseConnector):
    connector = "fake"
    route = "own_api"
    source_type = "community"

    async def fetch(self, query: SourceQuery) -> AsyncIterator[RawPost]:
        opts = self.source.options
        if opts.get("fail_on") and opts["fail_on"] in query.keywords:
            raise RuntimeError("simulated outage")
        cost = opts.get("cost_per_call", 0.0)
        now = utcnow()
        for i in range(opts.get("count", 30)):
            with self.services.costs.metered(self.name, usd=cost):
                pass
            kw = "-".join(query.keywords) or "all"
            post = self.make_post(
                url=f"https://forum.example.org/t/{kw}/{i}?utm_source=x",
                title=f"Post {i} about {kw}",
                text=f"I spend hours every week doing {kw} manually, item number {i} of the batch.",
                is_snippet=False,
                published_at=now - timedelta(hours=i),
                engagement={"upvotes": i},
                query=query.query_text,
            )
            if post:
                yield post
        if opts.get("emit_invalid"):
            bad = self.make_post(url="not a url", text="x", is_snippet=False, published_at=now)
            if bad:
                yield bad
