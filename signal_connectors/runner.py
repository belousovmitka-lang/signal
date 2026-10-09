"""Run one source end to end: build queries, fetch, store, meter, record the run.

Failures are loud in the logs and contained: a broken query never stops the next one,
and a broken source never stops the others (see run_sources).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .config import SourceConfig
from .connectors import get_connector_class
from .costs import BudgetExceeded, QuotaExceeded
from .models import FetchResult, RawPost, utcnow
from .services import Services

log = logging.getLogger(__name__)

SAVE_BATCH = 100


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
) -> RunOutput:
    started = utcnow()
    result = FetchResult(source=source.name, started_at=started)
    out = RunOutput(result)
    spent_before = services.costs.spent_this_month(source.name)

    defaults = services.sources.defaults
    if since is None:
        last = services.store.last_success(source.name) if incremental else None
        since = last or started - timedelta(days=defaults.initial_lookback_days)

    try:
        connector = get_connector_class(source.connector)(source, services)
    except Exception as exc:
        log.exception("%s: could not build connector %r", source.name, source.connector)
        result.status, result.finished_at = "failed", utcnow()
        result.errors.append(f"setup: {exc}")
        services.store.record_run(result)
        return out

    queries = source.build_queries(defaults)
    if keywords:
        queries = [q.model_copy(update={"keywords": keywords}) for q in queries[:1]]
    failed_queries = 0
    for query in queries:
        query = query.model_copy(update={"since": since, **({"limit": limit} if limit else {})})
        batch: list[RawPost] = []

        def flush() -> None:
            if save and batch:
                saved = services.store.save_posts(batch)
                result.new += saved.new
                result.duplicates += saved.duplicates
            batch.clear()

        try:
            count = 0
            agen = connector.fetch(query)
            try:
                async for post in agen:
                    if post.published_at < since:
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
        except (BudgetExceeded, QuotaExceeded) as exc:
            flush()
            log.warning("%s: stopping, %s", source.name, exc)
            result.errors.append(str(exc))
            failed_queries += 1
            break
        except Exception as exc:
            flush()
            log.exception("%s: query %s failed", source.name, query.query_text or query.communities)
            result.errors.append(f"{type(exc).__name__}: {exc}")
            failed_queries += 1

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
