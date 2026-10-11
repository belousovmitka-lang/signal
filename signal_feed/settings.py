"""Feed settings from the environment (.env in development).

Feed tables live in their own database for now (FEED_DATABASE_URL, default data/feed.db);
raw posts are read, never written, from the connectors' database (DATABASE_URL).
Pointing both at one database later is a config change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class FeedSettings:
    feed_database_url: str
    posts_database_url: str
    config_file: Path

    @classmethod
    def from_env(cls) -> FeedSettings:
        load_dotenv(REPO_ROOT / ".env")
        config = Path(os.environ.get("FEED_CONFIG_FILE") or "config/feed.yaml")
        if not config.is_absolute():
            config = REPO_ROOT / config
        return cls(
            feed_database_url=os.environ.get("FEED_DATABASE_URL") or f"sqlite:///{REPO_ROOT / 'data' / 'feed.db'}",
            posts_database_url=os.environ.get("DATABASE_URL") or f"sqlite:///{REPO_ROOT / 'data' / 'signal.db'}",
            config_file=config,
        )
