"""Bundle of shared services handed to every connector."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx

from .base import HttpClient
from .config import SourcesFile, load_sources
from .costs import CostMeter
from .robots import RobotsChecker
from .settings import Settings
from .store import Store


@dataclass
class Services:
    settings: Settings
    sources: SourcesFile
    http: HttpClient
    store: Store
    costs: CostMeter
    robots: RobotsChecker

    @classmethod
    @asynccontextmanager
    async def open(
        cls,
        settings: Settings | None = None,
        sources: SourcesFile | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        backoff_initial: float = 1.0,
    ) -> AsyncIterator[Services]:
        settings = settings or Settings.from_env()
        sources = sources or load_sources(settings.sources_file)
        d = sources.defaults
        http = HttpClient(
            user_agent=settings.user_agent,
            default_rps=d.requests_per_second,
            timeout=d.timeout_seconds,
            max_attempts=d.max_attempts,
            backoff_initial=backoff_initial,
            transport=transport,
        )
        for source in sources.sources.values():
            if source.rate_limit:
                for domain in source.rate_limit.domains:
                    http.limiter.set_rate(domain, source.rate_limit.requests_per_second)
        store = Store(settings.database_url)
        try:
            yield cls(
                settings=settings,
                sources=sources,
                http=http,
                store=store,
                costs=CostMeter(store, sources.budgets()),
                robots=RobotsChecker(http),
            )
        finally:
            await http.aclose()
            store.engine.dispose()
