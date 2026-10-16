"""Run one source end to end: build queries, fetch, store, meter, record the run.

Failures are loud in the logs and contained: a broken query never stops the next one,
and a broken source never stops the others (see run_sources).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .config import SourceConfig
from .connectors import get_connector_class
from .costs import BudgetExceeded, QuotaExceeded
from .models import FetchResult, RawPost, SourceQuery, utcnow
from .services import Services

log = logging.getLogger(__name__)

SAVE_BATCH = 100
RECHECK_WINDOW_DAYS = 30


@dataclass
class RunOutput:
    result: FetchResult
    posts: list[RawPost] = field(default_factory=list)  # filled only when collect=True


async def run_source(
    services: Services,
    source: SourceConfig,
    *,
    limit: int | None = None,
    since: datetime | None = None,
    keywords: list[str] | None = None,
    save: bool = True,
    collect: bool = False,
    incremental: bool = True,
    industry: str | None = None,
) -> RunOutput:
    """`industry` runs only that industry's queries (a first fill for a new map). The run is recorded
    as "<source>:<industry>" so the source's own schedule is untouched; cursors and costs stay the source's."""
    started = utcnow()
    result = FetchResult(source=f"{source.name}:{industry}" if industry else source.name, started_at=started)
    out = RunOutput(result)
    spent_before = services.costs.spent_this_month(source.name)

    defaults = services.sources.defaults
    lookback = started - timedelta(days=defaults.initial_lookback_days)

    try:
        connector = get_connector_class(source.connector)(source, services)
    except Exception as exc:
        log.exception("%s: could not build connector %r", source.name, source.connector)
        result.status, result.finished_at = "failed", utcnow()
        result.errors.append(f"setup: {exc}")
        services.store.record_run(result)
        return out

    queries = source.build_queries(defaults)
    if industry:
        queries = [q for q in queries if q.industry_hint == industry]
    if keywords:
        queries = [q.model_copy(update={"keywords": keywords}) for q in queries[:1]]
    failed_queries = 0
    stopped = asyncio.Event()      # budget or quota exhausted: queries not yet started are skipped

    async def run_query(query: SourceQuery) -> None:
        nonlocal failed_queries
        if stopped.is_set():
            return
        # Each query has its own cursor, so one failing feed or search never holds back the rest.
        cursor = services.store.get_cursor(source.name, query.key) if incremental else None
        query_since = since or cursor or lookback
        query = query.model_copy(update={"since": query_since, **({"limit": limit} if limit else {})})
        batch: list[RawPost] = []

        def flush() -> None:
            if save and batch:
                saved = services.store.save_posts(batch, text_dedupe=connector.text_dedupe)
                result.new += saved.new
                result.duplicates += saved.duplicates
            batch.clear()

        try:
            count = 0
            agen = connector.fetch(query)
            try:
                async for post in agen:
                    if connector.filter_since and (
                            post.published_at.date() < query_since.date() if connector.day_granular
                            else post.published_at < query_since):
                        continue
                    result.fetched += 1
                    count += 1
                    batch.append(post)
                    if collect:
                        out.posts.append(post)
                    if len(batch) >= SAVE_BATCH:
                        flush()
                    if count >= query.limit:
                        break
            finally:
                await agen.aclose()
            flush()
            if save:
                services.store.set_cursor(source.name, query.key, started)
        except (BudgetExceeded, QuotaExceeded) as exc:
            flush()
            if not stopped.is_set():
                log.warning("%s: stopping, %s", source.name, exc)
                result.errors.append(str(exc))
            stopped.set()
            failed_queries += 1
        except Exception as exc:
            flush()
            log.exception("%s: query %s failed", source.name, query.query_text or query.communities)
            result.errors.append(f"{type(exc).__name__}: {exc}")
            failed_queries += 1

    width = max(1, connector.parallel_queries)
    if width == 1:
        for q in queries:
            await run_query(q)
    else:
        gate = asyncio.Semaphore(width)

        async def gated(q: SourceQuery) -> None:
            async with gate:
                await run_query(q)

        await asyncio.gather(*(gated(q) for q in queries))

    result.invalid = connector.invalid
    result.cost_usd = round(services.costs.spent_this_month(source.name) - spent_before, 6)
    if failed_queries == 0:
        result.status = "ok"
    elif failed_queries < len(queries) or result.fetched:
        result.status = "partial"
    else:
        result.status = "failed"
    result.finished_at = utcnow()
    services.store.record_run(result)
    log.info("%s: %s fetched=%d new=%d dup=%d invalid=%d cost=$%.4f", source.name, result.status,
             result.fetched, result.new, result.duplicates, result.invalid, result.cost_usd)
    return out


async def recheck_deletions(services: Services, source: SourceConfig) -> int:
    """Purge stored posts their platform no longer serves (Bluesky guideline 3; Reddit/X terms).

    The connector must implement `deleted(urls) -> list[str]`. Returns the number purged.
    """
    connector = get_connector_class(source.connector)(source, services)
    deleted_fn = getattr(connector, "deleted", None)
    if deleted_fn is None:
        return 0
    post_source = source.options.get("post_source", source.name)
    # Deletions mostly happen soon after posting: check the newest posts of the recent window first,
    # capped per run so a slow API (Lemmy's 60s crawl delay) can't stall the scheduler.
    window = int(source.options.get("recheck_window_days", RECHECK_WINDOW_DAYS))
    rows = services.store.ids_for_recheck(post_source, utcnow() - timedelta(days=window),
                                          limit=source.deletion_recheck_limit)
    by_url = {url: post_id for post_id, url in rows}
    gone = await deleted_fn(list(by_url))
    purged = services.store.delete_posts([by_url[u] for u in gone if u in by_url])
    log.info("%s: re-checked %d posts, purged %d", source.name, len(rows), purged)
    return purged


async def run_sources(services: Services, sources: list[SourceConfig]) -> list[FetchResult]:
    """Run several sources concurrently; each is isolated from the others' failures."""

    async def one(s: SourceConfig) -> FetchResult:
        try:
            return (await run_source(services, s)).result
        except Exception as exc:  # run_source already contains failures; this is a last guard
            log.exception("%s: crashed", s.name)
            return FetchResult(source=s.name, started_at=utcnow(), finished_at=utcnow(),
                               status="failed", errors=[str(exc)])

    return list(await asyncio.gather(*(one(s) for s in sources)))
