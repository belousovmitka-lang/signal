"""The records every connector exchanges: RawPost, SourceQuery, FetchResult, HealthStatus."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .dedupe import canonical_url, sha256

SourceType = Literal["community", "social", "paid_demand", "review",
                     "news", "government", "media", "startup", "research"]
Route = Literal["own_api", "search_api", "rss", "public_pages"]
Cadence = Literal["hourly", "daily", "weekly", "monthly"]

RAW_MAX_STRING = 4000
RAW_MAX_ITEMS = 50
RAW_MAX_DEPTH = 6
_HEX64 = re.compile(r"[0-9a-f]{64}")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_utc(value: datetime) -> datetime:
    """Treat naive datetimes as UTC and convert aware ones to UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def make_post_id(source: str, url: str) -> str:
    """sha256 of the source name and the canonical URL."""
    return sha256(f"{source}|{canonical_url(url)}")


def trim_raw(value: Any, depth: int = 0) -> Any:
    """Bound the size of a raw payload: long strings cut, long lists shortened, deep nesting dropped."""
    if depth >= RAW_MAX_DEPTH:
        return None
    if isinstance(value, dict):
        return {str(k): trim_raw(v, depth + 1) for k, v in list(value.items())[:RAW_MAX_ITEMS * 2]}
    if isinstance(value, (list, tuple)):
        return [trim_raw(v, depth + 1) for v in value[:RAW_MAX_ITEMS]]
    if isinstance(value, str) and len(value) > RAW_MAX_STRING:
        return value[:RAW_MAX_STRING] + "…"
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class RawPost(BaseModel):
    """One collected post. Downstream code only ever sees this shape.

    `id` is computed from `source` and the canonical `url` when not given, and `url`
    is stored in canonical form.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    source: str
    source_type: SourceType
    route: Route
    url: str
    title: str | None = None
    text: str
    is_snippet: bool
    author_hash: str | None = None
    author_role: str | None = None
    community: str | None = None
    published_at: datetime
    fetched_at: datetime = Field(default_factory=utcnow)
    language: str | None = None
    engagement: dict[str, int] = Field(default_factory=dict)
    money: dict[str, Any] | None = None
    industry_hint: str | None = None
    query: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _fill_id(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("url"):
            data = dict(data)
            data["url"] = canonical_url(data["url"])
            if not data.get("id") and data.get("source"):
                data["id"] = make_post_id(data["source"], data["url"])
        return data

    @field_validator("published_at", "fetched_at")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return to_utc(v)

    @field_validator("title", "text", "author_role", "community")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v

    @field_validator("author_hash")
    @classmethod
    def _hashed_only(cls, v: str | None) -> str | None:
        if v is not None and not _HEX64.fullmatch(v):
            raise ValueError("author_hash must be a hash from privacy.hash_author, never a raw handle")
        return v

    @field_validator("engagement", mode="before")
    @classmethod
    def _engagement(cls, v: Any) -> Any:
        if isinstance(v, dict):
            return {k: int(n) for k, n in v.items() if n is not None}
        return v

    @field_validator("raw", mode="before")
    @classmethod
    def _trim(cls, v: Any) -> Any:
        return trim_raw(v) if isinstance(v, dict) else v

    @model_validator(mode="after")
    def _has_content(self) -> RawPost:
        if not self.text and not self.title:
            raise ValueError("a post needs a title or text")
        return self


class SourceQuery(BaseModel):
    """What one fetch should collect."""

    keywords: list[str] = Field(default_factory=list)
    communities: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)
    since: datetime | None = None
    limit: int = Field(default=50, ge=1, le=10_000)
    industry_hint: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)  # connector-specific extras

    @field_validator("since")
    @classmethod
    def _utc(cls, v: datetime | None) -> datetime | None:
        return to_utc(v) if v else None

    @property
    def query_text(self) -> str | None:
        return " ".join(self.keywords) or None

    @property
    def key(self) -> str:
        """Stable identity of what this query fetches (ignores since and limit)."""
        return sha256(self.model_dump_json(exclude={"since", "limit"}))


class FetchResult(BaseModel):
    """Summary of one run of one source."""

    source: str
    started_at: datetime
    finished_at: datetime | None = None
    status: Literal["ok", "partial", "failed", "skipped"] = "ok"
    fetched: int = 0      # valid RawPost records produced
    new: int = 0          # stored for the first time
    duplicates: int = 0
    invalid: int = 0      # items the connector could not turn into a RawPost
    cost_usd: float = 0.0
    errors: list[str] = Field(default_factory=list)


class HealthStatus(BaseModel):
    ok: bool
    auth_ok: bool | None = None
    quota_left: float | None = None      # units left in the current quota window
    budget_left_usd: float | None = None
    last_success: datetime | None = None
    message: str | None = None
