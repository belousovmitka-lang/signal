"""The v1 ranker (section 8): no trained model. Each action's probability is a hand-set linear
function of the features, clipped to [0, 1]; the score is the sum of event weight x probability,
times the quality factor. Every term lands in the score breakdown."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from ..config import TAGGED_POST_TYPES, FeedConfig
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
    """Hand-set linear action probabilities (section 8.2 v1). Subclasses replace `probabilities`."""

    name = "v1"

    def __init__(self, config: FeedConfig, value_model=None) -> None:
        self.config = config
        self.value_model = value_model          # learning.models.ValueModel or None

    def probabilities(self, features: list[dict[str, float]]) -> list[dict[str, float]]:
        coefficients = self.config.ranking.v1.coefficients
        return [{action: min(1.0, max(0.0, sum(coef * f.get(name, 1.0 if name == "intercept" else 0.0)
                                               for name, coef in coefs.items())))
                 for action, coefs in coefficients.items()} for f in features]

    def type_draws(self, state: UserState, rng: np.random.Generator | None) -> dict[str, float] | None:
        """Thompson sampling over post types (section 10): one draw per type per page, mapped to -1..1."""
        rk = self.config.ranking
        if not rk.thompson_post_types or rng is None:
            return None
        half = rk.type_thompson_prior / 2
        types = sorted(set(TAGGED_POST_TYPES) | set(state.type_evidence))   # sorted: same draws in every process
        return {t: 2 * rng.beta(half + state.type_evidence.get(t, (0, 0))[0],
                                half + state.type_evidence.get(t, (0, 0))[1]) - 1 for t in types}

    def score(self, data: FeedData, state: UserState, candidates: list[Candidate], now: datetime,
              rng: np.random.Generator | None = None) -> list[Scored]:
        v1, um, rk = self.config.ranking.v1, self.config.user_model, self.config.ranking
        weights, qf, rings = self.config.events.weights, rk.quality_factor, um.rings
        saved = [data.by_id[p].index for p in state.saved if p in data.by_id]
        nfm = [data.by_id[p].index for p in state.not_for_me if p in data.by_id]
        idx = [c.index for c in candidates]
        sims_saved = data.vectors[idx] @ data.vectors[saved].T if saved and idx else None
        sims_nfm = data.vectors[idx] @ data.vectors[nfm].T if nfm and idx else None
        draws = self.type_draws(state, rng)
        # Committing to a problem is an explicit signal (section 6.7): its niche ranks as if well liked, without
        # touching the learned map.
        focus = set(data.industry.subtree(state.committed_niche)) \
            if state.mode == "committed" and state.committed_niche in data.industry.nodes else set()
        feats, extra = [], []
        for k, c in enumerate(candidates):
            it = data.items[c.index]
            affinity, node = niche_affinity(data, state, it.niche_id, um.inherit)
            if it.niche_id in focus:
                affinity, node = max(affinity, um.committed_affinity), state.committed_niche
            hours = max((now - it.published_at).total_seconds() / 3600, 0.0)
            feats.append({
                "affinity": affinity,
                "skill": max((state.skill.get(sid, 0.0) for sid in it.skill_ids), default=0.0),
                "save_sim": _scale(float(sims_saved[k].max()), v1.similarity_low, v1.similarity_high)
                if sims_saved is not None else 0.0,
                "nfm_sim": _scale(float(sims_nfm[k].max()), v1.similarity_low, v1.similarity_high)
                if sims_nfm is not None else 0.0,
                "type_pref": draws.get(it.post_type, 0.0) if draws is not None else state.type_pref.get(it.post_type, 0.0),
                "source_pref": state.source_pref.get(it.source, 0.0),
                "problem": it.problem_rank,
                "intensity": it.intensity,
                "money": 1.0 if it.money else 0.0,
                "buyer": it.buyer,
                "quality": it.quality,
                "fresh": 0.5 ** (hours / v1.fresh_half_life_hours),
                "social": min(1.0, c.social / 3),
            })
            f = feats[-1]
            # Relevance gates value: a strong post outside someone's interests isn't one they save or build from.
            f["relevance"] = max(f["affinity"], 0.5 * f["skill"], f["save_sim"])
            for name in ("problem", "intensity", "money", "buyer"):
                f[f"{name}_fit"] = f["relevance"] * f[name]
            similar = int((sims_saved[k] >= v1.similar_save_cosine).sum()) if sims_saved is not None else 0
            extra.append((affinity, node, similar))
        probs = self.probabilities(feats)
        useful = None
        if self.value_model is not None and feats:
            from ..learning.datasets import feature_vector
            from ..learning.models import value_points
            p_useful = self.value_model.predict(np.asarray([feature_vector(f) for f in feats]))
            useful = list(zip(p_useful.tolist(), value_points(p_useful, self.config).tolist()))
        out = []
        for k, c in enumerate(candidates):
            it = data.items[c.index]
            affinity, node, similar = extra[k]
            terms = {action: weights[action] * p for action, p in probs[k].items()}
            quality = (qf.base + qf.problem_score * it.problem_rank) * (1 - qf.toxicity_penalty * it.toxicity)
            raw = sum(terms.values())
            score = raw * quality
            breakdown = {"ranker": self.name, "source": c.source,
                         "features": {n: round(v, 3) for n, v in feats[k].items()},
                         "probabilities": {a: round(p, 4) for a, p in probs[k].items()},
                         "terms": {a: round(t, 4) for a, t in terms.items()},
                         "raw": round(raw, 4), "quality_factor": round(quality, 4)}
            if useful is not None:     # section 8.4: blend the value model into the score
                p_useful, points = useful[k]
                score = (1 - rk.value_model_blend) * score + rk.value_model_blend * points
                breakdown["value"] = {"p_useful": round(p_useful, 4), "points": round(points, 4),
                                      "blend": rk.value_model_blend, "model_id": self.value_model.model_id}
            ring = "core" if affinity >= rings.core_min else "regular" if affinity >= rings.regular_min else "exploring"
            breakdown["ring"] = ring
            out.append(Scored(c, round(score, 4), ring, node, similar, breakdown))
        return out


class RankerV2(RankerV1):
    """Section 8.2 v2: trained per-action models replace the hand-set probabilities; everything else
    (quality factor, value blend, breakdown) is shared with v1."""

    name = "v2"

    def __init__(self, config: FeedConfig, model, value_model=None) -> None:
        super().__init__(config, value_model)
        self.model = model                      # learning.models.V2Model

    def probabilities(self, features: list[dict[str, float]]) -> list[dict[str, float]]:
        from ..learning.datasets import feature_vector
        if not features:
            return []
        pred = self.model.predict(np.asarray([feature_vector(f) for f in features], dtype=np.float32))
        return [{a: float(pred[a][k]) for a in pred} for k in range(len(features))]
