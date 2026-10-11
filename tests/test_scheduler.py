import asyncio
from datetime import timedelta

import httpx

from signal_connectors.models import FetchResult, utcnow
from signal_connectors.scheduler import plan, run_due

from . import fakes  # noqa: F401  registers the "fake" connector


def fake(cadence="daily", **options):
    return {"connector": "fake", "route": "own_api", "source_type": "community", "enabled": True,
            "cadence": cadence, "queries": [{"keywords": ["x"]}], "options": {"count": 3, **options}}


async def test_due_logic_with_slack(make_services):
    cfg = {"sources": {"daily": fake(), "hourly": fake("hourly"), "weekly": fake("weekly"),
                       "off": {**fake(), "enabled": False}}}
    async with make_services(cfg) as svc:
        now = utcnow()
        assert {s.name for s in plan(svc, now).runs} == {"daily", "hourly", "weekly"}   # never ran
        for name, ago in [("daily", timedelta(hours=23, minutes=57)), ("hourly", timedelta(minutes=30)),
                          ("weekly", timedelta(days=8))]:
            svc.store.record_run(FetchResult(source=name, started_at=now - ago, status="failed"))
        p = plan(svc, now)
        assert {s.name for s in p.runs} == {"daily", "weekly"}    # daily is within the 5-minute slack
        assert p.skipped == {"off": "not enabled in sources.yaml"}


async def test_failures_are_isolated_and_rechecks_recorded(make_services):
    cfg = {"sources": {"good": fake(), "bad": fake(fail_on="x"),
                       "social": {**fake(), "deletion_recheck_days": 7}}}
    async with make_services(cfg) as svc:
        results, checks, _ = await run_due(svc)
        assert {r.source: r.status for r in results} == {"good": "ok", "bad": "failed", "social": "ok"}
        assert [c.source for c in checks] == ["social:recheck"] and checks[0].status == "ok"
        # nothing is due again right away, including the re-check
        p = plan(svc)
        assert p.runs == [] and p.rechecks == []


async def test_concurrency_is_bounded(make_services, monkeypatch):
    import signal_connectors.scheduler as sched
    running, peak = 0, 0

    async def slow_run(services, source):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.02)
        running -= 1

        class Out:
            result = FetchResult(source=source.name, started_at=utcnow())
        return Out()

    monkeypatch.setattr(sched, "run_source", slow_run)
    cfg = {"sources": {f"s{i}": fake() for i in range(8)}}
    async with make_services(cfg) as svc:
        results, _, _ = await run_due(svc, concurrency=3)
    assert len(results) == 8 and peak == 3


async def test_failed_recheck_retries_after_a_day(make_services):
    cfg = {"sources": {"social": {**fake(), "deletion_recheck_days": 7}}}
    async with make_services(cfg) as svc:
        now = utcnow()
        svc.store.record_run(FetchResult(source="social", started_at=now, status="ok"))
        svc.store.record_run(FetchResult(source="social:recheck", started_at=now - timedelta(days=1, minutes=1),
                                         status="failed"))
        assert [s.name for s in plan(svc, now).rechecks] == ["social"]
        svc.store.record_run(FetchResult(source="social:recheck", started_at=now - timedelta(hours=2), status="ok"))
        assert plan(svc, now).rechecks == []
