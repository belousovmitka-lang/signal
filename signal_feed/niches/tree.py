"""Seed the two niche trees and place posts in them (section 4.1).

Roots are seeded by name: the 15 industry seed maps plus "Other", and the starter skills plus
"Other skills"; each industry also gets its branches from config/niches.yaml. A post reaches a root
by the name it was tagged with and a seeded branch by its first tagged sub-niche, then descends
into the child whose centroid it is closest to, as long as it clears that child's
`assign_min_cosine`.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
from sqlalchemy import delete, select

from signal_connectors.models import utcnow

from ..db import EnrichedPostRow, FeedStore, NicheRow, NicheVersionRow, PostNicheRow
from ..enrichment.schema import industry_names, niche_seeds, skill_vocabulary

INDUSTRY, SKILL = "industry", "skill"
OTHER_INDUSTRY, OTHER_SKILLS = "Other", "Other skills"


def seed_labels() -> dict[str, list[str]]:
    return {INDUSTRY: [*industry_names(), OTHER_INDUSTRY],
            SKILL: [name for name, _ in skill_vocabulary()] + [OTHER_SKILLS]}


def seed_tree(store: FeedStore, clock: Callable[[], datetime] = utcnow) -> int:
    """Create missing roots and seeded industry branches. Idempotent; returns how many were created.
    A seed removed from the config is left in place (it may hold posts and affinities)."""
    created = 0

    def add(s, tree: str, label: str, parent: NicheRow | None) -> NicheRow:
        nonlocal created
        node = NicheRow(tree=tree, label=label, parent_id=parent.id if parent else None,
                        level=parent.level + 1 if parent else 1, support=0, status="active", version=1,
                        created_at=clock(), seeded=True)
        s.add(node)
        s.flush()
        s.add(NicheVersionRow(niche_id=node.id, version=1, change="created", detail={"seeded": True}, at=clock()))
        created += 1
        return node

    with store.session() as s, s.begin():
        nodes = list(s.scalars(select(NicheRow).where(NicheRow.status == "active")))
        roots = {(n.tree, n.label): n for n in nodes if n.parent_id is None}
        children = {(n.parent_id, n.label.lower()): n for n in nodes if n.parent_id is not None}
        for tree, labels in seed_labels().items():
            for label in labels:
                if (tree, label) not in roots:
                    roots[(tree, label)] = add(s, tree, label, None)
                else:
                    roots[(tree, label)].seeded = True
        for industry, branches in niche_seeds().items():
            root = roots[(INDUSTRY, industry)]
            for name, _ in branches:
                existing = children.get((root.id, name.lower()))
                if existing is None:
                    add(s, INDUSTRY, name, root)
                elif not existing.seeded:
                    existing.seeded = True   # a grown niche that a seed now names
    return created


@dataclass
class Tree:
    """The active nodes of one tree, loaded for placement."""

    nodes: dict[int, NicheRow]
    roots: dict[str, int]                                  # lowercased label -> root id
    children: dict[int, list[int]] = field(default_factory=lambda: defaultdict(list))
    centroids: dict[int, np.ndarray] = field(default_factory=dict)

    @classmethod
    def load(cls, store: FeedStore, tree: str) -> Tree:
        with store.session() as s:
            nodes = {n.id: n for n in s.scalars(select(NicheRow).where(NicheRow.tree == tree,
                                                                         NicheRow.status == "active"))}
        t = cls(nodes=nodes, roots={n.label.lower(): n.id for n in nodes.values() if n.parent_id is None})
        for n in nodes.values():
            if n.parent_id is not None and n.parent_id in nodes:
                t.children[n.parent_id].append(n.id)
            if n.centroid_embedding:
                c = np.asarray(n.centroid_embedding, dtype=np.float32)
                t.centroids[n.id] = c / (np.linalg.norm(c) or 1)
        return t

    def path(self, node_id: int) -> list[str]:
        out = []
        while node_id is not None and node_id in self.nodes:
            out.append(self.nodes[node_id].label)
            node_id = self.nodes[node_id].parent_id
        return out[::-1]

    def child_by_label(self, parent: int, label: str) -> int | None:
        """A seeded child of `parent` named `label` (case-insensitive): posts reach those by their tag."""
        wanted = label.strip().lower()
        for child in self.children.get(parent, []):
            node = self.nodes[child]
            if node.seeded and node.label.lower() == wanted:
                return child
        return None

    def subtree(self, node_id: int) -> list[int]:
        out, stack = [], [node_id]
        while stack:
            n = stack.pop()
            out.append(n)
            stack.extend(self.children.get(n, []))
        return out

    def descend(self, start: int, vec: np.ndarray | None, min_cosine: float = 0.0) -> tuple[int, float | None]:
        """Walk down from `start` while the closest child accepts the post: at or above the child's own
        floor and `min_cosine`, and closer to the child than to the node it is leaving."""
        node, sim = start, None
        if vec is None:
            return node, sim
        while True:
            best, best_sim = None, -1.0
            for child in self.children.get(node, []):
                c = self.centroids.get(child)
                if c is None:
                    continue
                cs = float(vec @ c)
                if cs > best_sim:
                    best, best_sim = child, cs
            floor = self.nodes[best].assign_min_cosine if best is not None else None
            if best is None or floor is None or best_sim < max(floor, min_cosine):
                return node, sim
            here = self.centroids.get(node)
            if here is not None and best_sim <= float(vec @ here):
                return node, sim
            node, sim = best, best_sim


def _unit(vec: list[float] | None) -> np.ndarray | None:
    if not vec:
        return None
    v = np.asarray(vec, dtype=np.float32)
    n = np.linalg.norm(v)
    return v / n if n else None


def placements(rows: Iterable[EnrichedPostRow], industry: Tree, skill: Tree,
               min_cosine: float = 0.0) -> list[PostNicheRow]:
    """Where each post sits: one industry node, plus one node per tagged skill."""
    out: list[PostNicheRow] = []
    for row in rows:
        vec = _unit(row.embedding)
        top = (row.industry_path or [OTHER_INDUSTRY])[0].lower()
        root = industry.roots.get(top, industry.roots[OTHER_INDUSTRY.lower()])
        start = root
        if len(row.industry_path or []) > 1:
            start = industry.child_by_label(root, row.industry_path[1]) or root
        node, sim = industry.descend(start, vec, min_cosine)
        out.append(PostNicheRow(post_id=row.post_id, niche_id=node, tree=INDUSTRY, similarity=sim))
        seen: set[int] = set()
        for name in row.skills_needed or []:
            root = skill.roots.get(name.strip().lower(), skill.roots[OTHER_SKILLS.lower()])
            node, sim = skill.descend(root, vec, min_cosine)
            if node not in seen:
                seen.add(node)
                out.append(PostNicheRow(post_id=row.post_id, niche_id=node, tree=SKILL, similarity=sim))
    return out


def place_posts(store: FeedStore, rows: list[EnrichedPostRow], min_cosine: float = 0.0) -> list[PostNicheRow]:
    """Recompute every placement (cheap: one dot product per child per level)."""
    placed = placements(rows, Tree.load(store, INDUSTRY), Tree.load(store, SKILL), min_cosine)
    with store.session() as s, s.begin():
        s.execute(delete(PostNicheRow))
        s.add_all(placed)
    return placed
