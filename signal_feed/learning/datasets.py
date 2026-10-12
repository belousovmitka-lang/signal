"""Training rows from what the feed already logs.

Every impression stores the ranker's features (`score_breakdown.features`), so a model learns from the
exact inputs the ranker saw. Labels are what the user then did with that item: events linked to the
impression, or (when the client sent no impression id) to the same user, session and post.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

import numpy as np
from sqlalchemy import select

from ..db import EventRow, FeedStore, ImpressionRow, SurveyRow

FEATURES = ("affinity", "skill", "save_sim", "nfm_sim", "type_pref", "source_pref", "problem", "intensity",
            "money", "buyer", "quality", "fresh", "social", "relevance", "problem_fit", "intensity_fit", "money_fit",
            "buyer_fit")
# The ranker's actions; each is also an event type. An action counts only when its event carried weight
# (a 2-second "read" was discounted to 0 when it was logged).
ACTIONS = ("read", "save", "id_build", "idea", "outreach", "not_for_me", "scroll_past")


def feature_vector(features: dict) -> list[float]:
    return [float(features.get(name, 0.0)) for name in FEATURES]


@dataclass
class Dataset:
    x: np.ndarray                      # rows x FEATURES
    y: dict[str, np.ndarray]           # action -> 0/1 per row
    users: np.ndarray                  # user id per row (for user-level splits)
    impression_ids: np.ndarray
    sessions: np.ndarray               # "user/session" per row (offline replay ranks within a session)
    logged_scores: np.ndarray          # the score the feed used when it showed the item
    quality: np.ndarray                # the logged quality factor (replay re-uses it)


def impressions_dataset(store: FeedStore) -> Dataset:
    with store.session() as s:
        imps = list(s.execute(select(ImpressionRow.id, ImpressionRow.user_id, ImpressionRow.session_id,
                                     ImpressionRow.item_id, ImpressionRow.score_breakdown, ImpressionRow.score)
                              .where(ImpressionRow.candidate_source != "calibration")))
        acted: dict[tuple, set[str]] = defaultdict(set)
        for imp_id, uid, sid, item, etype, weight in s.execute(select(
                EventRow.impression_id, EventRow.user_id, EventRow.session_id, EventRow.item_id,
                EventRow.event_type, EventRow.weight)):
            if weight == 0:
                continue
            if imp_id is not None:
                acted[("imp", imp_id)].add(etype)
            acted[("item", uid, sid, item)].add(etype)
    rows, ys, users, ids, sessions, logged, quality = [], defaultdict(list), [], [], [], [], []
    for imp_id, uid, sid, item, breakdown, score in imps:
        feats = (breakdown or {}).get("features")
        if not feats:
            continue
        done = acted.get(("imp", imp_id)) or acted.get(("item", uid, sid, item), set())
        rows.append(feature_vector(feats))
        for action in ACTIONS:
            ys[action].append(1 if action in done else 0)
        users.append(uid)
        ids.append(imp_id)
        sessions.append(f"{uid}/{sid}")
        logged.append(score or 0.0)
        quality.append(breakdown.get("quality_factor", 1.0))
    x = np.asarray(rows, dtype=np.float32).reshape(-1, len(FEATURES))
    return Dataset(x, {a: np.asarray(ys[a], dtype=np.int8) for a in ACTIONS}, np.asarray(users), np.asarray(ids),
                   np.asarray(sessions), np.asarray(logged, dtype=np.float64), np.asarray(quality, dtype=np.float64))


def survey_dataset(store: FeedStore) -> tuple[np.ndarray, np.ndarray]:
    """("Was this useful?" answers) -> features of the impression they were asked on, and 1 = useful."""
    with store.session() as s:
        rows = list(s.execute(select(ImpressionRow.score_breakdown, SurveyRow.useful)
                              .join(ImpressionRow, ImpressionRow.id == SurveyRow.impression_id)))
    x = [feature_vector(b["features"]) for b, _ in rows if b and b.get("features")]
    y = [int(u) for b, u in rows if b and b.get("features")]
    return np.asarray(x, dtype=np.float32).reshape(-1, len(FEATURES)), np.asarray(y, dtype=np.int8)


def user_split(users: np.ndarray, holdout: float = 0.2, seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """Boolean train / test masks splitting by user, so a user's behaviour never leaks across the split."""
    unique = np.unique(users)
    rng = np.random.default_rng(seed)
    test_users = set(rng.choice(unique, size=max(1, int(round(len(unique) * holdout))), replace=False)) \
        if len(unique) > 1 else set()
    test = np.array([u in test_users for u in users])
    return ~test, test
