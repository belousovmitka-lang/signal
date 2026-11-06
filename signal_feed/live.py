"""The item page's live results: signal_connectors/live.py searches, shown at view time and never stored.

One site-limited Brave search for Reddit (plus Amazon Seller Forums for e-commerce) and one YouTube Data
API search per industry and topic: the industry's main role and the item's second-level niche ("gym owner"
+ "class scheduling and booking"). Facebook groups returned nothing for topic queries (2026-11-06) and
Meetup groups aren't about one problem, so the item page skips both. Results stay in memory for 15 minutes
(Brave 3(b)(i) transient storage, in signal_connectors/live.py), so problems in the same niche share one
search. Brave spend is capped by `defaults.provider_budgets.brave` in sources.yaml (just under the $5
monthly free credit), YouTube by its own daily search bucket; once either is used up its section is empty.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from contextlib import AsyncExitStack

from signal_connectors.industries import Industry, load_industries
from signal_connectors.live import INDUSTRY_PLATFORMS, live_search
from signal_connectors.services import Services

log = logging.getLogger(__name__)

PLATFORMS = ["reddit", "youtube"]
LIMIT = 5
TAG = re.compile(r"<[^>]+>")


def _text(s: str | None) -> str:
    """Providers mark matches with <strong>: shown as plain text (never rendered as HTML)."""
    return html.unescape(TAG.sub("", s or "")).strip()


class LiveLane:
    def __init__(self, industries: dict[str, Industry] | None = None, services: Services | None = None):
        self._industries = {i.name: i for i in (industries or load_industries()).values()}
        self._services, self._stack, self._lock = services, None, asyncio.Lock()

    async def _open(self) -> Services:
        async with self._lock:
            if self._services is None:
                self._stack = AsyncExitStack()
                self._services = await self._stack.enter_async_context(Services.open())
        return self._services

    async def __call__(self, industry_name: str, topic: str | None) -> dict:
        industry = self._industries.get(industry_name)
        if industry is None:                       # "Other" and niches with no industry map: nothing to ask
            return {"sections": []}
        try:
            results = await live_search(await self._open(), industry, extra=topic, limit=LIMIT,
                                        platforms=PLATFORMS + INDUSTRY_PLATFORMS.get(industry.industry, []))
        except Exception:
            log.exception("live lane failed for %s / %s", industry_name, topic)
            return {"sections": []}
        sections = []
        for s in results.sections:
            if s.error:
                log.warning("live %s: %s", s.platform, s.error)
            if s.items:
                sections.append({"platform": s.platform, "label": s.label, "provider": s.provider, "items": [
                    {"title": _text(i.title), "url": i.url, "snippet": _text(i.snippet), "age": i.age,
                     "channel": i.channel, "thumbnail": i.thumbnail} for i in s.items]})
        return {"sections": sections}
