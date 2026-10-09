"""Cost meter and budget guard (rule 6).

Every paid call goes through `CostMeter.metered(...)`, which refuses the call when it would
take the source past `monthly_budget_usd` in sources.yaml, and records it in the ledger
afterwards. Sources without a budget get 0, so they can make free calls only.

Quota-limited free APIs (YouTube units per day, Threads queries per 7 days) use the same
ledger through `units` and `ensure_quota`.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from .models import utcnow
from .store import Store

log = logging.getLogger(__name__)

EPSILON = 1e-9


class BudgetExceeded(Exception):
    pass


class QuotaExceeded(Exception):
    pass


class Charge:
    def __init__(self, usd: float) -> None:
        self.usd = usd


def month_bounds(now: datetime) -> tuple[datetime, datetime]:
    start = now.astimezone(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    return start, end


class CostMeter:
    def __init__(self, store: Store, budgets: Mapping[str, float],
                 provider_budgets: Mapping[str, float] | None = None) -> None:
        self.store = store
        self.budgets = dict(budgets)
        # Caps on a paid API shared by several sources (e.g. Brave's free credit), summed across them.
        self.provider_budgets = dict(provider_budgets or {})

    def budget(self, source: str) -> float:
        return self.budgets.get(source, 0.0)

    def spent_this_month(self, source: str, now: datetime | None = None) -> float:
        start, end = month_bounds(now or utcnow())
        return self.store.cost_totals(start, end, source).get(source, {}).get("usd", 0.0)

    def remaining(self, source: str, now: datetime | None = None) -> float:
        return max(0.0, self.budget(source) - self.spent_this_month(source, now))

    def provider_spent_this_month(self, provider: str, now: datetime | None = None) -> float:
        return self.store.provider_spend(provider, *month_bounds(now or utcnow()))

    def provider_remaining(self, provider: str) -> float:
        return max(0.0, self.provider_budgets.get(provider, 0.0) - self.provider_spent_this_month(provider))

    def ensure_provider_budget(self, provider: str, estimated_usd: float) -> None:
        if estimated_usd <= 0:
            return
        spent = self.provider_spent_this_month(provider)
        budget = self.provider_budgets.get(provider, 0.0)
        if spent + estimated_usd > budget + EPSILON:
            raise BudgetExceeded(f"provider {provider}: ${spent:.4f} spent of ${budget:.2f} this month; "
                                 f"refusing a ${estimated_usd:.4f} call")

    def ensure_budget(self, source: str, estimated_usd: float) -> None:
        if estimated_usd <= 0:
            return
        spent = self.spent_this_month(source)
        budget = self.budget(source)
        if spent + estimated_usd > budget + EPSILON:
            raise BudgetExceeded(
                f"{source}: ${spent:.4f} spent of ${budget:.2f} this month; refusing a ${estimated_usd:.4f} call")

    def units_used(self, source: str, window: timedelta, now: datetime | None = None) -> float:
        now = now or utcnow()
        return self.store.cost_totals(now - window, now + timedelta(seconds=1), source).get(source, {}).get("units", 0.0)

    def ensure_quota(self, source: str, units: float, limit: float, window: timedelta) -> None:
        used = self.units_used(source, window)
        if used + units > limit:
            raise QuotaExceeded(f"{source}: {used:.0f}/{limit:.0f} units used in the last {window}; need {units:.0f}")

    def record(self, source: str, usd: float = 0.0, units: float = 0.0, calls: int = 1,
               note: str | None = None, provider: str | None = None) -> None:
        self.store.add_cost_event(source, utcnow(), usd, units, calls, note, provider)

    @contextmanager
    def metered(self, source: str, usd: float = 0.0, units: float = 0.0, note: str | None = None,
                quota: tuple[float, timedelta] | None = None,
                provider: str | None = None) -> Iterator[Charge]:
        """Guard and record one call. The call is recorded even if it raises, since it may still be billed.

        `usd` is the estimate used for the guard; set `charge.usd` inside the block to record the
        actual cost when the API reports it.
        """
        self.ensure_budget(source, usd)
        if provider:
            self.ensure_provider_budget(provider, usd)
        if quota:
            self.ensure_quota(source, units, *quota)
        charge = Charge(usd)
        try:
            yield charge
        finally:
            self.record(source, usd=charge.usd, units=units, note=note, provider=provider)

    def ledger(self, now: datetime | None = None) -> dict[str, dict[str, float]]:
        """This month's totals per source, with budget and remaining."""
        start, end = month_bounds(now or utcnow())
        totals = self.store.cost_totals(start, end)
        for name in set(totals) | set(self.budgets):
            row = totals.setdefault(name, {"usd": 0.0, "units": 0.0, "calls": 0.0})
            row["budget"] = self.budget(name)
            row["remaining"] = max(0.0, row["budget"] - row["usd"])
        return totals
