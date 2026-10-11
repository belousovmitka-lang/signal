from datetime import timedelta

from signal_connectors.costs import CostMeter
from signal_connectors.models import FetchResult, utcnow
from signal_connectors.status import source_status


def test_failure_streak_recent_volume_and_spend(store):
    now = utcnow()
    for i, status in enumerate(["failed", "failed", "ok", "failed"]):     # newest first
        store.record_run(FetchResult(source="rss", started_at=now - timedelta(hours=i), status=status,
                                     new=5, errors=["boom"] if status == "failed" else []))
    store.record_run(FetchResult(source="rss", started_at=now - timedelta(days=9), status="ok", new=100))
    costs = CostMeter(store, {"rss": 1})
    costs.record("rss", usd=0.25)
    st = source_status(store, costs, "rss", None, now)
    assert st.failures_in_a_row == 2 and st.new_7d == 20 and st.runs_7d == 4
    assert st.last_status == "failed" and st.last_error == "boom" and st.spent_month_usd == 0.25
    assert source_status(store, costs, "nothing", None, now).last_run is None
