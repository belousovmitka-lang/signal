"""Live lane: search Reddit, X, Facebook groups, YouTube... now for an industry; print, store nothing.

    python scripts/live.py dental [--platforms reddit,youtube] [--extra "insurance verification"] [--json]

The app calls signal_connectors.live.live_search() the same way when a user opens their feed.
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from signal_connectors.industries import INDUSTRIES_DIR, load_industry  # noqa: E402
from signal_connectors.live import live_search  # noqa: E402
from signal_connectors.services import Services  # noqa: E402


async def main(args: argparse.Namespace) -> int:
    industry = load_industry(INDUSTRIES_DIR / f"{args.industry}.yaml")
    async with Services.open() as services:
        results = await live_search(services, industry, extra=args.extra, limit=args.limit, freshness=args.freshness,
                                    platforms=args.platforms.split(",") if args.platforms else None)
    if args.json:
        print(json.dumps(results.to_dict(), indent=2))
        return 0
    print(f"query: {results.query}\n")
    for s in results.sections:
        print(f"== {s.label} ({s.provider}){'  ERROR: ' + s.error if s.error else ''}")
        for item in s.items:
            print(f"  - {item.title[:90]}\n    {item.url}")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("industry")
    p.add_argument("--platforms", help="comma list: reddit,x,facebook,youtube,linkedin,quora")
    p.add_argument("--extra", help="extra words for the query")
    p.add_argument("--limit", type=int, default=8)
    p.add_argument("--freshness", default="py", help="Brave window: pd, pw, pm, py (a month is too short for most niches)")
    p.add_argument("--json", action="store_true")
    sys.exit(asyncio.run(main(p.parse_args())))
