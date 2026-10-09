"""Process-wide settings read from the environment (.env in development)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from . import __version__

REPO_ROOT = Path(__file__).resolve().parent.parent
PLACEHOLDER_CONTACT = "contact@yourdomain"


@dataclass(frozen=True)
class Settings:
    database_url: str
    contact_email: str
    sources_file: Path

    @property
    def user_agent(self) -> str:
        return f"SignalBot/{__version__} (+mailto:{self.contact_email})"

    @property
    def has_real_contact(self) -> bool:
        return self.contact_email != PLACEHOLDER_CONTACT

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv(REPO_ROOT / ".env")
        sources = Path(os.environ.get("SOURCES_FILE") or "sources.yaml")
        if not sources.is_absolute():
            sources = REPO_ROOT / sources
        return cls(
            database_url=os.environ.get("DATABASE_URL") or f"sqlite:///{REPO_ROOT / 'data' / 'signal.db'}",
            contact_email=os.environ.get("SIGNAL_CONTACT_EMAIL") or PLACEHOLDER_CONTACT,
            sources_file=sources,
        )


def env_flag(name: str) -> bool:
    """True when an environment variable is set to a truthy value (1, true, yes, on)."""
    return (os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}
