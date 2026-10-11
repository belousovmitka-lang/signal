"""Phase 3 demo: one builder's first sessions, on a throwaway copy of the feed database.

    python scripts/feed_session.py --industries "Healthcare" "Legal > Litigation support" --skills "AI agents"
    python scripts/feed_session.py --industries "IT and developer tools" --like tool_gap pain --pages 2
    python scripts/feed_session.py --industries "Healthcare" --like pain --map   # then the map after a week

Onboards a user, shows the calibration deck, answers it (Interested for --like post types, Not for me
otherwise), then prints feed pages with each item's internal reason (logged, never shown in the app).
Real users are never touched: the feed database is copied to a temporary file first (SQLite only).
"""

import argparse
import shutil
import sys
import tempfile
import textwrap
import uuid
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
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--industries", nargs="+", required=True, help='e.g. "Healthcare" "Legal > Contract work"')
    ap.add_argument("--skills", nargs="*", default=[])
    ap.add_argument("--like", nargs="*", default=["pain", "tool_gap"], help="post types to answer Interested")
    ap.add_argument("--pages", type=int, default=1)
    ap.add_argument("--map", action="store_true",
                    help="save the liked items shown, run the weekly map job, and print the map and its changes")
    args = ap.parse_args()

    settings = FeedSettings.from_env()
    if not settings.feed_database_url.startswith("sqlite:///"):
        raise SystemExit("this demo copies a SQLite feed database; FEED_DATABASE_URL is not SQLite")
    tmp = Path(tempfile.mkdtemp()) / "feed.db"
    shutil.copy(settings.feed_database_url.removeprefix("sqlite:///"), tmp)
    store = FeedStore(f"sqlite:///{tmp}")
    client = TestClient(create_app(store, PostsReader(settings.posts_database_url), load_feed_config(settings.config_file)))

    r = client.post("/onboarding", json={"industries": args.industries, "skills": args.skills})
    if r.status_code != 200:
        raise SystemExit(r.json()["detail"])
    uid = r.json()["user_id"]
    cal = client.get("/feed", params={"user_id": uid, "session_id": "calibration"}).json()
    print(f"Calibration ({len(cal['items'])} cards):")
    events = []
    for it in cal["items"]:
        liked = it["post_type"] in args.like
        print(f"  [{'Interested' if liked else 'Not for me':10}] {it['post_type']:11} "
              f"{' > '.join(it['niche_path'])[:38]:38} {textwrap.shorten(it['summary'] or it['title'] or '', 60)}")
        events.append({"client_event_id": uuid.uuid4().hex, "session_id": "calibration", "item_id": it["post_id"],
                       "type": "interested" if liked else "not_for_me", "at": utcnow().isoformat(),
                       "impression_id": it["impression_id"]})
    client.post("/events", json={"user_id": uid, "events": events})
    shown = []
    for n in range(1, args.pages + 1):
        page = client.get("/feed", params={"user_id": uid, "session_id": "first"}).json()
        shown += page["items"]
        print(f"\nFeed page {n}: {len(page['items'])} items{' (caught up)' if page['caught_up'] else ''}")
        for it in page["items"]:
            extra = f" +{it['similar_posts']} similar" if it["similar_posts"] else ""
            reason = it["score_breakdown"].get("reason", "")
            print(f"  {it['score']:5.2f}  {it['post_type']:11} {textwrap.shorten(reason, 58):58} "
                  f"| {textwrap.shorten(it['summary'] or it['title'] or '', 60)}{extra}")
    if args.map:
        from signal_feed.feed.weekly import WeeklyStats, run_user
        saves = [{"client_event_id": uuid.uuid4().hex, "session_id": "first", "item_id": it["post_id"], "type": "save",
                  "at": utcnow().isoformat(), "impression_id": it["impression_id"]}
                 for it in shown if it["post_type"] in args.like]
        if saves:
            client.post("/events", json={"user_id": uid, "events": saves})
        run_user(store, load_feed_config(settings.config_file), uid, utcnow(), WeeklyStats())
        m = client.get("/map", params={"user_id": uid}).json()
        print(f"\nMap after saving {len(saves)} posts and one weekly run (what the app gets: no messages):")
        for node in m["nodes"]:
            print(f"  {node['ring']:9} {node['affinity']:.2f}  r={node['radius']:.2f} angle={node['angle']:6.1f}  "
                  f"{' > '.join(node['path'])[:50]:50} {node['status']}")
        from signal_feed.db import MapChangeLogRow
        with store.session() as s:
            for c in s.query(MapChangeLogRow).filter_by(user_id=uid).order_by(MapChangeLogRow.at):
                print(f"  (internal log, never shown) {c.message}")
        for u in client.get("/profile/niches/pending", params={"user_id": uid}).json()["updates"]:
            print(f"  profile suggestion: {u['action']} {' > '.join(u['path'])}?")
    shutil.rmtree(tmp.parent, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
