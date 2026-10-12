"""Find public job boards (Greenhouse, Lever, Ashby) for each industry's incumbent tools.

    python scripts/find_job_boards.py [--industry KEY] [--dry-run]

Each tool in a map's vocabulary.tools is tried as a board name on the three ATS APIs; boards that
clearly belong to that company are written to the map's job_boards (status live, locked), which the
`job_boards` source then reads weekly. Tools already listed are skipped.
"""

import argparse
import asyncio
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from signal_connectors.base import HttpClient  # noqa: E402
from signal_connectors.connectors.job_boards import FoundBoard, find_boards  # noqa: E402
from signal_connectors.industries import INDUSTRIES_DIR, Link, load_industries, save_industry  # noqa: E402
from signal_connectors.robots import RobotsChecker  # noqa: E402
from signal_connectors.settings import Settings  # noqa: E402


async def main(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    industries = load_industries()
    if args.industry:
        industries = {args.industry: industries[args.industry]}
    tools = sorted({t for ind in industries.values() for t in ind.vocabulary.tools
                    if t not in {b.name for b in ind.job_boards}})
    async with HttpClient(user_agent=settings.user_agent, max_attempts=2, timeout=20) as http:
        robots = RobotsChecker(http)
        gate = asyncio.Semaphore(6)

        async def one(tool: str) -> tuple[str, list[FoundBoard]]:
            async with gate:
                return tool, await find_boards(http, robots, tool)

        found = dict(await asyncio.gather(*(one(t) for t in tools)))
    today = date.today()
    for key, ind in industries.items():
        added = []
        for tool in ind.vocabulary.tools:
            for board in found.get(tool, []):
                if board.url not in {b.url for b in ind.job_boards}:
                    ind.job_boards.append(Link(name=tool, url=board.url, status="live", checked_at=today, locked=True,
                                               notes=f"{board.ats} board, {board.jobs} open jobs (find_job_boards.py)"))
                    added.append(f"{tool} ({board.ats})")
        if added:
            print(f"{key}: {', '.join(added)}")
            if not args.dry_run:
                save_industry(ind, INDUSTRIES_DIR / f"{key}.yaml")
    print(f"{sum(bool(v) for v in found.values())} of {len(tools)} tools have a public board")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--industry")
    p.add_argument("--dry-run", action="store_true")
    sys.exit(asyncio.run(main(p.parse_args())))
