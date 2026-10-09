from datetime import datetime, timedelta, timezone

import pytest

from signal_connectors.costs import BudgetExceeded, CostMeter, QuotaExceeded, month_bounds


def test_month_bounds():
    assert month_bounds(datetime(2026, 12, 15, tzinfo=timezone.utc)) == (
        datetime(2026, 12, 1, tzinfo=timezone.utc), datetime(2027, 1, 1, tzinfo=timezone.utc))


def test_budget_guard_refuses_past_budget(store):
    meter = CostMeter(store, {"x": 0.012})
    with meter.metered("x", usd=0.005):
        pass
    with meter.metered("x", usd=0.005):
        pass
    assert meter.spent_this_month("x") == pytest.approx(0.010)
    with pytest.raises(BudgetExceeded):
        with meter.metered("x", usd=0.005):
            pytest.fail("the call must not be made")
    assert meter.spent_this_month("x") == pytest.approx(0.010)


def test_unbudgeted_sources_may_only_make_free_calls(store):
    meter = CostMeter(store, {})
    meter.ensure_budget("free", 0)
    with pytest.raises(BudgetExceeded):
        meter.ensure_budget("free", 0.001)


def test_failed_calls_are_still_recorded(store):
    meter = CostMeter(store, {"x": 1})
    with pytest.raises(RuntimeError):
        with meter.metered("x", usd=0.25):
            raise RuntimeError("boom")
    assert meter.spent_this_month("x") == pytest.approx(0.25)


def test_last_month_does_not_count(store):
    meter = CostMeter(store, {"x": 1})
    store.add_cost_event("x", datetime.now(timezone.utc) - timedelta(days=40), 0.9, 0, 1, None)
    assert meter.remaining("x") == 1


def test_quota_window(store):
    meter = CostMeter(store, {})
    quota = (500, timedelta(days=7))
    store.add_cost_event("threads", datetime.now(timezone.utc) - timedelta(days=8), 0, 499, 1, None)
    for _ in range(3):
        with meter.metered("threads", units=166, quota=quota):
            pass
    with pytest.raises(QuotaExceeded):
        meter.ensure_quota("threads", 3, *quota)


def test_ledger(store):
    meter = CostMeter(store, {"x": 5, "y": 0})
    meter.record("x", usd=1.5, units=10)
    ledger = meter.ledger()
    assert ledger["x"]["usd"] == 1.5 and ledger["x"]["remaining"] == 3.5
    assert ledger["y"] == {"usd": 0.0, "units": 0.0, "calls": 0.0, "budget": 0, "remaining": 0.0}
