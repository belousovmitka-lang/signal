"""Schema and loader for sources.yaml."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .industries import INDUSTRIES_DIR, Industry, load_industries
from .models import Cadence, Route, SourceQuery, SourceType
from .settings import env_flag

Status = Literal["todo", "done", "awaiting_approval", "paid_flag_off", "blocked"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AccessCheck(_Strict):
    """Rule 2: the robots.txt and terms decision for a site, recorded before reading it."""

    robots: Literal["allowed", "partial", "disallowed", "not_applicable", "unchecked"] = "unchecked"
    terms: Literal["allowed", "unclear", "forbidden", "not_applicable", "unchecked"] = "unchecked"
    decision: Literal["full", "search_only", "skip", "pending"] = "pending"
    terms_url: str | None = None
    checked_at: date | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> AccessCheck:
        if self.decision == "full" and (self.robots == "disallowed" or self.terms == "forbidden"):
            raise ValueError("decision 'full' is not allowed when robots disallows or terms forbid; "
                             "use 'search_only' or 'skip'")
        if self.decision in {"full", "search_only"} and self.checked_at is None:
            raise ValueError("record checked_at when making an access decision")
        return self


class RateLimit(_Strict):
    requests_per_second: float = Field(gt=0)
    domains: list[str] = Field(default_factory=list)  # domains this limit applies to


class Quota(_Strict):
    """A free-tier quota, e.g. YouTube 10,000 units per 1 day, Threads 500 queries per 7 days."""

    units: float = Field(gt=0)
    window_days: float = Field(gt=0)

    @property
    def window(self) -> timedelta:
        return timedelta(days=self.window_days)


class QueryConfig(_Strict):
    keywords: list[str] = Field(default_factory=list)
    communities: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)
    limit: int | None = Field(default=None, ge=1, le=10_000)
    industry_hint: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class Defaults(_Strict):
    requests_per_second: float = Field(default=1.0, gt=0)
    timeout_seconds: float = Field(default=20.0, gt=0)
    max_attempts: int = Field(default=5, ge=1)
    initial_lookback_days: int = Field(default=7, ge=1)
    limit: int = Field(default=50, ge=1)


class SourceConfig(_Strict):
    name: str = ""                   # filled from the mapping key
    connector: str = ""              # module in signal_connectors/connectors; defaults to name
    enabled: bool = False
    status: Status = "todo"
    status_date: date | None = None
    route: Route
    source_type: SourceType
    cadence: Cadence = "daily"
    monthly_budget_usd: float = Field(default=0.0, ge=0)
    paid: bool = False
    feature_flag: str | None = None  # env var that must be truthy for the source to run
    env: list[str] = Field(default_factory=list)  # env keys the connector needs
    access_check: AccessCheck = Field(default_factory=AccessCheck)
    rate_limit: RateLimit | None = None
    quota: Quota | None = None
    deletion_recheck_days: int | None = Field(default=None, ge=1)  # Reddit and X terms
    queries: list[QueryConfig] = Field(default_factory=list)
    options: dict[str, Any] = Field(default_factory=dict)
    notes: str | None = None

    @model_validator(mode="after")
    def _rules(self) -> SourceConfig:
        if self.paid and not self.feature_flag:
            raise ValueError(f"{self.name}: paid sources need a feature_flag (off by default)")
        if self.enabled and self.route == "public_pages" and self.access_check.decision != "full":
            raise ValueError(f"{self.name}: a public_pages source can only be enabled after its "
                             "access_check records decision 'full' (rule 2)")
        if self.enabled and self.access_check.decision == "skip":
            raise ValueError(f"{self.name}: access_check decision is 'skip'; it cannot be enabled")
        if self.enabled and self.status == "blocked":
            raise ValueError(f"{self.name}: blocked sources cannot be enabled")
        return self

    def blocked_reason(self, *, ignore_enabled: bool = False) -> str | None:
        """Why this source must not run right now, or None if it may."""
        if self.status == "blocked":
            return "status is blocked"
        if self.access_check.decision == "skip":
            return "access_check decision is skip"
        if self.route == "public_pages" and self.access_check.decision != "full":
            return "public_pages source without a recorded access check (rule 2)"
        if self.feature_flag and not env_flag(self.feature_flag):
            return f"feature flag {self.feature_flag} is off"
        if not ignore_enabled and not self.enabled:
            return "not enabled in sources.yaml"
        return None

    def build_queries(self, defaults: Defaults) -> list[SourceQuery]:
        configs = self.queries or [QueryConfig()]
        return [
            SourceQuery(
                keywords=q.keywords, communities=q.communities, domains=q.domains,
                limit=q.limit or defaults.limit, industry_hint=q.industry_hint, params=q.params,
            )
            for q in configs
        ]


class SourcesFile(_Strict):
    version: Literal[1] = 1
    defaults: Defaults = Field(default_factory=Defaults)
    sources: dict[str, SourceConfig] = Field(default_factory=dict)

    @field_validator("sources", mode="before")
    @classmethod
    def _names(cls, value: Any) -> Any:
        if isinstance(value, dict):
            filled = {}
            for key, cfg in value.items():
                cfg = dict(cfg or {})
                cfg.setdefault("name", key)
                cfg.setdefault("connector", key)
                filled[key] = cfg
            return filled
        return value

    def budgets(self) -> dict[str, float]:
        return {name: s.monthly_budget_usd for name, s in self.sources.items()}

    def add_industry_feeds(self, industries: dict[str, Industry]) -> None:
        """Append one query per live industry feed to sources with options.industry_feeds: true."""
        for source in self.sources.values():
            if not source.options.get("industry_feeds"):
                continue
            have = {q.params.get("feed") for q in source.queries}
            for key, industry in industries.items():
                for feed in industry.live_feeds():
                    if feed.url not in have:
                        have.add(feed.url)
                        source.queries.append(QueryConfig(industry_hint=key,
                                                          params={"feed": feed.url, "name": feed.name}))

    def get(self, name: str) -> SourceConfig:
        try:
            return self.sources[name]
        except KeyError:
            raise KeyError(f"no source named {name!r} in sources.yaml") from None


def load_sources(path: Path | str, industries_dir: Path | None = INDUSTRIES_DIR) -> SourcesFile:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    sources = SourcesFile.model_validate(data)
    if industries_dir is not None and industries_dir.is_dir():
        sources.add_industry_feeds(load_industries(industries_dir))
    return sources
