"""One-off cleanup of rows stored before 2026-10-10's data fixes (run once, on request):

1. published_at more than a day after fetched_at (EdWeek webinar dates, scheduled Federal Register
   documents) -> set to fetched_at, keep the claimed date in raw.claimed_published_at.
2. Regulations.gov comments whose whole text is "See attached file(s)" -> delete.

    python scripts/cleanup_2026_10_10.py --dry-run
    python scripts/cleanup_2026_10_10.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from signal_connectors.connectors.regulations_gov import ATTACHMENT_ONLY  # noqa: E402
from signal_connectors.models import FUTURE_TOLERANCE  # noqa: E402
from signal_connectors.settings import Settings  # noqa: E402
from signal_connectors.store import PostRow, Store  # noqa: E402


def main(dry_run: bool) -> int:
    store = Store(Settings.from_env().database_url)
    with store.session() as s, s.begin():
        future = [r for r in s.scalars(select(PostRow)) if r.published_at > r.fetched_at + FUTURE_TOLERANCE]
        stubs = [r for r in s.scalars(select(PostRow).where(PostRow.source == "regulations_gov"))
                 if ATTACHMENT_ONLY.match(r.text or "")]
        print(f"future-dated rows: {len(future)}; attachment-only comments: {len(stubs)}")
        if dry_run:
            s.rollback()
            return 0
        for r in future:
            r.raw = {**(r.raw or {}), "claimed_published_at": r.published_at.isoformat()}
            r.published_at = r.fetched_at
        for r in stubs:
            s.delete(r)
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main("--dry-run" in sys.argv))
