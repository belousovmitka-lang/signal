"""Simulated builders: hidden tastes, real feed. For exercising the learning loop and the offline
evaluation before real users exist (FEED_ALGORITHM.md Phases 6-7). Never run against the real feed
database: callers use a throwaway copy.

A simulated builder likes some industries and post types (hidden from the system). For each item they
see, `interest` = industry taste x type taste. They read with probability rising in interest, save and
"I'd build this" only after reading, answer "Not for me" or scroll past what they don't like, and answer
"Was this useful?" from their taste. Time moves forward one day per session.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np

from ..config import FeedConfig
from ..db import FeedStore
from ..events import EventBatch, EventIn, log_events
from ..feed.service import FeedPage, FeedService
from ..feed.usermodel import apply_events, onboard
from ..posts import PostsReader


@dataclass
class SimBuilder:
    industries: dict[str, float]          # root industry -> taste 0-1 (others: `other`)
    types: dict[str, float]               # post type -> taste 0-1 (others: 0.5)
    quiz: list[str]                       # what they pick in onboarding (may miss real interests)
    other: float = 0.05
    user_id: str | None = None

    def interest(self, niche_path: list[str], post_type: str) -> float:
        return self.industries.get(niche_path[0], self.other) * self.types.get(post_type, 0.5)


@dataclass
class SimStats:
    pages: int = 0
    shown: int = 0
    saves: int = 0
    reads: int = 0
    not_for_me: int = 0
    surveys: int = 0
    per_session_save_rate: list[float] = field(default_factory=list)
    per_session_mean_interest: list[float] = field(default_factory=list)


def random_builders(n: int, industries: list[str], rng: np.random.Generator) -> list[SimBuilder]:
    """Each builder loves 1-2 industries, likes 1-2 more, and names only some of them in the quiz."""
    types = ["pain", "tool_gap", "paid_demand", "change", "idea"]
    out = []
    for _ in range(n):
        picks = list(rng.choice(industries, size=min(4, len(industries)), replace=False))
        loves, likes = picks[:int(rng.integers(1, 3))], picks[2:4]
        tastes = {i: float(rng.uniform(0.8, 1.0)) for i in loves} | {i: float(rng.uniform(0.4, 0.6)) for i in likes}
        type_taste = {t: float(rng.uniform(0.3, 1.0)) for t in types}
        quiz = loves[:1] + ([likes[0]] if likes and rng.random() < 0.5 else [])   # under-reported on purpose
        out.append(SimBuilder(tastes, type_taste, quiz))
    return out


def _respond(b: SimBuilder, page: FeedPage, session: str, at: datetime, rng: np.random.Generator,
             stats: SimStats) -> list[EventIn]:
    events = []

    def ev(item, etype, **kw):
        events.append(EventIn(client_event_id=uuid.uuid4().hex, session_id=session, type=etype, item_id=item.post_id,
                              at=at, impression_id=item.impression_id, position=item.position, **kw))

    for item in page.items:
        interest = b.interest(item.niche_path, item.post_type)
        stats.shown += 1
        if page.calibration:
            ev(item, "interested" if interest >= 0.4 else "not_for_me")
            continue
        if rng.random() < 0.1 + 0.8 * interest:
            ev(item, "read", dwell_ms=180_000)
            stats.reads += 1
            if rng.random() < 0.6 * interest ** 2:
                ev(item, "save")
                stats.saves += 1
                if rng.random() < 0.3 * interest:
                    ev(item, "id_build")
            if item.ask_useful:
                ev(item, "useful_yes" if interest >= 0.4 else "useful_no")
                stats.surveys += 1
        elif interest < 0.2 and rng.random() < 0.35:
            ev(item, "not_for_me")
            stats.not_for_me += 1
        else:
            ev(item, "scroll_past", dwell_ms=600, fully_visible=True)
    return events


def simulate(store: FeedStore, posts: PostsReader, config: FeedConfig, builders: list[SimBuilder],
             sessions: int, pages_per_session: int, start: datetime, seed: int = 0) -> SimStats:
    rng = np.random.default_rng(seed)
    clock = {"now": start}
    from ..feed.data import FeedDataCache
    service = FeedService(store, config, FeedDataCache(store, posts), clock=lambda: clock["now"], seed=seed)
    stats = SimStats()
    for b in builders:
        b.user_id = onboard(store, config, b.quiz, [], clock=lambda: clock["now"])
    for day in range(sessions):
        clock["now"] = start + timedelta(days=day, hours=9)
        saves_before, shown_before, interest = stats.saves, stats.shown, []
        for b in builders:
            session = f"d{day}"
            for _ in range(pages_per_session):
                page = service.page(b.user_id, session)
                stats.pages += 1
                interest += [b.interest(i.niche_path, i.post_type) for i in page.items if not page.calibration]
                events = _respond(b, page, session, clock["now"], rng, stats)
                if events:
                    stored = []
                    log_events(EventBatch(user_id=b.user_id, events=events), store, posts, config, now=clock["now"],
                               on_stored=stored.extend)
                    apply_events(store, posts, config, stored, clock=lambda: clock["now"])
                if page.caught_up:
                    break
        shown = stats.shown - shown_before
        stats.per_session_save_rate.append(round((stats.saves - saves_before) / shown, 4) if shown else 0.0)
        stats.per_session_mean_interest.append(round(float(np.mean(interest)), 4) if interest else 0.0)
    return stats
