"""Scheduled feed jobs (signal_feed/jobs.py) and the tagging age limit they rely on."""

from datetime import datetime, timedelta, timezone

from signal_feed.db import FeedCostRow, JobRunRow
from signal_feed.enrichment.pipeline import Enricher
from signal_feed.jobs import tick

T0 = datetime(2026, 10, 28, 3, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class Recorder:
    def __init__(self, fail=()):
        self.ran, self.fail = [], set(fail)

    def __call__(self, job, timeout_s):
        self.ran.append(job.name)
        return job.name not in self.fail, f"{job.name} done"


def test_free_jobs_run_when_due_and_paid_ones_need_the_flag(feed_store, feed_config):
    clock, run = Clock(T0), Recorder()
    outcomes = tick(feed_store, feed_config, paid=False, runner=run, clock=clock)
    assert run.ran == ["collect", "rescore", "nightly", "map", "weights"]
    assert {o.status for o in outcomes} == {"ok"}

    clock.t = T0 + timedelta(minutes=15)           # nothing is due again yet
    run.ran.clear()
    assert tick(feed_store, feed_config, paid=False, runner=run, clock=clock) == [] and run.ran == []

    clock.t = T0 + timedelta(minutes=58)           # hourly jobs, with slack for cron drift
    tick(feed_store, feed_config, paid=False, runner=run, clock=clock)
    assert run.ran == ["collect", "rescore"]

    clock.t = T0 + timedelta(minutes=60)
    run.ran.clear()
    tick(feed_store, feed_config, paid=True, runner=run, clock=clock)
    assert run.ran == ["tag", "embed", "build", "inferred"]       # in pipeline order, after the free ones ran


def test_a_failure_waits_for_its_next_slot_and_keeps_its_output(feed_store, feed_config):
    clock, run = Clock(T0), Recorder(fail={"nightly"})
    out = {o.job: o for o in tick(feed_store, feed_config, paid=False, runner=run, clock=clock)}
    assert out["nightly"].status == "failed" and out["map"].status == "ok"          # the others still ran
    clock.t = T0 + timedelta(hours=1)
    run.ran.clear()
    tick(feed_store, feed_config, paid=False, runner=run, clock=clock)
    assert "nightly" not in run.ran
    with feed_store.session() as s:
        row = s.query(JobRunRow).filter_by(job="nightly").one()
        assert row.status == "failed" and row.output == "nightly done"


def test_the_daily_cap_stops_paid_jobs_but_not_free_ones(feed_store, feed_config):
    with feed_store.session() as s, s.begin():
        s.add(FeedCostRow(at=T0 - timedelta(hours=2), provider="anthropic", purpose="tag", model="m", batch=False,
                          input_tokens=1, output_tokens=1, cache_read_tokens=0, cache_write_tokens=0,
                          usd=feed_config.jobs.max_daily_usd))
    run = Recorder()
    out = {o.job: o.status for o in tick(feed_store, feed_config, paid=True, runner=run, clock=Clock(T0))}
    assert out["tag"] == "skipped" and out["build"] == "skipped" and out["collect"] == "ok"
    assert "tag" not in run.ran
    # Yesterday's spend doesn't count: the next day it runs.
    run.ran.clear()
    tick(feed_store, feed_config, paid=True, runner=run, clock=Clock(T0 + timedelta(days=1)))
    assert "tag" in run.ran


def test_an_interrupted_run_is_marked_failed(feed_store, feed_config):
    with feed_store.session() as s, s.begin():
        s.add(JobRunRow(job="build", started_at=T0 - timedelta(hours=1), status="running"))
    tick(feed_store, feed_config, paid=False, runner=Recorder(), clock=Clock(T0))
    with feed_store.session() as s:
        assert s.query(JobRunRow).filter_by(job="build").one().status == "failed"


def test_escalations_are_scheduled_only_when_configured(feed_store, feed_config):
    run = Recorder()
    tick(feed_store, feed_config, paid=True, runner=run, clock=Clock(T0))
    assert "escalate" not in run.ran
    cfg = feed_config.model_copy(update={"jobs": feed_config.jobs.model_copy(update={"escalate_per_day": 50})})
    run.ran.clear()
    tick(feed_store, cfg, paid=True, runner=run, clock=Clock(T0 + timedelta(days=1)))
    assert "escalate" in run.ran


def test_tagging_can_leave_old_posts_alone(feed_store, posts, feed_config, post_ids):
    enricher = Enricher(feed_store, posts, feed_config, tagger=None)
    everything = enricher.pending_post_ids()
    assert set(post_ids) <= set(everything)
    assert enricher.pending_post_ids(published_after=datetime(2100, 1, 1, tzinfo=timezone.utc)) == []
    assert enricher.pending_post_ids(published_after=datetime(2000, 1, 1, tzinfo=timezone.utc)) == everything
