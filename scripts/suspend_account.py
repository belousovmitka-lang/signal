"""Suspend or restore a Signal account (terms section 8; Brave Search API terms 4(c) require prompt suspension).

    python scripts/suspend_account.py someone@example.com --reason "stored live search results"
    python scripts/suspend_account.py someone@example.com --restore

A suspended account's sessions stop working at once: every route answers 403 "account suspended".
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from signal_feed.auth import Auth, LogMailer  # noqa: E402
from signal_feed.config import load_feed_config  # noqa: E402
from signal_feed.db import FeedStore  # noqa: E402
from signal_feed.settings import FeedSettings  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("email")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--reason", help="why (kept on the account, up to 200 characters)")
    group.add_argument("--restore", action="store_true", help="lift a suspension")
    args = ap.parse_args()
    settings = FeedSettings.from_env()
    auth = Auth(FeedStore(settings.feed_database_url), load_feed_config(settings.config_file), LogMailer(), "")
    done = auth.unsuspend(args.email) if args.restore else auth.suspend(args.email, args.reason)
    print(("restored" if args.restore else "suspended") if done else f"no account for {args.email}")
    return 0 if done else 1


if __name__ == "__main__":
    sys.exit(main())
