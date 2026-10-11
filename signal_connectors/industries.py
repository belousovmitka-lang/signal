"""Industry seed maps (industries/<industry>.yaml): where each industry talks, and how it talks.

Entries carry a `status` that scripts/verify_industries.py fills in from live checks.
Only entries with status "live" are used by connectors. Subreddits stay "unverified" until
the search route can check them (reddit.com is never fetched directly).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .settings import REPO_ROOT

INDUSTRIES_DIR = REPO_ROOT / "industries"

Status = Literal["unverified", "live", "dead", "blocked", "private", "search_only"]
ForumSoftware = Literal["discourse", "xenforo", "vbulletin", "phpbb", "invision", "khoros", "proboards", "mybb",
                        "custom", "unknown"]


class _Entry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    url: str | None = None
    status: Status = "unverified"
    checked_at: date | None = None
    notes: str | None = None
    locked: bool = False           # status set by a human: verify_industries.py leaves it alone


class Feed(_Entry):
    url: str
    entries: int | None = None       # entry count at the last check
    full_text: bool | None = None    # feed ships full article content


class Forum(_Entry):
    url: str
    software: ForumSoftware = "unknown"
    rss: str | None = None           # feed URL found on the page or at the software's default path
    robots: Literal["allowed", "disallowed", "unchecked"] = "unchecked"
    # Rule 2: read by hand. Only "allowed" forums are collected; verify_industries.py never changes this.
    terms: Literal["allowed", "forbidden", "unchecked"] = "unchecked"
    terms_url: str | None = None
    terms_notes: str | None = None

    @property
    def collectable(self) -> bool:
        return self.status == "live" and self.robots == "allowed" and self.terms == "allowed" and bool(self.rss)


class Link(_Entry):
    """Associations, job boards and other sites we only need to know exist."""


class Vocabulary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    roles: list[str] = Field(default_factory=list)          # self-descriptions: "paralegal"
    tasks: list[str] = Field(default_factory=list)          # "billing", "intake"
    pain_phrases: list[str] = Field(default_factory=list)   # "chasing signatures"
    jargon: list[str] = Field(default_factory=list)         # "e-discovery", "IOLTA"
    tools: list[str] = Field(default_factory=list)          # incumbent software: "Clio"


class DiscoveryInfo(BaseModel):
    """Set on maps written by the discovery agent: nothing here is collectable until a human reviews it."""

    model_config = ConfigDict(extra="forbid")

    generated_at: date
    needs_review: bool = True
    queries: int = 0
    cost_usd: float = 0.0
    candidates_checked: int = 0
    on_demand: bool = False        # generated for a user request and enabled by automatic checks (on_demand.py)
    request: str | None = None     # the request that produced it
    notes: str | None = None


class Industry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    industry: str
    name: str
    discovery: DiscoveryInfo | None = None
    vocabulary: Vocabulary = Field(default_factory=Vocabulary)
    subreddits: list[str] = Field(default_factory=list)
    stackexchange_sites: list[str] = Field(default_factory=list)
    forums: list[Forum] = Field(default_factory=list)
    rss_feeds: list[Feed] = Field(default_factory=list)
    associations: list[Link] = Field(default_factory=list)
    job_boards: list[Link] = Field(default_factory=list)
    podcasts: list[Link] = Field(default_factory=list)
    youtube_channels: list[Link] = Field(default_factory=list)

    def live_feeds(self) -> list[Feed]:
        return [f for f in self.rss_feeds if f.status == "live"]

    def collectable_forums(self) -> list[Forum]:
        return [f for f in self.forums if f.collectable]


def industry_path(industry: str, directory: Path = INDUSTRIES_DIR) -> Path:
    return directory / f"{industry}.yaml"


def load_industry(path: Path) -> Industry:
    return Industry.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def load_industries(directory: Path = INDUSTRIES_DIR) -> dict[str, Industry]:
    return {p.stem: load_industry(p) for p in sorted(directory.glob("*.yaml"))}


def save_industry(industry: Industry, path: Path) -> None:
    data = industry.model_dump(mode="json", exclude_none=True, exclude_defaults=False)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100), encoding="utf-8")
