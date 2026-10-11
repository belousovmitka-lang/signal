"""Event logging: validate a client batch, weight each event (section 6.3) and append it.

Batches are idempotent on (user, client_event_id), so a client can resend after a timeout.
Shape errors reject the whole request; per-event problems (unknown item, bad timestamp)
reject only that event and the rest are stored.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.exc import IntegrityError

from signal_connectors.models import to_utc, utcnow

from .config import EventType, FeedConfig
from .db import EventRow, FeedStore
from .posts import PostsReader

ItemType = Literal["post", "problem"]
NotForMeReason = Literal["not_my_skills", "industry_not_interesting", "too_crowded", "too_small", "already_exists"]


class UnknownUserError(LookupError):
    pass


class BatchTooLargeError(ValueError):
    pass


class EventIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_event_id: str = Field(min_length=1, max_length=64)
    session_id: str = Field(min_length=1, max_length=64)
    type: EventType
    item_type: ItemType = "post"
    item_id: str = Field(min_length=1, max_length=64)
    at: datetime
    dwell_ms: int | None = Field(default=None, ge=0)
    fully_visible: bool | None = None
    reason: NotForMeReason | None = None
    position: int | None = Field(default=None, ge=0)
    impression_id: int | None = None

    @field_validator("at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return to_utc(value)

    @model_validator(mode="after")
    def _fields_fit_type(self) -> EventIn:
        if self.reason is not None and self.type != "not_for_me":
            raise ValueError("reason is only allowed on not_for_me events")
        if self.type in {"read", "scroll_past"} and self.dwell_ms is None:
            raise ValueError(f"{self.type} events need dwell_ms")
        if self.type == "scroll_past" and self.fully_visible is None:
            raise ValueError("scroll_past events need fully_visible")
        return self


class EventBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1, max_length=64)
    events: list[EventIn] = Field(min_length=1)


class Rejected(BaseModel):
    index: int
    client_event_id: str
    error: str


class BatchResult(BaseModel):
    accepted: int = 0
    duplicates: int = 0
    rejected: list[Rejected] = Field(default_factory=list)


def expected_read_ms(words: int, config: FeedConfig) -> float:
    ev = config.events
    return max(ev.reading_min_expected_ms, words / ev.reading_words_per_minute * 60_000)


def event_weight(event: EventIn, config: FeedConfig, words: int | None) -> tuple[float, str | None]:
    """The weight an event counts for, and a note when it was discounted to 0.

    A scroll-past counts only when it was quick and the item fully visible; a read counts only
    at >= the configured share of the post's expected reading time (posts only: problems have
    no single length).
    """
    ev = config.events
    weight = ev.weights[event.type]
    if event.type == "scroll_past":
        if not event.fully_visible:
            return 0.0, "not_fully_visible"
        if event.dwell_ms is not None and event.dwell_ms >= ev.scroll_past_max_dwell_ms:
            return 0.0, "dwell_too_long"
    if event.type == "read" and words is not None and event.dwell_ms is not None:
        if event.dwell_ms < ev.read_min_share_of_expected * expected_read_ms(words, config):
            return 0.0, "read_too_short"
    return weight, None


def _problem_id(item_id: str) -> int | None:
    return int(item_id) if item_id.isdigit() else None


def log_events(batch: EventBatch, store: FeedStore, posts: PostsReader, config: FeedConfig,
               now: datetime | None = None,
               on_stored: Callable[[list[EventRow]], object] | None = None) -> BatchResult:
    """`on_stored` gets the newly stored rows (not duplicates or rejects), e.g. to update the user model."""
    now = now or utcnow()
    ev = config.events
    if len(batch.events) > ev.max_batch_size:
        raise BatchTooLargeError(f"batch has {len(batch.events)} events; the limit is {ev.max_batch_size}")
    if not store.user_exists(batch.user_id):
        raise UnknownUserError(batch.user_id)

    post_ids = {e.item_id for e in batch.events if e.item_type == "post"}
    problem_ids = {pid for e in batch.events if e.item_type == "problem" and (pid := _problem_id(e.item_id))}
    impression_ids = {e.impression_id for e in batch.events if e.impression_id is not None}
    known_posts = posts.existing_ids(post_ids)
    known_problems = store.existing_problem_ids(problem_ids)
    owned_impressions = store.owned_impression_ids(batch.user_id, impression_ids)
    words = posts.word_counts({e.item_id for e in batch.events if e.type == "read" and e.item_type == "post"})

    for attempt in range(2):
        result = BatchResult()
        stored = store.seen_client_event_ids(batch.user_id, {e.client_event_id for e in batch.events})
        rows: list[EventRow] = []
        in_batch: set[str] = set()
        for i, e in enumerate(batch.events):
            if e.client_event_id in stored or e.client_event_id in in_batch:
                result.duplicates += 1
                continue
            error = _check(e, known_posts, known_problems, owned_impressions, now, config)
            if error:
                result.rejected.append(Rejected(index=i, client_event_id=e.client_event_id, error=error))
                continue
            in_batch.add(e.client_event_id)
            weight, note = event_weight(e, config, words.get(e.item_id) if e.item_type == "post" else None)
            rows.append(EventRow(
                user_id=batch.user_id, client_event_id=e.client_event_id, session_id=e.session_id,
                item_type=e.item_type, item_id=e.item_id, event_type=e.type, weight=weight, weight_note=note,
                dwell_ms=e.dwell_ms, fully_visible=e.fully_visible, reason=e.reason, position=e.position,
                impression_id=e.impression_id, at=e.at, received_at=now, config_version=config.version,
            ))
        try:
            store.add_events(rows)
        except IntegrityError:
            # A concurrent resend of the same batch won the race: recount duplicates once.
            if attempt == 0:
                continue
            raise
        result.accepted = len(rows)
        if on_stored is not None and rows:
            on_stored(rows)
        return result
    raise AssertionError("unreachable")


def _check(e: EventIn, known_posts: set[str], known_problems: set[int], owned_impressions: set[int],
           now: datetime, config: FeedConfig) -> str | None:
    if e.item_type == "post" and e.item_id not in known_posts:
        return "unknown post"
    if e.item_type == "problem" and _problem_id(e.item_id) not in known_problems:
        return "unknown problem"
    if e.impression_id is not None and e.impression_id not in owned_impressions:
        return "unknown impression"
    if e.at > now + timedelta(seconds=config.events.max_clock_skew_seconds):
        return "timestamp in the future"
    if e.at < now - timedelta(days=config.events.max_age_days):
        return "timestamp too old"
    return None
