"""Offline replay (section 13): re-rank held-out users' logged sessions with a ranker and measure
NDCG@20 on saves and on ideas. Uses the features and quality factor each impression logged, so nothing
about past state has to be reconstructed.

The held-out users are the same split v2 training used (`user_split`, seed 0), so a v2 candidate is
always scored on users it never saw. Both rankers are compared without the value-model blend (it is the
same for either), on the sessions where the user saved (or turned into an idea) at least one item.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable

import numpy as np
from sqlalchemy import select

from ..config import FeedConfig
from ..db import FeedStore, ModelRow
from ..feed.ranker import RankerV1, RankerV2
from ..learning.datasets import FEATURES, Dataset, impressions_dataset, user_split
from ..learning.models import V2Model


def ndcg(scores: np.ndarray, gains: np.ndarray, k: int) -> float:
    order = np.argsort(-scores, kind="stable")[:k]
    discounts = 1 / np.log2(np.arange(2, len(order) + 2))
    dcg = float((gains[order] * discounts).sum())
    ideal = float((np.sort(gains)[::-1][:k] * discounts[:min(k, len(gains))]).sum())
    return dcg / ideal if ideal else 0.0


def _scorer(ranker: RankerV1, config: FeedConfig) -> Callable[[np.ndarray, np.ndarray], np.ndarray]:
    weights = config.events.weights

    def score(x: np.ndarray, quality: np.ndarray) -> np.ndarray:
        probs = ranker.probabilities([dict(zip(FEATURES, row.tolist())) for row in x])
        raw = np.array([sum(weights[a] * p for a, p in pr.items()) for pr in probs])
        return raw * quality
    return score


def evaluate(data: Dataset, mask: np.ndarray, score_fn, k: int) -> dict:
    """Mean NDCG@k over sessions in `mask` with at least one positive, for saves and for ideas."""
    by_session = defaultdict(list)
    for i in np.flatnonzero(mask):
        by_session[data.sessions[i]].append(i)
    out = {}
    for label in ("save", "idea"):
        vals = []
        for rows in by_session.values():
            rows = np.asarray(rows)
            gains = data.y[label][rows].astype(float)
            if gains.sum() == 0:
                continue
            vals.append(ndcg(score_fn(rows), gains, k))
        out[label] = {"ndcg": round(float(np.mean(vals)), 4) if vals else None, "sessions": len(vals)}
    return out


def compare(store: FeedStore, config: FeedConfig, candidate_id: int | None = None) -> dict:
    """v1 (current coefficients), the logged order, and a v2 candidate (latest if not given) on held-out users."""
    ev = config.evaluation
    data = impressions_dataset(store)
    if not len(data.x):
        return {"verdict": "no data"}
    _, test = user_split(data.users)
    v1 = _scorer(RankerV1(config), config)
    report = {"held_out_users": int(len(set(data.users[test]))),
              "logged": evaluate(data, test, lambda r: data.logged_scores[r], ev.ndcg_k),
              "v1": evaluate(data, test, lambda r: v1(data.x[r], data.quality[r]), ev.ndcg_k)}
    if candidate_id is None:
        with store.session() as s:
            candidate_id = s.scalar(select(ModelRow.id).where(ModelRow.kind == "v2", ModelRow.status == "candidate")
                                    .order_by(ModelRow.id.desc()))
    model = V2Model.load(store, model_id=candidate_id) if candidate_id is not None else None
    if model is None:
        return report | {"verdict": "no v2 candidate"}
    v2 = _scorer(RankerV2(config, model), config)
    report["v2"] = evaluate(data, test, lambda r: v2(data.x[r], data.quality[r]), ev.ndcg_k)
    report["candidate_id"] = candidate_id
    s1, s2 = report["v1"]["save"], report["v2"]["save"]
    i1, i2 = report["v1"]["idea"]["ndcg"], report["v2"]["idea"]["ndcg"]
    reasons = []
    if s2["sessions"] < ev.min_sessions:
        reasons.append(f"only {s2['sessions']} held-out sessions with a save (need {ev.min_sessions})")
    elif s2["ndcg"] is None or s1["ndcg"] is None or s2["ndcg"] < s1["ndcg"] + ev.min_gain:
        reasons.append(f"saves NDCG {s2['ndcg']} vs v1 {s1['ndcg']}: not {ev.min_gain} better")
    if i1 is not None and i2 is not None and i2 < i1 - ev.idea_tolerance:
        reasons.append(f"ideas NDCG {i2} is worse than v1 {i1}")
    report["verdict"] = "promote" if not reasons else "keep v1"
    report["reasons"] = reasons
    return report


def promote_if_better(store: FeedStore, config: FeedConfig, candidate_id: int | None = None) -> dict:
    """Activate the v2 candidate only when `compare` says so; the previous active v2 is retired."""
    report = compare(store, config, candidate_id)
    if report.get("verdict") == "promote":
        with store.session() as s, s.begin():
            for old in s.scalars(select(ModelRow).where(ModelRow.kind == "v2", ModelRow.status == "active")):
                old.status = "retired"
            row = s.get(ModelRow, report["candidate_id"])
            row.status = "active"
            row.metrics = dict(row.metrics) | {"offline": {k: report[k] for k in ("v1", "v2", "logged")}}
        report["promoted"] = report["candidate_id"]
    return report
