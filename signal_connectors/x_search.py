"""On-demand, stored X search for the feed's inferred problems ("who is affected, in their own words").

    from signal_connectors.x_search import search_x, XSearchDisabled, XBudgetReached, XSearchError
    posts = search_x('"hose assemblies" (manually OR "by hand")', max_results=10,
                     industry_hint="manufacturing", tag="inferred:<key>")

Synchronous (runs its own event loop, or a worker thread when called inside one). Builds Services from
.env and sources.yaml (`x` entry), searches X's last 7 days, stores new posts in signal.db through the
normal store path (dedupe on id, then text) and returns the stored RawPosts, including ones stored
before. Posts that were text-duplicates of a different stored post aren't returned.

Refuses with an exception the caller can catch, before any paid call:
- XSearchDisabled: the `x` source is not enabled (until X approves the stored use case, see sources.yaml)
- XBudgetReached: the month's `monthly_budget_usd` for `x` would be exceeded (a BudgetExceeded)
- XSearchError: X refused the search (auth, credits, malformed query)

`dry_run=True` needs no network, key or budget and writes nothing: it parses a canned response
(DRY_RUN_RESPONSE) through the same code and returns the posts, for wiring up callers.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from typing import Any

import httpx

from .config import SourcesFile, load_sources
from .connectors import get_connector_class
from .connectors.x import XSearchError
from .costs import BudgetExceeded
from .models import FetchResult, RawPost, SourceQuery, utcnow
from .services import Services
from .settings import Settings

__all__ = ["search_x", "asearch_x", "XSearchDisabled", "XBudgetReached", "XSearchError", "DRY_RUN_RESPONSE"]

SOURCE = "x"


class XSearchDisabled(RuntimeError):
    pass


class XBudgetReached(BudgetExceeded):
    pass


DRY_RUN_RESPONSE: dict[str, Any] = {
    "data": [
        {"id": "1900000000000000001", "author_id": "100001", "lang": "en", "conversation_id": "1900000000000000001",
         "created_at": "2026-10-14T15:02:11.000Z",
         "text": "I'm a dispatcher and I spent all morning re-keying load confirmations by hand again. There has to be a better way",
         "public_metrics": {"like_count": 4, "reply_count": 2, "retweet_count": 0, "quote_count": 0}},
        {"id": "1900000000000000002", "author_id": "100002", "lang": "en", "conversation_id": "1900000000000000002",
         "created_at": "2026-10-15T09:40:00.000Z",
         "text": "Every Friday: export from the TMS, paste into a spreadsheet, email the broker. Two hours gone.",
         "public_metrics": {"like_count": 11, "reply_count": 5, "retweet_count": 1, "quote_count": 0}},
    ],
    "meta": {"result_count": 2},
}


def _dry_transport() -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: httpx.Response(200, json=DRY_RUN_RESPONSE))


async def asearch_x(query: str, *, max_results: int = 10, industry_hint: str | None = None,
                    tag: str | None = None, dry_run: bool = False,
                    services: Services | None = None) -> list[RawPost]:
    if services is None:
        settings = Settings.from_env()
        if dry_run:
            settings = Settings(database_url="sqlite:///:memory:", contact_email=settings.contact_email,
                                sources_file=settings.sources_file)
        sources: SourcesFile = load_sources(settings.sources_file)
        async with Services.open(settings, sources, transport=_dry_transport() if dry_run else None) as svc:
            return await _search(svc, query, max_results, industry_hint, tag, dry_run)
    return await _search(services, query, max_results, industry_hint, tag, dry_run)


async def _search(services: Services, query: str, max_results: int, industry_hint: str | None,
                  tag: str | None, dry_run: bool) -> list[RawPost]:
    source = services.sources.get(SOURCE)
    reason = source.blocked_reason()
    if reason and not dry_run:
        raise XSearchDisabled(f"X search is off: {reason}")
    if not query or not query.strip():
        raise ValueError("search_x needs a query")
    connector = get_connector_class(source.connector)(source, services)
    if dry_run:
        services.costs.budgets[SOURCE] = float("inf")
        connector._headers = lambda: {"Authorization": "Bearer dry-run"}   # type: ignore[method-assign]
    q = SourceQuery(params={"q": query.strip(), "tag": tag}, limit=max(1, max_results), industry_hint=industry_hint)
    result = FetchResult(source=SOURCE, started_at=utcnow())
    posts: list[RawPost] = []
    try:
        async for post in connector.fetch(q):
            posts.append(post)
    except BudgetExceeded as exc:
        _record(services, result, "failed", str(exc), dry_run)
        raise XBudgetReached(str(exc)) from None
    except Exception as exc:
        _record(services, result, "failed", f"{type(exc).__name__}: {exc}", dry_run)
        raise
    posts = posts[:max_results]
    if dry_run:
        return posts
    saved = services.store.save_posts(posts, text_dedupe=connector.text_dedupe)
    result.fetched, result.new, result.duplicates, result.invalid = len(posts), saved.new, saved.duplicates, connector.invalid
    _record(services, result, "ok", None, dry_run)
    stored = [services.store.get_post(p.id) for p in posts]
    return [p for p in stored if p is not None]


def _record(services: Services, result: FetchResult, status: str, error: str | None, dry_run: bool) -> None:
    if dry_run:
        return
    result.status, result.finished_at = status, utcnow()
    if error:
        result.errors.append(error)
    services.store.record_run(result)


def search_x(query: str, *, max_results: int = 10, industry_hint: str | None = None, tag: str | None = None,
             dry_run: bool = False) -> list[RawPost]:
    coro = asearch_x(query, max_results=max_results, industry_hint=industry_hint, tag=tag, dry_run=dry_run)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:   # called from inside a loop
        return pool.submit(asyncio.run, coro).result()
