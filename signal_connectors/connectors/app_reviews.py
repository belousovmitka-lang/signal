"""G10: app and plugin marketplace reviews.

Live today: WordPress.org plugins, through each plugin's review and support-forum RSS feeds
(wordpress.org/support/plugin/<slug>/reviews/feed/ and .../feed/). The plugin info API at
api.wordpress.org is disallowed by its robots.txt, so it isn't used.

Checked 2026-10-09 and not usable: Apple App Store reviews feed (robots.txt disallows /*/rss/*),
Chrome Web Store and Atlassian Marketplace REST API (robots.txt disallows), Shopify App Store
(Shopify ToS forbids robots and scrapers).

sources.yaml query: {industry_hint: education, params: {plugin: tutor, kind: reviews}}
kind is "reviews" (default) or "support".
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Any

from ..models import RawPost, SourceQuery
from . import register
from .rss import RssConnector

FEEDS = {
    "reviews": "https://wordpress.org/support/plugin/{slug}/reviews/feed/",
    "support": "https://wordpress.org/support/plugin/{slug}/feed/",
}
_RATING = re.compile(r"Rating:\s*(\d)\s*stars?", re.I)
_REPLIES = re.compile(r"Replies:\s*(\d+)", re.I)
_TITLE_STARS = re.compile(r"\s*\(\d stars?\)\s*$", re.I)


@register
class AppReviewsConnector(RssConnector):
    connector = "app_reviews"
    route = "rss"
    source_type = "review"

    async def fetch(self, query: SourceQuery) -> AsyncIterator[RawPost]:
        slug = query.params.get("plugin")
        kind = query.params.get("kind", "reviews")
        if not slug or kind not in FEEDS:
            raise ValueError("app_reviews query needs params.plugin and kind reviews|support")
        params = {**query.params, "feed": FEEDS[kind].format(slug=slug), "name": f"wordpress:{slug}"}
        async for post in super().fetch(query.model_copy(update={"params": params})):
            yield post

    def adapt(self, fields: dict[str, Any], entry: Any, query: SourceQuery) -> dict[str, Any]:
        slug, kind = query.params["plugin"], query.params.get("kind", "reviews")
        text = fields["text"]
        engagement: dict[str, int] = {}
        if m := _RATING.search(text):
            engagement["rating"] = int(m.group(1))
        if m := _REPLIES.search(text):
            engagement["replies"] = int(m.group(1))
        body = "\n".join(line for line in text.split("\n")
                         if not _RATING.fullmatch(line.strip()) and not _REPLIES.fullmatch(line.strip())).strip()
        return {
            **fields,
            "source": f"wordpress:{slug}" + ("" if kind == "reviews" else ":support"),
            "source_type": "review" if kind == "reviews" else "community",
            "title": _TITLE_STARS.sub("", fields["title"] or "") or None,
            "text": body,
            "community": f"WordPress plugin: {slug}",
            "engagement": engagement,
            "raw": {**fields["raw"], "plugin": slug, "kind": kind},
        }
