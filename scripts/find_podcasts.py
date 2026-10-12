"""Find each industry's podcasts on Podcast Index and write their feeds to the maps.

    python scripts/find_podcasts.py [--industry KEY] [--per-industry 8] [--dry-run]

Named shows already in a map get their feed URL; keyword searches (the industry's name and top
roles) add more. A show is kept only if it is alive (an episode in the last 90 days), has 5+
episodes, is in English and its title or description matches the industry's vocabulary.
Needs PODCASTINDEX_KEY and PODCASTINDEX_SECRET (free at api.podcastindex.org).
"""

import argparse
import asyncio
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from signal_connectors.base import HttpClient  # noqa: E402
from signal_connectors.connectors.podcasts import find_shows  # noqa: E402
from signal_connectors.industries import INDUSTRIES_DIR, Link, load_industries, save_industry  # noqa: E402
from signal_connectors.on_demand import vocabulary_terms  # noqa: E402
from signal_connectors.settings import Settings  # noqa: E402


async def main(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    industries = load_industries()
    if args.industry:
        industries = {args.industry: industries[args.industry]}
    today = date.today()
    async with HttpClient(user_agent=settings.user_agent, max_attempts=3, timeout=20) as http:
        for key, ind in industries.items():
            found = await find_shows(http, ind.name, ind.vocabulary.roles, [p.name for p in ind.podcasts if not p.url],
                                     vocabulary_terms(ind), today, limit=args.per_industry)
            have = {p.url for p in ind.podcasts if p.url}
            for name, feed in found:
                url, note = feed["url"], f"Podcast Index {feed['id']}, {feed.get('episodeCount')} episodes"
                if url in have:
                    continue
                entry = next((p for p in ind.podcasts if name and p.name == name), None)
                if entry is None:
                    entry = Link(name=feed.get("title") or url)
                    ind.podcasts.append(entry)
                entry.url, entry.status, entry.checked_at, entry.locked, entry.notes = url, "live", today, True, note
                have.add(url)
            live = [p.name for p in ind.podcasts if p.status == "live"]
            print(f"{key}: {len(live)} live shows: {', '.join(live)}")
            if not args.dry_run:
                save_industry(ind, INDUSTRIES_DIR / f"{key}.yaml")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--industry")
    p.add_argument("--per-industry", type=int, default=8)
    p.add_argument("--dry-run", action="store_true")
    sys.exit(asyncio.run(main(p.parse_args())))
