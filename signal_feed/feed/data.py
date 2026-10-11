"""A read-only snapshot of everything the feed ranks: `ok` posts with their placement, problem and
embedding, plus the niche tree. Built once and reused until enrichment, placements or problems
change (a cheap fingerprint query decides)."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from threading import Lock

import numpy as np
from sqlalchemy import func, select

from ..db import EnrichedPostRow, FeedStore, NicheRow, PostNicheRow, ProblemPostRow, ProblemRow
from ..niches.tree import INDUSTRY, SKILL, Tree
from ..posts import PostsReader


@dataclass
class Item:
    """One rankable post."""

    post_id: str
    index: int                      # row in FeedData.vectors
    post_type: str
    source: str
    source_type: str
    url: str
    title: str | None
    summary: str | None             # the pain statement: the app shows summaries and links, not full text
    published_at: datetime
    niche_id: int                   # deepest industry node
    skill_ids: list[int]
    problem_id: int | None
    problem_kind: str | None
    problem_score: float
    problem_rank: float             # percentile of problem_score among problems of the same kind (pain|demand)
    problem_confidence: float
    similar_posts: int              # other posts in the same problem
    trend: float                    # the problem's trend component (0.5 = flat)
    intensity: float
    money: dict | None
    buyer: float
    quality: float
    toxicity: float
    author_role: str | None
    tools: tuple[str, ...] = ()     # products the post names (lower-cased), for committed-mode competitors


@dataclass
class FeedData:
    items: list[Item]
    by_id: dict[str, Item]
    vectors: np.ndarray             # unit rows, aligned with Item.index
    industry: Tree
    skill: Tree
    by_niche: dict[int, list[int]] = field(default_factory=dict)    # niche id -> item indexes (direct)
    by_skill: dict[int, list[int]] = field(default_factory=dict)
    fingerprint: tuple = ()

    def path(self, niche_id: int) -> list[str]:
        return self.industry.path(niche_id)

    def ancestors(self, niche_id: int) -> list[int]:
        """[leaf, parent, grandparent, ...] within the active industry tree."""
        out, node = [], niche_id
        while node is not None and node in self.industry.nodes:
            out.append(node)
            node = self.industry.nodes[node].parent_id
        return out

    def subtree_items(self, niche_id: int) -> list[int]:
        return [i for n in self.industry.subtree(niche_id) for i in self.by_niche.get(n, [])]


def fingerprint(store: FeedStore) -> tuple:
    with store.session() as s:
        return (s.scalar(select(func.count()).select_from(EnrichedPostRow).where(EnrichedPostRow.status == "ok")),
                s.scalar(select(func.max(EnrichedPostRow.enriched_at))),
                s.scalar(select(func.count()).select_from(PostNicheRow)),
                s.scalar(select(func.max(ProblemRow.updated_at))),
                s.scalar(select(func.max(NicheRow.version))))


def load(store: FeedStore, posts: PostsReader) -> FeedData:
    with store.session() as s:
        rows = {r.post_id: r for r in s.scalars(select(EnrichedPostRow).where(
            EnrichedPostRow.status == "ok", EnrichedPostRow.embedding.is_not(None)))}
        placed: dict[str, int] = {}
        skills: dict[str, list[int]] = defaultdict(list)
        for pid, nid, tree in s.execute(select(PostNicheRow.post_id, PostNicheRow.niche_id, PostNicheRow.tree)):
            if tree == INDUSTRY:
                placed[pid] = nid
            else:
                skills[pid].append(nid)
        problems = {p.id: p for p in s.scalars(select(ProblemRow).where(ProblemRow.status == "active"))}
        post_problem = dict(s.execute(select(ProblemPostRow.post_id, ProblemPostRow.problem_id)).all())
    raw = {p.id: p for p in posts.get_posts(list(rows))}
    items, vecs = [], []
    for pid, row in rows.items():
        post = raw.get(pid)
        if post is None or pid not in placed:
            continue
        prob = problems.get(post_problem.get(pid))
        items.append(Item(
            post_id=pid, index=len(items), post_type=row.post_type or "pain", source=post.source,
            source_type=post.source_type, url=post.url, title=post.title, summary=row.pain_statement,
            published_at=post.published_at, niche_id=placed[pid], skill_ids=skills.get(pid, []),
            problem_id=prob.id if prob else None, problem_kind=prob.kind if prob else None,
            problem_score=prob.score or 0.0 if prob else 0.0, problem_rank=0.0,
            problem_confidence=prob.confidence or 0.0 if prob else 0.0,
            similar_posts=max(0, (prob.post_count if prob else 1) - 1),
            trend=(prob.score_breakdown or {}).get("trend", 0.5) if prob else 0.5,
            intensity=row.intensity or 0.0, money=row.money_signal, buyer=row.buyer_likelihood or 0.0,
            quality=row.quality or 0.0, toxicity=row.toxicity or 0.0, author_role=post.author_role,
            tools=tuple(t.lower() for t in (row.tools_mentioned or []))))
        vecs.append(row.embedding)
    # Pain and demand scores come from different formulas: rank each within its kind so the ranker can
    # compare a pain post with a tender.
    by_kind: dict[str, list[float]] = defaultdict(list)
    for p in problems.values():
        by_kind[p.kind].append(p.score or 0.0)
    for kind in by_kind:
        by_kind[kind].sort()
    for it in items:
        scores = by_kind.get(it.problem_kind or "", [])
        if scores:
            it.problem_rank = float(np.searchsorted(scores, it.problem_score, side="right")) / len(scores)
    m = np.asarray(vecs, dtype=np.float32) if vecs else np.zeros((0, 1), dtype=np.float32)
    if len(m):
        m /= np.where((n := np.linalg.norm(m, axis=1, keepdims=True)) == 0, 1, n)
    data = FeedData(items=items, by_id={it.post_id: it for it in items}, vectors=m,
                    industry=Tree.load(store, INDUSTRY), skill=Tree.load(store, SKILL))
    by_niche, by_skill = defaultdict(list), defaultdict(list)
    for it in items:
        by_niche[it.niche_id].append(it.index)
        for sid in it.skill_ids:
            by_skill[sid].append(it.index)
    data.by_niche, data.by_skill = dict(by_niche), dict(by_skill)
    return data


class FeedDataCache:
    """One snapshot per process, rebuilt when the fingerprint changes."""

    def __init__(self, store: FeedStore, posts: PostsReader) -> None:
        self.store, self.posts = store, posts
        self._data: FeedData | None = None
        self._lock = Lock()

    def get(self) -> FeedData:
        fp = fingerprint(self.store)
        with self._lock:
            if self._data is None or self._data.fingerprint != fp:
                self._data = load(self.store, self.posts)
                self._data.fingerprint = fp
            return self._data
