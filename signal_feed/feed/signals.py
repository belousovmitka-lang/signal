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

import numpy as np

from .data import FeedData, Item

TYPES = ("pain", "paid_demand", "tool_gap", "change")
WEEKS = 8
NEAR_IDENTICAL = 0.95      # posts this alike say the same thing (a tender republished per school)
AS_SIGNAL = {"idea": "pain"}


def signal_type(it: Item) -> str:
    return AS_SIGNAL.get(it.post_type, it.post_type)


def link(data: FeedData, cfg=None) -> None:
    """Group posts by problem (a post outside any problem is its own group) and link groups of different
    signal types that describe the same thing: centroid cosine >= `link_cosine` and the same industry. A
    group's signals are its own posts plus its linked groups' posts. Links are direct, not transitive, so
    a chain of loose matches can't grow into one blob. Sets data.groups, data.group_of, data.links and each
    post's `signal_types`."""
    from ..config import SignalsConfig
    cfg = cfg or SignalsConfig()
    keyed: dict[object, int] = {}
    groups: list[list[int]] = []
    group_of: dict[int, int] = {}
    for it in data.items:
        if it.item_type != "post":
            continue
        key = ("problem", it.problem_id) if it.problem_id is not None else ("post", it.post_id)
        if key not in keyed:
            keyed[key] = len(groups)
            groups.append([])
        groups[keyed[key]].append(it.index)
        group_of[it.index] = keyed[key]
    data.groups, data.group_of, data.links = groups, group_of, {}
    if not groups:
        return
    centroids = np.stack([data.vectors[g].mean(axis=0) for g in groups]).astype(np.float32)
    centroids /= np.where((n := np.linalg.norm(centroids, axis=1, keepdims=True)) == 0, 1, n)
    kind = np.array([Counter(signal_type(data.items[i]) for i in g).most_common(1)[0][0] for g in groups])
    root = np.array([data.ancestors(data.items[g[0]].niche_id)[-1] if data.items[g[0]].niche_id in
                     data.industry.nodes else -1 for g in groups])
    links: dict[int, set[int]] = {}
    for b in range(0, len(groups), 2048):
        sims = centroids[b:b + 2048] @ centroids.T
        for j, row in enumerate(sims):
            g = b + j
            hits = np.nonzero((row >= cfg.link_cosine) & (kind != kind[g]) & (root == root[g]))[0]
            for h in hits.tolist():
                links.setdefault(g, set()).add(h)
                links.setdefault(h, set()).add(g)
    data.links = {g: tuple(sorted(v)) for g, v in links.items()}
    for g, idx in enumerate(groups):
        n_types = len({signal_type(data.items[i]) for k in (g, *data.links.get(g, ())) for i in groups[k]} & set(TYPES))
        for i in idx:
            data.items[i].signal_types = max(1, n_types)


def members(it: Item, data: FeedData) -> list[Item]:
    """The posts behind the item's signals: its problem's posts and those of problems linked to it
    (the item itself included)."""
    if it.item_type != "post":
        return []
    g = data.group_of.get(it.index)
    if g is None:
        return [it]
    return [data.items[i] for k in (g, *data.links.get(g, ())) for i in data.groups[k]]


def counts(posts: list[Item]) -> dict[str, int]:
    c = Counter(signal_type(p) for p in posts)
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
    # The same tender is often republished school by school: list each near-identical post once.
    others, alike, kept = [], 0, [data.vectors[it.index]]
    for p in sorted((p for p in posts if p.post_id != it.post_id), key=lambda p: p.published_at, reverse=True):
        if max(float(data.vectors[p.index] @ v) for v in kept) >= NEAR_IDENTICAL:
            alike += 1
            continue
        kept.append(data.vectors[p.index])
        others.append(p)
    return {"signals": counts(posts), "total": len(posts), "weekly": weekly(posts, now),
            "first_seen": min(p.published_at for p in posts).isoformat(),
            "sources": [{"source": s, "count": n} for s, n in by_source.most_common(8)],
            "sources_more": max(0, len(by_source) - 8),
            "posts": [{"item_type": "post", "item_id": p.item_id, "headline": headline(p, written),
                       "post_type": p.post_type, "source": p.source, "published_at": p.published_at.isoformat(),
                       "money": p.money} for p in others[:limit]],
            "posts_more": max(0, len(others) - limit), "posts_alike": alike,
            "roles": sorted({p.author_role for p in posts if p.author_role})[:3]}
