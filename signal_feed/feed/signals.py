"""What the app shows about the evidence behind an item: its signal strip, weekly mentions and sources.

The design (signal-design, 2026-10-30) gives every problem five signal types: pain, paid demand, tool gap,
change and size. The first four are counts of the posts in the item's problem (feed/build.py clusters) by
post type; a post outside any problem counts alone. Size is a market-size figure, which Signal doesn't have
yet, so it is always empty: never estimated. An "idea" post (someone describing a problem along with the
product they'd want) counts as pain: it states the problem in the poster's own words. Only posts in the feed's
data are counted (X posts aren't), so the strip, the sources and the post list agree with one another.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta

from .data import FeedData, Item

TYPES = ("pain", "paid_demand", "tool_gap", "change")
WEEKS = 8


def members(it: Item, data: FeedData) -> list[Item]:
    """The posts in the item's problem (the item itself included), or just the item."""
    if it.item_type != "post" or it.problem_id is None:
        return [it] if it.item_type == "post" else []
    return [data.items[i] for i in data.by_problem.get(it.problem_id, [])] or [it]


AS_SIGNAL = {"idea": "pain"}


def counts(posts: list[Item]) -> dict[str, int]:
    c = Counter(AS_SIGNAL.get(p.post_type, p.post_type) for p in posts)
    return {t: c.get(t, 0) for t in TYPES}


def item_signals(it: Item, data: FeedData) -> dict[str, int] | None:
    """Counts per signal type, or None for an inferred problem (its card shows its evidence instead)."""
    posts = members(it, data)
    return counts(posts) if posts else None


def weekly(posts: list[Item], now: datetime) -> list[int]:
    """Posts published in each of the last WEEKS weeks, oldest first; the last one is the week to `now`."""
    out = [0] * WEEKS
    for p in posts:
        age = (now - p.published_at).total_seconds()
        k = int(age // timedelta(weeks=1).total_seconds())
        if 0 <= k < WEEKS:
            out[WEEKS - 1 - k] += 1
    return out


def problem_detail(it: Item, data: FeedData, written: dict[str, str], headline, now: datetime,
                   limit: int = 6) -> dict | None:
    """The item page's evidence: signal counts, weekly mentions, where the posts come from, other posts."""
    posts = members(it, data)
    if not posts:
        return None
    by_source = Counter(p.source for p in posts)
    others = sorted((p for p in posts if p.post_id != it.post_id), key=lambda p: p.published_at, reverse=True)
    return {"signals": counts(posts), "total": len(posts), "weekly": weekly(posts, now),
            "first_seen": min(p.published_at for p in posts).isoformat(),
            "sources": [{"source": s, "count": n} for s, n in by_source.most_common(8)],
            "sources_more": max(0, len(by_source) - 8),
            "posts": [{"item_type": "post", "item_id": p.item_id, "headline": headline(p, written),
                       "post_type": p.post_type, "source": p.source, "published_at": p.published_at.isoformat(),
                       "money": p.money} for p in others[:limit]],
            "roles": sorted({p.author_role for p in posts if p.author_role})[:3]}
