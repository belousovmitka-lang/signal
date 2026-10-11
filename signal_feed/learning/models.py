"""The value model (section 8.4) and the v2 ranking models (section 8.2).

Value model: logistic regression on "Was this useful?" answers, stored as coefficients in `models`.
v2: one gradient-boosted classifier per action (scikit-learn's HistGradientBoosting: no new dependency),
saved with joblib under `ranking.models_dir`. Both start as `candidate`; the nightly job activates a
value model directly (it only reweights 20% of the score), while a v2 model must first beat v1 in the
offline comparison (Phase 7).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import numpy as np
from sqlalchemy import select

from signal_connectors.models import utcnow

from ..config import FeedConfig
from ..db import FeedStore, ModelRow
from ..settings import REPO_ROOT
from .datasets import ACTIONS, FEATURES, impressions_dataset, survey_dataset, user_split


class NotEnoughData(RuntimeError):
    pass


# --- value model -------------------------------------------------------------------

class ValueModel:
    def __init__(self, coef: list[float], intercept: float, model_id: int | None = None) -> None:
        self.coef, self.intercept, self.model_id = np.asarray(coef, dtype=np.float64), float(intercept), model_id

    def predict(self, x: np.ndarray) -> np.ndarray:
        """P(useful) per row of FEATURES."""
        return 1 / (1 + np.exp(-(x @ self.coef + self.intercept)))

    @classmethod
    def load_active(cls, store: FeedStore) -> ValueModel | None:
        with store.session() as s:
            row = s.scalars(select(ModelRow).where(ModelRow.kind == "value", ModelRow.status == "active")
                            .order_by(ModelRow.id.desc())).first()
        if row is None or row.features != list(FEATURES):
            return None
        return cls(row.params["coef"], row.params["intercept"], row.id)


def train_value_model(store: FeedStore, config: FeedConfig, clock: Callable[[], datetime] = utcnow,
                      min_answers: int | None = None) -> ModelRow:
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score

    x, y = survey_dataset(store)
    need = config.ranking.value_model_min_surveys if min_answers is None else min_answers
    if len(y) < need or len(set(y.tolist())) < 2:
        raise NotEnoughData(f"{len(y)} survey answers (need {need}, with both yes and no)")
    model = LogisticRegression(max_iter=1000, C=1.0).fit(x, y)
    p = model.predict_proba(x)[:, 1]
    metrics = {"answers": int(len(y)), "useful_share": round(float(y.mean()), 4),
               "train_auc": round(float(roc_auc_score(y, p)), 4)}
    with store.session() as s, s.begin():
        for old in s.scalars(select(ModelRow).where(ModelRow.kind == "value", ModelRow.status == "active")):
            old.status = "retired"
        row = ModelRow(kind="value", status="active", trained_at=clock(), examples=int(len(y)), features=list(FEATURES),
                       params={"coef": model.coef_[0].round(6).tolist(), "intercept": round(float(model.intercept_[0]), 6)},
                       metrics=metrics)
        s.add(row)
    return row


def value_points(p_useful: np.ndarray, config: FeedConfig) -> np.ndarray:
    """P(useful) on the score's scale: the expected "Was this useful?" event weight (+3 / -3)."""
    w = config.events.weights
    return p_useful * w["useful_yes"] + (1 - p_useful) * w["useful_no"]


# --- v2 ranking models -----------------------------------------------------------------

class V2Model:
    def __init__(self, models: dict, base_rates: dict[str, float], model_id: int | None = None) -> None:
        self.models, self.base_rates, self.model_id = models, base_rates, model_id

    def predict(self, x: np.ndarray) -> dict[str, np.ndarray]:
        out = {}
        for action in ACTIONS:
            m = self.models.get(action)
            out[action] = m.predict_proba(x)[:, 1] if m is not None else np.full(len(x), self.base_rates.get(action, 0.0))
        return out

    @classmethod
    def load(cls, store: FeedStore, status: str = "active", model_id: int | None = None) -> V2Model | None:
        import joblib

        with store.session() as s:
            q = select(ModelRow).where(ModelRow.kind == "v2")
            q = q.where(ModelRow.id == model_id) if model_id is not None else q.where(ModelRow.status == status)
            row = s.scalars(q.order_by(ModelRow.id.desc())).first()
        if row is None or row.features != list(FEATURES) or not Path(row.params["path"]).exists():
            return None
        bundle = joblib.load(row.params["path"])
        return cls(bundle["models"], bundle["base_rates"], row.id)


def train_v2(store: FeedStore, config: FeedConfig, clock: Callable[[], datetime] = utcnow,
             min_impressions: int | None = None) -> ModelRow:
    """Train one classifier per action on labelled impressions; register it as a `candidate`."""
    import joblib
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.metrics import roc_auc_score

    data = impressions_dataset(store)
    need = config.ranking.v2_min_labeled_impressions if min_impressions is None else min_impressions
    if len(data.x) < need:
        raise NotEnoughData(f"{len(data.x)} labelled impressions (need {need})")
    train, test = user_split(data.users)
    models, base_rates, metrics = {}, {}, {"impressions": int(len(data.x))}
    for action in ACTIONS:
        y = data.y[action]
        base_rates[action] = float(y.mean()) if len(y) else 0.0
        if len(set(y[train].tolist())) < 2:          # an action nobody (or everybody) took: use its base rate
            metrics[f"{action}_auc"] = None
            continue
        m = HistGradientBoostingClassifier(max_iter=200, learning_rate=0.08, max_leaf_nodes=15,
                                           l2_regularization=1.0, random_state=0).fit(data.x[train], y[train])
        models[action] = m
        ok = test.any() and len(set(y[test].tolist())) == 2
        metrics[f"{action}_auc"] = round(float(roc_auc_score(y[test], m.predict_proba(data.x[test])[:, 1])), 4) if ok else None
    now = clock()
    out_dir = Path(config.ranking.models_dir)
    out_dir = out_dir if out_dir.is_absolute() else REPO_ROOT / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"v2-{now.strftime('%Y%m%dT%H%M%S')}.joblib"
    joblib.dump({"models": models, "base_rates": base_rates, "features": list(FEATURES)}, path)
    with store.session() as s, s.begin():
        row = ModelRow(kind="v2", status="candidate", trained_at=now, examples=int(len(data.x)),
                       features=list(FEATURES), params={"path": str(path)},
                       metrics=metrics | {"base_rates": {k: round(v, 5) for k, v in base_rates.items()}})
        s.add(row)
    return row
