"""Niche tree and problems: python scripts/problems.py build | tree | top"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from signal_feed.problems.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
