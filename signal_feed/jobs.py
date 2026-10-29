"""Scheduled feed jobs (FEED_ALGORITHM.md section 10), run by scripts/feed_jobs.py from cron.

Each tick runs the jobs that are due, in pipeline order, one at a time. A job is due one interval after its
last run of any status (5 minutes' slack), so a failure waits for the next slot instead of retrying every tick.
Each job is its own script in a subprocess: one failing job never stops the others, and its last lines are
kept in `job_runs`.

Paid jobs (Claude, Voyage) run only when the tick is started with `paid=True`, and none starts once today's
spend (all providers, plus open batch reservations) reaches `jobs.max_daily_usd`. The cap is checked before a
job starts, so one job can go past it; each paid script also keeps its own monthly budget. A paid job skipped
by the cap records nothing, so it runs on the next tick that has room.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select

from signal_connectors.models import utcnow

from .config import FeedConfig
from .db import FeedStore, JobRunRow
from .enrichment.costs import CostLedger
from .settings import REPO_ROOT

HOUR, DAY, WEEK = timedelta(hours=1), timedelta(days=1), timedelta(days=7)
SLACK = timedelta(minutes=5)          # cron ticks drift; a job run at 03:01 is due again at 03:00 the next day
OUTPUT_CHARS = 4000


@dataclass(frozen=True)
class Job:
    name: str
    every: timedelta
    argv: tuple[str, ...]             # a script under scripts/ and its arguments
    paid: bool = False


def jobs(config: FeedConfig) -> list[Job]:
    """Every job in the order a tick runs them: batches finish before tagging, tagging before embedding,
    the weekly tree build before the map job and the detectors that read it."""
    j = config.jobs
    out = [
        Job("collect", HOUR, ("enrich.py", "collect")),                            # finish batches, no paid calls
        Job("tag", DAY, ("enrich.py", "run", "--max-age-days", str(j.tag_max_age_days),
                         "--limit", str(j.tag_max_posts)), paid=True),
        Job("embed", HOUR, ("enrich.py", "embed"), paid=True),                      # also learns new clips
        Job("headlines", DAY, ("feed_text.py", "headlines", "--limit", "2000"), paid=True),
        Job("rescore", HOUR, ("problems.py", "rescore")),
        Job("nightly", DAY, ("feed_nightly.py",)),
        Job("build", WEEK, ("problems.py", "build"), paid=True),
        Job("map", WEEK, ("map_weekly.py",)),
        Job("inferred", WEEK, ("inferred.py", "run"), paid=True),
        Job("weights", WEEK, ("inferred.py", "weights")),
    ]
    if j.escalate_per_day:
        out.insert(2, Job("escalate", DAY, ("enrich.py", "escalate", "--limit", str(j.escalate_per_day)), paid=True))
    return out


@dataclass
class Outcome:
    job: str
    status: str                       # ok | failed | skipped
    detail: str = ""


Runner = Callable[[Job, float], tuple[bool, str]]


def run_script(job: Job, timeout_s: float) -> tuple[bool, str]:
    try:
        p = subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / job.argv[0]), *job.argv[1:]],
                           cwd=REPO_ROOT, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or b"").decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        return False, f"{out}\ntimed out after {timeout_s / 60:.0f} minutes"
    lines = [ln for ln in (p.stdout + p.stderr).splitlines() if "HTTP Request:" not in ln]
    return p.returncode == 0, "\n".join(lines)


def last_runs(store: FeedStore) -> dict[str, JobRunRow]:
    with store.session() as s:
        rows = s.scalars(select(JobRunRow).order_by(JobRunRow.started_at))
        return {r.job: r for r in rows}


def due(job: Job, last: JobRunRow | None, now: datetime) -> bool:
    return last is None or now - last.started_at >= job.every - SLACK


def tick(store: FeedStore, config: FeedConfig, *, paid: bool, only: set[str] | None = None,
         runner: Runner = run_script, clock: Callable[[], datetime] = utcnow) -> list[Outcome]:
    """Run every due job once. The caller holds the lock, so a run still marked `running` was interrupted."""
    with store.session() as s, s.begin():
        for r in s.scalars(select(JobRunRow).where(JobRunRow.status == "running")):
            r.status, r.finished_at = "failed", r.finished_at or clock()
            r.output = (r.output or "") + "\ninterrupted (the scheduler stopped before the job finished)"
    ledger, cap, out = CostLedger(store), config.jobs.max_daily_usd, []
    last = last_runs(store)
    for job in jobs(config):
        now = clock()
        if (only and job.name not in only) or not due(job, last.get(job.name), now):
            continue
        if job.paid and not paid:          # silent: a free-only crontab would log this every tick
            continue
        if job.paid:
            spent = ledger.spend_since(now.replace(hour=0, minute=0, second=0, microsecond=0))
            if spent >= cap:
                out.append(Outcome(job.name, "skipped", f"today's spend ${spent:.2f} reached the ${cap:.2f} cap"))
                continue
        with store.session() as s, s.begin():
            row = JobRunRow(job=job.name, started_at=now, status="running")
            s.add(row)
        ok, text = runner(job, config.jobs.job_timeout_minutes * 60)
        tail = text[-OUTPUT_CHARS:]
        with store.session() as s, s.begin():
            r = s.get(JobRunRow, row.id)
            r.status, r.finished_at, r.output = ("ok" if ok else "failed"), clock(), tail
        out.append(Outcome(job.name, "ok" if ok else "failed", tail.strip().splitlines()[-1] if tail.strip() else ""))
    return out
