"""Run one connector live: python scripts/smoke.py rss --limit 20"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from signal_connectors.smoke import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
