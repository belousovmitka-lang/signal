"""Phase 0 demo: log a session of events through POST /events against real posts.

Reads posts read-only from the connectors' database; writes feed tables to a throwaway
database unless --feed-db is given.

    python scripts/feed_demo.py
    python scripts/feed_demo.py --feed-db sqlite:///data/feed.db
"""

import argparse
import sys
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from signal_connectors.models import utcnow  # noqa: E402
from signal_feed.api import create_app  # noqa: E402
from signal_feed.config import load_feed_config  # noqa: E402
from signal_feed.db import FeedStore  # noqa: E402
from signal_feed.posts import PostsReader  # noqa: E402
from signal_feed.settings import FeedSettings  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--feed-db", help="feed database URL (default: a temporary file)")
    args = parser.parse_args()

    settings = FeedSettings.from_env()
    feed_url = args.feed_db or f"sqlite:///{Path(tempfile.mkdtemp()) / 'feed.db'}"
    store = FeedStore(feed_url)
    posts = PostsReader(settings.posts_database_url)
    config = load_feed_config(settings.config_file)
    client = TestClient(create_app(store, posts, config))

    sample = list(posts.iter_posts(limit=5))
    if len(sample) < 5:
        print("need at least 5 posts in the connectors' database; run some connectors first")
        return 1
    user = store.create_user()
    now, session = utcnow(), uuid.uuid4().hex[:12]

    def event(n, type, post, **kw):
        return {"client_event_id": f"{session}-{n}", "session_id": session, "type": type, "item_id": post.id,
                "at": (now - timedelta(seconds=60 - n)).isoformat(), "position": n, **kw}

    a, b, c, d, e = sample
    events = [
        event(0, "impression", a), event(1, "read", a, dwell_ms=90_000), event(2, "save", a),
        event(3, "id_build", a), event(4, "scroll_past", b, dwell_ms=600, fully_visible=True),
        event(5, "scroll_past", c, dwell_ms=4000, fully_visible=True), event(6, "read", d, dwell_ms=500),
        event(7, "not_for_me", e, reason="industry_not_interesting"), event(8, "useful_yes", a),
        {**event(9, "save", a), "item_id": "not-a-real-post"},
    ]
    print(f"feed db:  {feed_url}\nposts db: {settings.posts_database_url} (read-only)\nuser:     {user}\n")

    first = client.post("/events", json={"user_id": user, "events": events})
    print(f"POST /events -> {first.status_code} {first.json()}")
    again = client.post("/events", json={"user_id": user, "events": events})
    print(f"resend       -> {again.status_code} {again.json()}")
    ghost = client.post("/events", json={"user_id": "ghost", "events": events[:1]})
    print(f"unknown user -> {ghost.status_code} {ghost.json()}\n")

    print(f"{'event':<12} {'weight':>6}  {'note':<18} post")
    for row in store.events_for(user):
        title = next(p.title or p.text for p in sample if p.id == row.item_id)[:50]
        print(f"{row.event_type:<12} {row.weight:>6}  {row.weight_note or '':<18} {title}")
    total = sum(r.weight for r in store.events_for(user))
    print(f"\n{store.count_events(user)} events stored, net weight {total:+g} (config v{config.version})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
