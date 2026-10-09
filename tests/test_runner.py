from datetime import timedelta

from signal_connectors.models import utcnow
from signal_connectors.runner import run_source, run_sources
from signal_connectors.smoke import main as smoke_main

from . import fakes  # noqa: F401  registers the "fake" connector


def fake(name="fake", **options):
    return {"connector": "fake", "route": "own_api", "source_type": "community", "enabled": True,
            "monthly_budget_usd": options.pop("budget", 0),
            "queries": options.pop("queries", [{"keywords": ["intake"]}]), "options": options}


async def test_run_stores_and_is_idempotent(make_services):
    async with make_services({"sources": {"fake": fake()}}) as svc:
        src = svc.sources.get("fake")
        r1 = (await run_source(svc, src)).result
        assert (r1.status, r1.fetched, r1.new) == ("ok", 30, 30)
        r2 = (await run_source(svc, src, incremental=False)).result
        assert (r2.new, r2.duplicates) == (0, 30)
        assert svc.store.count_posts("fake") == 30
        assert svc.store.last_success("fake") == r2.started_at


async def test_limit_and_since(make_services):
    async with make_services({"sources": {"fake": fake()}}) as svc:
        src = svc.sources.get("fake")
        assert (await run_source(svc, src, limit=5)).result.fetched == 5
        r = (await run_source(svc, src, since=utcnow() - timedelta(hours=9, minutes=30), incremental=False)).result
        assert r.fetched == 10  # posts are 0..29 hours old


async def test_one_failing_query_does_not_stop_the_others(make_services):
    cfg = fake(fail_on="bad", queries=[{"keywords": ["bad"]}, {"keywords": ["good"]}])
    async with make_services({"sources": {"fake": cfg}}) as svc:
        r = (await run_source(svc, svc.sources.get("fake"))).result
    assert r.status == "partial" and r.new == 30 and "simulated outage" in r.errors[0]


async def test_invalid_items_are_counted_not_fatal(make_services):
    async with make_services({"sources": {"fake": fake(emit_invalid=True, count=3)}}) as svc:
        r = (await run_source(svc, svc.sources.get("fake"))).result
    assert (r.status, r.new, r.invalid) == ("ok", 3, 1)


async def test_budget_stops_paid_calls(make_services):
    async with make_services({"sources": {"fake": fake(cost_per_call=0.005, budget=0.05)}}) as svc:
        r = (await run_source(svc, svc.sources.get("fake"))).result
        assert r.new == 10 and r.cost_usd == 0.05
        assert "refusing" in r.errors[0]
        assert svc.costs.remaining("fake") == 0


async def test_missing_connector_fails_that_source_only(make_services):
    sources = {"sources": {"ghost": {"route": "own_api", "source_type": "news"}, "fake": fake()}}
    async with make_services(sources) as svc:
        results = await run_sources(svc, [svc.sources.get("ghost"), svc.sources.get("fake")])
    assert [r.status for r in results] == ["failed", "ok"]
    assert "no connector module" in results[0].errors[0]


def test_smoke_cli(settings, tmp_path, capsys):
    settings.sources_file.write_text(
        "sources:\n  fake:\n    connector: fake\n    route: own_api\n    source_type: community\n"
        "    queries: [{keywords: [intake]}]\n")
    assert smoke_main(["fake", "--limit", "25"], settings=settings) == 0
    out = capsys.readouterr().out
    assert "fetched=25 new=25" in out and "https://forum.example.org/t/intake/0" in out
    assert smoke_main(["fake", "--limit", "25", "--min", "26"], settings=settings) == 1  # capped, not dry
    settings.sources_file.write_text(settings.sources_file.read_text() + "    options: {count: 7}\n")
    assert smoke_main(["fake"], settings=settings) == 0  # ran dry at 7 with no errors
    assert smoke_main(["--list"], settings=settings) == 0
    assert "runnable" in capsys.readouterr().out


async def test_cursors_are_per_query(make_services):
    cfg = fake(fail_on="bad", queries=[{"keywords": ["bad"]}, {"keywords": ["good"]}])
    async with make_services({"sources": {"fake": cfg}}) as svc:
        src = svc.sources.get("fake")
        first = (await run_source(svc, src)).result
        good, bad = (q.key for q in reversed(src.build_queries(svc.sources.defaults)))
        assert svc.store.get_cursor("fake", good) == first.started_at
        assert svc.store.get_cursor("fake", bad) is None  # failed: retried over the same window
        second = (await run_source(svc, src)).result
        # good query resumes from its cursor: only the post stamped after the first run comes back
        assert second.fetched <= 1 and second.new == 0
        assert svc.store.get_cursor("fake", good) == second.started_at


def test_query_key_ignores_since_and_limit():
    from signal_connectors.models import SourceQuery
    a = SourceQuery(keywords=["x"], limit=5)
    assert a.key == SourceQuery(keywords=["x"], limit=50, since=utcnow()).key != SourceQuery(keywords=["y"]).key
