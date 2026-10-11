"""The weekly niche-tree job (section 4.2): retire, split, merge, then re-place every post.

Split: a node with enough recent support has the posts sitting directly on it clustered (PCA, then
HDBSCAN). A cluster becomes a named child when it has enough posts, voices and sources and is not
just a copy of an existing sibling. Merge: siblings whose centroids are nearly the same collapse
into the larger one. Retire: a non-root node below `child_min_posts` for N weeks in a row hands its
posts back to its parent. Every change writes a `niche_versions` row so affinities can migrate.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import numpy as np
from sqlalchemy import select

from signal_connectors.models import RawPost, utcnow

from ..config import FeedConfig
from ..db import EnrichedPostRow, FeedStore, NicheRow, NicheSupportRow, NicheVersionRow, PostNicheRow
from ..posts import PostsReader
from ..vectors import centroid, unit_matrix
from .naming import NicheNamer
from .tree import INDUSTRY, SKILL, Tree, place_posts

log = logging.getLogger(__name__)


@dataclass
class GrowthStats:
    created: list[str] = field(default_factory=list)      # "Legal > Citation verification"
    merged: list[str] = field(default_factory=list)
    retired: list[str] = field(default_factory=list)
    placed: int = 0


def week_of(day: datetime) -> date:
    d = day.date()
    return d - timedelta(days=d.weekday())


def cluster_vectors(unit: np.ndarray, min_size: int, min_samples: int, pca_dims: int) -> list[np.ndarray]:
    """Index arrays of the dense clusters in `unit` (rows of length 1); noise points are left out."""
    from sklearn.cluster import HDBSCAN
    from sklearn.decomposition import PCA

    if len(unit) < max(min_size, 2):
        return []
    dims = min(pca_dims, len(unit) - 1, unit.shape[1])
    x = PCA(n_components=dims, random_state=0).fit_transform(unit) if dims < unit.shape[1] else unit
    x = x / np.where((n := np.linalg.norm(x, axis=1, keepdims=True)) == 0, 1, n)
    labels = HDBSCAN(min_cluster_size=min_size, min_samples=min(min_samples, min_size), copy=True).fit_predict(x)
    return [np.flatnonzero(labels == k) for k in sorted(set(labels) - {-1})]


class NicheGrower:
    def __init__(self, store: FeedStore, posts: PostsReader, config: FeedConfig, namer: NicheNamer,
                 clock: Callable[[], datetime] = utcnow) -> None:
        self.store, self.posts, self.config, self.namer, self.clock = store, posts, config, namer, clock
        self.cfg = config.niches

    # --- data ----------------------------------------------------------------

    def _load(self) -> tuple[list[EnrichedPostRow], dict[str, RawPost]]:
        with self.store.session() as s:
            rows = list(s.scalars(select(EnrichedPostRow).where(EnrichedPostRow.status == "ok",
                                                                EnrichedPostRow.embedding.is_not(None))))
        return rows, {p.id: p for p in self.posts.get_posts([r.post_id for r in rows])}

    def _recent(self, raw: dict[str, RawPost]) -> set[str]:
        since = self.clock() - timedelta(days=self.cfg.support_window_days)
        return {pid for pid, p in raw.items() if p.published_at >= since}

    def voices(self, post_ids: list[str], raw: dict[str, RawPost]) -> int:
        keys = set()
        for pid in post_ids:
            p = raw.get(pid)
            if p is None:
                continue
            if p.author_hash:
                keys.add(p.author_hash)
            elif self.cfg.authorless_counts_as_distinct:
                keys.add(pid)
        return len(keys)

    # --- the job -------------------------------------------------------------

    def run(self, *, split: bool = True) -> GrowthStats:
        stats = GrowthStats()
        rows, raw = self._load()
        by_id = {r.post_id: r for r in rows}
        recent = self._recent(raw)
        strict = self.cfg.child_min_cosine
        # First pass with each child's own floor only: what a niche attracts by its own definition is
        # what its cohesion is judged on (a strict floor would trim the loose tail and hide it).
        place_posts(self.store, rows)
        self._update_nodes(by_id, recent)
        for tree in (INDUSTRY, SKILL):
            stats.retired += self._retire(tree)
        place_posts(self.store, rows, strict)
        if split:
            for tree in (INDUSTRY, SKILL):
                stats.created += self._split(tree, by_id, raw, recent)
        for tree in (INDUSTRY, SKILL):
            stats.merged += self._merge(tree)
        stats.placed = len(place_posts(self.store, rows, strict))
        self._update_nodes(by_id, recent, record_week=True)
        return stats

    def _direct(self, tree: str) -> dict[int, list[str]]:
        with self.store.session() as s:
            out: dict[int, list[str]] = defaultdict(list)
            for pid, nid in s.execute(select(PostNicheRow.post_id, PostNicheRow.niche_id)
                                      .where(PostNicheRow.tree == tree)):
                out[nid].append(pid)
        return out

    def _update_nodes(self, by_id: dict[str, EnrichedPostRow], recent: set[str], record_week: bool = False) -> None:
        """Support (recent posts in the subtree) and centroid (all posts in the subtree) per active node."""
        week = week_of(self.clock())
        for tree in (INDUSTRY, SKILL):
            t, direct = Tree.load(self.store, tree), self._direct(tree)
            with self.store.session() as s, s.begin():
                for nid in t.nodes:
                    members = list({pid for sub in t.subtree(nid) for pid in direct.get(sub, [])})
                    node = s.get(NicheRow, nid)
                    node.support = sum(pid in recent for pid in members)
                    vecs = [by_id[p].embedding for p in members if p in by_id]
                    if vecs:
                        node.centroid_embedding = centroid(unit_matrix(vecs)).tolist()
                    if record_week:
                        s.merge(NicheSupportRow(niche_id=nid, week=week, support=node.support))

    def _version(self, s, node: NicheRow, change: str, detail: dict) -> None:
        node.version += 1
        s.add(NicheVersionRow(niche_id=node.id, version=node.version, change=change, detail=detail, at=self.clock()))

    def _cohesion(self, tree: str) -> dict[int, float]:
        """Median cosine of each node's directly placed posts to its centroid."""
        sims: dict[int, list[float]] = defaultdict(list)
        with self.store.session() as s:
            for nid, sim in s.execute(select(PostNicheRow.niche_id, PostNicheRow.similarity)
                                      .where(PostNicheRow.tree == tree, PostNicheRow.similarity.is_not(None))):
                sims[nid].append(sim)
        return {nid: float(np.median(v)) for nid, v in sims.items()}

    def _retire(self, tree: str) -> list[str]:
        """Retire grown nodes whose support stayed low for N weeks, or that are too loose to be a niche."""
        weeks = self.cfg.retire_after_weeks_below
        cohesion = self._cohesion(tree)
        out = []
        t = Tree.load(self.store, tree)
        with self.store.session() as s, s.begin():
            for nid, node in t.nodes.items():
                if node.parent_id is None or node.seeded:
                    continue
                if nid in cohesion and cohesion[nid] < self.cfg.child_min_cohesion:
                    row = s.get(NicheRow, nid)
                    row.status = "retired"
                    self._version(s, row, "retired", {"parent_id": node.parent_id, "reason": "loose",
                                                      "cohesion": round(cohesion[nid], 3)})
                    out.append(" > ".join(t.path(nid)))
                    continue
                history = list(s.scalars(select(NicheSupportRow.support).where(NicheSupportRow.niche_id == nid)
                                         .order_by(NicheSupportRow.week.desc()).limit(weeks)))
                if len(history) == weeks and all(v < self.cfg.child_min_posts for v in history):
                    row = s.get(NicheRow, nid)
                    row.status = "retired"
                    self._version(s, row, "retired", {"parent_id": node.parent_id, "weeks_below": weeks})
                    out.append(" > ".join(t.path(nid)))
        return out

    def _split(self, tree: str, by_id: dict[str, EnrichedPostRow], raw: dict[str, RawPost],
               recent: set[str]) -> list[str]:
        cfg = self.cfg
        t, direct = Tree.load(self.store, tree), self._direct(tree)
        created = []
        for nid in sorted(t.nodes, key=lambda n: t.nodes[n].level):
            node = t.nodes[nid]
            if node.support < cfg.split_min_support_90d:
                continue
            members = [pid for pid in direct.get(nid, []) if pid in recent and pid in by_id]
            if len(members) < cfg.child_min_posts:
                continue
            unit = unit_matrix([by_id[p].embedding for p in members])
            siblings = [t.nodes[c].label for c in t.children.get(nid, [])]
            sibling_centroids = [t.centroids[c] for c in t.children.get(nid, []) if c in t.centroids]
            for idx in cluster_vectors(unit, cfg.child_min_posts, cfg.hdbscan_min_samples, cfg.pca_dims):
                ids = [members[i] for i in idx]
                sources = {raw[p].source for p in ids if p in raw}
                if len(ids) < cfg.child_min_posts or self.voices(ids, raw) < cfg.child_min_authors \
                        or len(sources) < cfg.child_min_sources:
                    continue
                c = centroid(unit[idx])
                if any(float(c @ sc) >= cfg.merge_cosine for sc in sibling_centroids):
                    continue     # a copy of an existing child: its posts just didn't clear the bar
                sims = unit[idx] @ c
                # Judge cohesion the way the weekly retire step will: over every post on the parent this
                # child would attract with its own floor, not just the cluster's members.
                floor = float(np.percentile(sims, cfg.assign_percentile))
                attracted = unit @ c
                if float(np.median(attracted[attracted >= floor])) < cfg.child_min_cohesion:
                    continue     # a loose catch-all, not a niche
                samples = [by_id[ids[i]].pain_statement for i in np.argsort(-sims)
                           if by_id[ids[i]].pain_statement][:cfg.name_samples]
                level_hint = node.level        # industry_path[level] is the tagged niche one below
                hints = [by_id[p].industry_path[level_hint] for p in ids
                         if tree == INDUSTRY and len(by_id[p].industry_path or []) > level_hint]
                roles = [by_id[p].who_has_pain for p in ids if by_id[p].who_has_pain]
                label = self.namer.name(t.path(nid), siblings, samples, hints, roles)
                with self.store.session() as s, s.begin():
                    child = NicheRow(tree=tree, label=label, parent_id=nid, level=node.level + 1,
                                     centroid_embedding=c.tolist(), support=len(ids), status="active", version=1,
                                     created_at=self.clock(),
                                     assign_min_cosine=floor)
                    s.add(child)
                    s.flush()
                    s.add(NicheVersionRow(niche_id=child.id, version=1, change="created", at=self.clock(),
                                          detail={"parent_id": nid, "posts": len(ids), "sources": len(sources)}))
                    self._version(s, s.get(NicheRow, nid), "split", {"child_id": child.id, "label": label})
                siblings.append(label)
                sibling_centroids.append(c)
                created.append(" > ".join([*t.path(nid), label]))
                log.info("new niche %s (%d posts)", created[-1], len(ids))
        return created

    def _merge(self, tree: str) -> list[str]:
        t = Tree.load(self.store, tree)
        out = []
        with self.store.session() as s, s.begin():
            for parent, kids in t.children.items():
                alive = sorted((k for k in kids if k in t.centroids), key=lambda k: -t.nodes[k].support)
                gone: set[int] = set()
                for i, first in enumerate(alive):
                    for second in alive[i + 1:]:
                        if first in gone or second in gone:
                            continue
                        if float(t.centroids[first] @ t.centroids[second]) < self.cfg.merge_cosine:
                            continue
                        a, b = t.nodes[first], t.nodes[second]
                        if a.seeded and b.seeded:
                            continue                  # two seeds stay separate even when close
                        # The larger survives (alive is sorted by support), but a seed always survives.
                        keep, other = (second, first) if b.seeded else (first, second)
                        row = s.get(NicheRow, other)
                        row.status, row.merged_into_id = "merged", keep
                        self._version(s, row, "merged", {"into_id": keep})
                        self._version(s, s.get(NicheRow, keep), "merged", {"absorbed_id": other})
                        for child in t.children.get(other, []):     # the survivor adopts the children
                            s.get(NicheRow, child).parent_id = keep
                        gone.add(other)
                        out.append(f"{' > '.join(t.path(other))} -> {t.nodes[keep].label}")
        return out

