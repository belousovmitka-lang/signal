"""Make sure a user's industry has a map: match an existing one, or build one now (on_demand.py).

    python scripts/ensure_industry.py "Dog grooming" --description "I run a mobile grooming van"
    python scripts/ensure_industry.py "Dog grooming" --roles "groomer,salon owner" --no-collect

Prints a JSON result for the app: key, action (matched | exists | created), feeds enabled and held,
forums waiting for a terms review, the first fill, and the cost.
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from signal_connectors.on_demand import IndustryRequest, ensure_industry  # noqa: E402
from signal_connectors.services import Services  # noqa: E402


def split(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


async def main(args: argparse.Namespace) -> int:
    request = IndustryRequest(args.name, args.description or "", split(args.roles), split(args.keywords), split(args.tools))
    async with Services.open() as services:
        result = await ensure_industry(services, request, collect=not args.no_collect, discovery_budget_usd=args.budget)
    print(result.to_json())
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("name")
    p.add_argument("--description")
    p.add_argument("--roles")
    p.add_argument("--keywords")
    p.add_argument("--tools")
    p.add_argument("--budget", type=float, default=0.15, help="discovery search budget for this industry (USD)")
    p.add_argument("--no-collect", action="store_true", help="write the map but skip the first fill")
    sys.exit(asyncio.run(main(p.parse_args())))
