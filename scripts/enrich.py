"""Stage A enrichment: python scripts/enrich.py status | run | collect | embed"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from signal_feed.enrichment.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
