"""Collection health at a glance: python scripts/status.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from signal_connectors.config import load_sources  # noqa: E402
from signal_connectors.costs import CostMeter  # noqa: E402
from signal_connectors.models import utcnow  # noqa: E402
from signal_connectors.settings import Settings  # noqa: E402
from signal_connectors.status import source_status, stored_posts_by_prefix  # noqa: E402
from signal_connectors.store import Store  # noqa: E402


def ago(dt) -> str:
    if dt is None:
        return "never"
    mins = int((utcnow() - dt).total_seconds() // 60)
    return f"{mins}m ago" if mins < 120 else f"{mins // 60}h ago" if mins < 2880 else f"{mins // 1440}d ago"


def main() -> int:
    settings = Settings.from_env()
    sources = load_sources(settings.sources_file)
    store = Store(settings.database_url)
    costs = CostMeter(store, sources.budgets(), sources.defaults.provider_budgets)
    print(f"{'source':18} {'last run':>9} {'status':8} {'fails':>5} {'new 7d':>7} {'$ month':>8}  note")
    unhealthy = 0
    for name, cfg in sorted(sources.sources.items()):
        st = source_status(store, costs, name, cfg.blocked_reason())
        if st.runnable:
            print(f"{name:18} {'-':>9} {'off':8} {'':>5} {'':>7} {'':>8}  {st.runnable}")
            continue
        flag = "  <- failing" if st.failures_in_a_row >= 2 else ("  <- never ran" if st.last_run is None else "")
        unhealthy += bool(flag)
        note = (st.last_error or "")[:60]
        print(f"{name:18} {ago(st.last_run):>9} {st.last_status or '-':8} {st.failures_in_a_row:>5} {st.new_7d:>7} "
              f"{st.spent_month_usd:>8.3f}  {note}{flag}")
    totals = stored_posts_by_prefix(store)
    print(f"\nstored posts: {sum(totals.values()):,}  (" +
          ", ".join(f"{k} {v:,}" for k, v in sorted(totals.items(), key=lambda kv: -kv[1])[:8]) + ", ...)")
    print("provider spend this month: " + ", ".join(
        f"{p} ${costs.provider_spent_this_month(p):.3f} of ${b:.2f}" for p, b in sources.defaults.provider_budgets.items()))
    return 1 if unhealthy else 0


if __name__ == "__main__":
    sys.exit(main())
