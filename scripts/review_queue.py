"""What still needs a human: forums whose terms nobody has read, and feeds held by the on-demand checks.

    python scripts/review_queue.py [--industry KEY]

Approve a forum by reading its terms and setting terms: allowed (or forbidden, with terms_notes) in
industries/<key>.yaml; release a held feed by setting status: live and locked: false.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from signal_connectors.on_demand import review_queue  # noqa: E402

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--industry")
    args = p.parse_args()
    rows = [r for r in review_queue() if not args.industry or r["industry"] == args.industry]
    for r in rows:
        print(f"{r['industry']:22} {r['kind']:12} {r['url']:60} {r['detail']}")
    print(f"{len(rows)} items")
