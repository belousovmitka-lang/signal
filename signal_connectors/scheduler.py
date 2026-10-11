"""Run whatever is due: each runnable source by its cadence, plus deletion re-checks.

A source is due when its last run (any status) started at least one cadence interval ago, so a
failing source waits for its next slot instead of retrying in a loop. Sources run concurrently
(bounded); per-domain rate limits keep shared hosts polite. Deletion re-checks run every
`deletion_recheck_days` per source and are recorded as runs named "<source>:recheck".
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from .config import SourceConfig
from .models import FetchResult, utcnow
from .runner import recheck_deletions, run_source
from .services import Services

log = logging.getLogger(__name__)

INTERVALS = {"hourly": timedelta(hours=1), "daily": timedelta(days=1), "weekly": timedelta(days=7),
             "monthly": timedelta(days=30)}
SLACK = timedelta(minutes=5)       # a daily cron at 06:00 shouldn't miss a run that started at 06:02


@dataclass
class Plan:
    runs: list[SourceConfig]
    rechecks: list[SourceConfig]
    skipped: dict[str, str]          # source -> why it can't run


def plan(services: Services, now: datetime | None = None, only: set[str] | None = None) -> Plan:
    now = now or utcnow()
    runs, rechecks, skipped = [], [], {}
    for name, source in services.sources.sources.items():
        if only and name not in only:
            continue
        reason = source.blocked_reason()
        if reason:
            skipped[name] = reason
            continue
        last = services.store.last_run(name)
        if last is None or now - last >= INTERVALS[source.cadence] - SLACK:
            runs.append(source)
        if source.deletion_recheck_days:
            last_check = services.store.last_run(f"{name}:recheck")
            if last_check is None or now - last_check >= timedelta(days=source.deletion_recheck_days) - SLACK:
                rechecks.append(source)
    return Plan(runs, rechecks, skipped)


async def _recheck(services: Services, source: SourceConfig) -> FetchResult:
    result = FetchResult(source=f"{source.name}:recheck", started_at=utcnow())
    try:
        result.duplicates = await recheck_deletions(services, source)   # purged count
    except Exception as exc:
        log.exception("%s: deletion re-check failed", source.name)
        result.status, result.errors = "failed", [f"{type(exc).__name__}: {exc}"]
    result.finished_at = utcnow()
    services.store.record_run(result)
    return result


async def run_due(services: Services, *, only: set[str] | None = None, concurrency: int = 6,
                  now: datetime | None = None) -> tuple[list[FetchResult], list[FetchResult], Plan]:
    p = plan(services, now, only)
    gate = asyncio.Semaphore(concurrency)

    async def one(source: SourceConfig) -> FetchResult:
        async with gate:
            try:
                return (await run_source(services, source)).result
            except Exception as exc:     # run_source contains failures; this is a last guard
                log.exception("%s: crashed", source.name)
                return FetchResult(source=source.name, started_at=utcnow(), finished_at=utcnow(),
                                   status="failed", errors=[str(exc)])

    results = list(await asyncio.gather(*(one(s) for s in p.runs)))
    checks = [await _recheck(services, s) for s in p.rechecks]
    return results, checks, p
