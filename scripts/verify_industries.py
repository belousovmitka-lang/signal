"""Check every site in the industry seed maps and write the results back.

    python scripts/verify_industries.py              # all industries
    python scripts/verify_industries.py legal hr     # some
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from signal_connectors.base import HttpClient  # noqa: E402
from signal_connectors.industries import INDUSTRIES_DIR, load_industry, save_industry  # noqa: E402
from signal_connectors.robots import RobotsChecker  # noqa: E402
from signal_connectors.settings import Settings  # noqa: E402
from signal_connectors.verify import verify_industry  # noqa: E402


async def main(names: list[str]) -> int:
    settings = Settings.from_env()
    paths = [INDUSTRIES_DIR / f"{n}.yaml" for n in names] if names else sorted(INDUSTRIES_DIR.glob("*.yaml"))
    async with HttpClient(user_agent=settings.user_agent, max_attempts=2, timeout=15) as http:
        robots = RobotsChecker(http)
        async def one(path: Path) -> tuple[str, dict]:
            industry = load_industry(path)
            counts = await verify_industry(http, robots, industry)
            save_industry(industry, path)
            return path.stem, dict(counts)

        for name, counts in await asyncio.gather(*(one(p) for p in paths)):
            print(f"{name:16} " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser()
    parser.add_argument("industries", nargs="*")
    sys.exit(asyncio.run(main(parser.parse_args().industries)))
