""""Clip to Signal" clips as interest signals (the button and its table are the connectors' clips.py).

A clip is the user saying "this matters to me" about a page anywhere on the web, so it counts like a strong
save, with weight `user_model.clips.weight`. It isn't one of our posts, so it is placed by its nearest
posts: one embedding of title + selection + note (Voyage, the same space as the posts), then:
- interests: a weighted vote of the closest posts' niches (at least `min_cosine`, at least `min_neighbors`
  of them). The winning niche gets the evidence, with the usual share to its parents. A split vote moves
  up the tree until one node holds `niche_min_share`. Skills held by `skill_min_share` of them get evidence too.
- ranking: the clip's vector joins the user's saved posts, so posts like it rank higher (`save_sim`).

Clips stay the user's own. Their text is never copied into feed.db, a cluster or another user's feed: only
the vector and the evidence it added are kept, so deleting or editing a clip undoes or redoes exactly that.
Silent like everything else: nothing tells the user which niche a clip moved.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime

import numpy as np
from sqlalchemy import select

from signal_connectors.models import utcnow

from ..config import FeedConfig
from ..db import FeedStore, NicheRow, UserClipRow, UserNicheAffinityRow, UserRow, UserSkillAffinityRow
from ..enrichment.costs import CostLedger, Usage
from ..enrichment.embeddings import Embedder
from ..posts import PostsReader
from .data import FeedData
from .usermodel import _bump, _decayed

log = logging.getLogger(__name__)


def clip_text(clip, max_chars: int) -> str:
    return "\n".join(p for p in (clip.title, clip.text, clip.note) if p and p.strip())[:max_chars]


def place(data: FeedData, vector: np.ndarray, config: FeedConfig) -> tuple[dict[int, float], dict[int, float]]:
    """Evidence shares per niche and skill node for a clip vector, from its nearest posts. Empty when too
    few posts are close enough to say."""
    cfg = config.user_model.clips
    if not len(data.items):
        return {}, {}
    sims = data.vectors @ vector
    top = [int(k) for k in np.argsort(-sims)[: cfg.neighbors] if sims[k] >= cfg.min_cosine]
    if len(top) < cfg.min_neighbors:
        return {}, {}
    total = float(sum(sims[k] for k in top))
    # Niche: vote at the leaves; if no leaf holds the share, every vote moves up one level, and so on.
    chains = {k: data.ancestors(data.items[k].niche_id) for k in top if data.items[k].niche_id is not None}
    niches: dict[int, float] = {}
    for depth in range(max((len(c) for c in chains.values()), default=0)):
        votes: dict[int, float] = defaultdict(float)
        for k, chain in chains.items():
            if chain:
                votes[chain[min(depth, len(chain) - 1)]] += float(sims[k])
        if votes:
            node, w = max(votes.items(), key=lambda kv: kv[1])
            if w / total >= cfg.niche_min_share:
                niches = {node: w / total}
                break
    skill_votes: dict[int, float] = defaultdict(float)
    for k in top:
        for sid in data.items[k].skill_ids:
            skill_votes[sid] += float(sims[k])
    skills = {sid: w / total for sid, w in skill_votes.items() if w / total >= cfg.skill_min_share}
    return niches, skills


def _unbump(s, cls, user_id: str, node_id: int, added: float, added_at: datetime, prior: float,
            config: FeedConfig, now: datetime) -> None:
    """Take back what a clip added, as decayed since it was added (the rest has already faded)."""
    um = config.user_model
    row = s.get(cls, (user_id, node_id))
    if row is None:
        return
    row.alpha, row.beta = _decayed(row.alpha, row.beta, prior, um.prior_strength,
                                   (now - row.updated_at).total_seconds() / 86400, um.evidence_half_life_days)
    left = added * 0.5 ** (max((now - added_at).total_seconds(), 0.0) / 86400 / um.evidence_half_life_days)
    row.alpha = max(prior * um.prior_strength, row.alpha - left)
    row.updated_at = now


def sync_clips(store: FeedStore, posts: PostsReader, config: FeedConfig, data: FeedData, embedder: Embedder | None,
               user_id: str | None = None, clock: Callable[[], datetime] = utcnow) -> int:
    """Learn from new and edited clips, undo deleted ones. Returns how many clips changed. Without an embedder,
    deletions are still undone and new clips wait."""
    um, ecfg = config.user_model, config.embeddings
    now = clock()
    clips = {c.id: c for c in posts.clips(user_id)}
    with store.session() as s:
        q = select(UserClipRow) if user_id is None else select(UserClipRow).where(UserClipRow.user_id == user_id)
        known = {r.clip_id: r for r in s.scalars(q)}
        for r in known.values():
            s.expunge(r)
        users = set(s.scalars(select(UserRow.id).where(UserRow.id.in_({c.user_id for c in clips.values()}))))
    gone = [r for cid, r in known.items() if cid not in clips or clips[cid].updated_at != r.clip_updated_at]
    todo = [c for c in clips.values() if c.user_id in users
            and (c.id not in known or c.updated_at != known[c.id].clip_updated_at) and clip_text(c, 1)]
    if gone:
        with store.session() as s, s.begin():
            _undo(s, gone, config, now)
    if not todo or embedder is None:
        return len(gone)
    ledger = CostLedger(store)
    texts = [clip_text(c, ecfg.max_input_chars) for c in todo]
    ledger.check("voyage", sum(map(len, texts)) / 2 * ecfg.price_per_mtok / 1_000_000, ecfg.monthly_budget_usd, now)
    result = embedder.embed(texts)
    ledger.record(provider="voyage", purpose="embed_clip", usage=Usage(result.model, result.total_tokens),
                  usd=result.total_tokens * ecfg.price_per_mtok / 1_000_000, at=now)
    evidence = um.clips.weight * um.evidence_per_weight
    parents = None
    with store.session() as s, s.begin():
        for clip, vec in zip(todo, result.vectors, strict=True):
            v = np.asarray(vec, dtype=np.float32)
            v = v / (np.linalg.norm(v) or 1.0)
            niches, skills = place(data, v, config) if len(v) == data.vectors.shape[1] else ({}, {})
            priors = (s.get(UserRow, clip.user_id).feed_prefs or {}).get("priors", {})
            nd: dict[str, float] = {}
            if niches:
                parents = parents or dict(s.execute(select(NicheRow.id, NicheRow.parent_id)).all())
                (node, share), = niches.items()
                for k in (1.0, um.propagation.parent, um.propagation.grandparent):
                    if node is None:
                        break
                    nd[str(node)] = round(evidence * share * k, 6)
                    _bump(s, UserNicheAffinityRow, clip.user_id, node, nd[str(node)],
                          priors.get(f"n{node}", um.default_affinity), config, now)
                    node = parents.get(node)
            sd = {str(sid): round(evidence * share, 6) for sid, share in skills.items()}
            for sid, d in sd.items():
                _bump(s, UserSkillAffinityRow, clip.user_id, int(sid), d,
                      priors.get(f"s{sid}", um.default_affinity), config, now)
            s.merge(UserClipRow(clip_id=clip.id, user_id=clip.user_id, embedding=v.tolist(), niche_deltas=nd,
                                skill_deltas=sd, clip_updated_at=clip.updated_at, applied_at=now,
                                clip_created_at=clip.created_at))
    return len(gone) + len(todo)


def _undo(s, rows: list[UserClipRow], config: FeedConfig, now: datetime) -> None:
    um = config.user_model
    for r in rows:
        user = s.get(UserRow, r.user_id)
        priors = (user.feed_prefs or {}).get("priors", {}) if user else {}
        for cls, deltas, prefix in ((UserNicheAffinityRow, r.niche_deltas, "n"), (UserSkillAffinityRow, r.skill_deltas, "s")):
            for node, d in (deltas or {}).items():
                _unbump(s, cls, r.user_id, int(node), d, r.applied_at,
                        priors.get(f"{prefix}{node}", um.default_affinity), config, now)
        row = s.get(UserClipRow, r.clip_id)
        if row is not None:
            s.delete(row)


def clip_vectors(store: FeedStore, user_id: str, limit: int) -> list[list[float]]:
    """The user's most recent clips' vectors, for ranking (like saved posts)."""
    if limit <= 0:
        return []
    with store.session() as s:
        return [v for v in s.scalars(select(UserClipRow.embedding).where(UserClipRow.user_id == user_id)
                                     .order_by(UserClipRow.clip_created_at.desc()).limit(limit)) if v]
