"""Purge stored posts that were deleted at the source: python scripts/recheck_deletions.py bluesky"""

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from signal_connectors.scheduler import _recheck  # noqa: E402
from signal_connectors.services import Services  # noqa: E402


async def main(names: list[str]) -> int:
    async with Services.open() as services:
        for name in names:
            # Recorded as a "<name>:recheck" run, as the scheduler does; "partial" = still running at the platform.
            result = await _recheck(services, services.sources.get(name))
            print(f"{name}: {result.status}, purged {result.duplicates}", *result.errors)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    sys.exit(asyncio.run(main(sys.argv[1:])))
