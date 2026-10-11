"""Collection health per source: last run, failure streak, recent volume, spend, stored posts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select

from .costs import CostMeter
from .models import utcnow
from .store import PostRow, RunRow, Store


@dataclass
class SourceStatus:
    name: str
    runnable: str | None          # None, or why it can't run
    last_run: datetime | None
    last_status: str | None
    failures_in_a_row: int
    new_7d: int
    runs_7d: int
    spent_month_usd: float
    last_error: str | None


def source_status(store: Store, costs: CostMeter, name: str, runnable: str | None,
                  now: datetime | None = None) -> SourceStatus:
    now = now or utcnow()
    with store.session() as s:
        runs = list(s.scalars(select(RunRow).where(RunRow.source == name).order_by(RunRow.started_at.desc()).limit(50)))
    streak = 0
    for r in runs:
        if r.status != "failed":
            break
        streak += 1
    recent = [r for r in runs if r.started_at >= now - timedelta(days=7)]
    last = runs[0] if runs else None
    return SourceStatus(
        name=name, runnable=runnable,
        last_run=last.started_at if last else None, last_status=last.status if last else None,
        failures_in_a_row=streak, new_7d=sum(r.new for r in recent), runs_7d=len(recent),
        spent_month_usd=costs.spent_this_month(name),
        last_error=(last.errors[0] if last and last.errors else None),
    )


def stored_posts_by_prefix(store: Store) -> dict[str, int]:
    """Stored posts per post source (e.g. rss:abovethelaw, forum:plctalk.net), totalled by prefix."""
    with store.session() as s:
        rows = s.execute(select(PostRow.source, func.count()).group_by(PostRow.source)).all()
    totals: dict[str, int] = {}
    for source, n in rows:
        key = source.split(":", 1)[0]
        totals[key] = totals.get(key, 0) + n
    return totals
