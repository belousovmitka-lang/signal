"""The v1 ranker (section 8): no trained model. Each action's probability is a hand-set linear
function of the features, clipped to [0, 1]; the score is the sum of event weight x probability,
times the quality factor. Every term lands in the score breakdown."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..config import FeedConfig
from .candidates import Candidate
from .data import FeedData
from .usermodel import UserState


@dataclass
class Scored:
    candidate: Candidate
    score: float
    ring: str                         # core | regular | exploring
    affinity_node: int | None         # the node whose affinity counted (leaf or an ancestor)
    similar_saves: int = 0            # the user's saves at least `similar_save_cosine` close
    breakdown: dict = field(default_factory=dict)

    @property
    def index(self) -> int:
        return self.candidate.index


def _scale(cos: float, low: float, high: float) -> float:
    return min(1.0, max(0.0, (cos - low) / (high - low)))


def niche_affinity(data: FeedData, state: UserState, niche_id: int, inherit: list[float]) -> tuple[float, int | None]:
    """The best of the leaf's affinity and its ancestors' (discounted by `inherit`); 0 under a
    removed node."""
    best, node = 0.0, None
    for depth, anc in enumerate(data.ancestors(niche_id)):
        if anc in state.removed:
            return 0.0, anc
        if depth < len(inherit):
            a = state.niche_affinity(anc) * inherit[depth]
            if a > best:
                best, node = a, anc
    return best, node


class RankerV1:
    def __init__(self, config: FeedConfig) -> None:
        self.config = config

    def score(self, data: FeedData, state: UserState, candidates: list[Candidate], now: datetime) -> list[Scored]:
        v1, um = self.config.ranking.v1, self.config.user_model
        weights, qf, rings = self.config.events.weights, self.config.ranking.quality_factor, um.rings
        saved = [data.by_id[p].index for p in state.saved if p in data.by_id]
        nfm = [data.by_id[p].index for p in state.not_for_me if p in data.by_id]
        idx = [c.index for c in candidates]
        sims_saved = data.vectors[idx] @ data.vectors[saved].T if saved and idx else None
        sims_nfm = data.vectors[idx] @ data.vectors[nfm].T if nfm and idx else None
        out = []
        for k, c in enumerate(candidates):
            it = data.items[c.index]
            affinity, node = niche_affinity(data, state, it.niche_id, um.inherit)
            hours = max((now - it.published_at).total_seconds() / 3600, 0.0)
            f = {
                "intercept": 1.0,
                "affinity": affinity,
                "skill": max((state.skill.get(sid, 0.0) for sid in it.skill_ids), default=0.0),
                "save_sim": _scale(float(sims_saved[k].max()), v1.similarity_low, v1.similarity_high)
                if sims_saved is not None else 0.0,
                "nfm_sim": _scale(float(sims_nfm[k].max()), v1.similarity_low, v1.similarity_high)
                if sims_nfm is not None else 0.0,
                "type_pref": state.type_pref.get(it.post_type, 0.0),
                "source_pref": state.source_pref.get(it.source, 0.0),
                "problem": it.problem_rank,
                "intensity": it.intensity,
                "money": 1.0 if it.money else 0.0,
                "buyer": it.buyer,
                "quality": it.quality,
                "fresh": 0.5 ** (hours / v1.fresh_half_life_hours),
                "social": min(1.0, c.social / 3),
            }
            probs = {action: min(1.0, max(0.0, sum(coef * f[name] for name, coef in coefs.items())))
                     for action, coefs in v1.coefficients.items()}
            terms = {action: weights[action] * p for action, p in probs.items()}
            quality = (qf.base + qf.problem_score * it.problem_rank) * (1 - qf.toxicity_penalty * it.toxicity)
            raw = sum(terms.values())
            ring = "core" if affinity >= rings.core_min else "regular" if affinity >= rings.regular_min else "exploring"
            similar = int((sims_saved[k] >= v1.similar_save_cosine).sum()) if sims_saved is not None else 0
            out.append(Scored(c, round(raw * quality, 4), ring, node, similar, {
                "ranker": "v1", "source": c.source, "ring": ring,
                "features": {n: round(v, 3) for n, v in f.items() if n != "intercept"},
                "probabilities": {a: round(p, 4) for a, p in probs.items()},
                "terms": {a: round(t, 4) for a, t in terms.items()},
                "raw": round(raw, 4), "quality_factor": round(quality, 4)}))
        return out
