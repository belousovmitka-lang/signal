"""The onboarding gap check (founder, 2026-11-08): when a new user's industries have fewer than
`opportunities.min_clusters` job clusters in the bank, search for that industry right away
(signal_connectors/targeted.py, free official APIs). The search runs in the background: onboarding
answers at once, and the found posts are tagged, scored and clustered by the scheduled feed jobs, so they
reach this user's feed, and every later user's, within the jobs' cadence.

`TargetedSearch` is what the API holds: it remembers when each industry was last searched, so many
sign-ups for one thin industry start one search per `targeted_cooldown_hours`, not one each.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Sequence

from sqlalchemy import select

from signal_connectors.industries import Industry, load_industries

from ..config import FeedConfig
from ..db import EnrichedPostRow, FeedStore, JobClusterPostRow

log = logging.getLogger(__name__)


def industry_of(label: str) -> str:
    """"Healthcare > Pharmacy" -> "Healthcare"."""
    return label.split(">")[0].strip()


def coverage(store: FeedStore, industry_names: Sequence[str]) -> dict[str, int]:
    """Job clusters per industry name: clusters with at least one core post in that industry."""
    names = {industry_of(n) for n in industry_names}
    with store.session() as s:
        rows = s.execute(select(JobClusterPostRow.cluster_id, EnrichedPostRow.industry_path).join(
            EnrichedPostRow, EnrichedPostRow.post_id == JobClusterPostRow.post_id).where(
            JobClusterPostRow.role == "core")).all()
    seen: dict[str, set[int]] = {n: set() for n in names}
    for cluster_id, path in rows:
        top = (path or [None])[0]
        if top in seen:
            seen[top].add(cluster_id)
    return {n: len(c) for n, c in seen.items()}


class TargetedSearch:
    def __init__(self, store: FeedStore, config: FeedConfig, industries: dict[str, Industry] | None = None,
                 search=None, clock=time.monotonic) -> None:
        self.store, self.config, self.clock = store, config, clock
        self.industries = {i.name: i for i in (industries or load_industries()).values()}
        self.search = search or _search
        self.last: dict[str, float] = {}
        self.lock = threading.Lock()

    def thin(self, industry_names: Sequence[str]) -> list[Industry]:
        """The named industries under `min_clusters` that have a map and weren't searched in the cooldown."""
        cfg = self.config.opportunities
        cover = coverage(self.store, industry_names)
        now, out = self.clock(), []
        with self.lock:
            for name, n in cover.items():
                ind = self.industries.get(name)
                if ind is None or n >= cfg.min_clusters:
                    continue
                if now - self.last.get(name, float("-inf")) < cfg.targeted_cooldown_hours * 3600:
                    continue
                self.last[name] = now
                out.append(ind)
        return out

    def __call__(self, industry_names: Sequence[str]) -> dict[str, int]:
        todo = self.thin(industry_names)
        if not todo:
            return {}
        cfg = self.config.opportunities
        try:
            counts = self.search(todo, cfg.targeted_days, cfg.targeted_limit)
        except Exception:
            log.exception("targeted search failed for %s", [i.industry for i in todo])
            return {}
        log.info("targeted search for %s: %s", [i.industry for i in todo], counts)
        return counts

    def start(self, industry_names: Sequence[str]) -> threading.Thread:
        """In the background: the caller (onboarding) doesn't wait."""
        t = threading.Thread(target=self, args=(list(industry_names),), name="targeted-search", daemon=True)
        t.start()
        return t


def _search(industries: list[Industry], days: int, limit: int) -> dict[str, int]:
    from signal_connectors.services import Services
    from signal_connectors.targeted import targeted_search

    async def go() -> dict[str, int]:
        async with Services.open() as services:
            return await targeted_search(services, industries, days=days, limit=limit)
    return asyncio.run(go())
