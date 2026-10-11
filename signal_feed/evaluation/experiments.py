"""A/B tests (section 13 "Online"): per-user assignment, config overrides per arm, and the report.

Assignment is sticky: the first time a user is seen, a hash of (experiment, user) picks the arm by the
configured shares, and the choice is stored. Ship rule from the spec: an arm ships only if it improves
the north star without hurting the "Not for me" rate or diversity.
"""

from __future__ import annotations

import copy
import hashlib
import math
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import select

from signal_connectors.models import utcnow

from ..config import Experiment, FeedConfig
from ..db import ExperimentAssignmentRow, FeedStore
from ..posts import PostsReader
from .metrics import weekly_metrics


def bucket(experiment: str, user_id: str) -> float:
    """A stable number in [0, 1) per (experiment, user)."""
    digest = hashlib.sha256(f"{experiment}:{user_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2 ** 64


def pick_arm(exp: Experiment, user_id: str) -> str:
    x, edge = bucket(exp.name, user_id), 0.0
    for name, arm in exp.arms.items():
        edge += arm.share
        if x < edge:
            return name
    return list(exp.arms)[-1]


def assign(store: FeedStore, config: FeedConfig, user_id: str, clock: Callable[[], datetime] = utcnow) -> dict[str, str]:
    """experiment -> arm for this user, assigning (and storing) any experiment they aren't in yet."""
    experiments = config.evaluation.experiments
    if not experiments:
        return {}
    with store.session() as s, s.begin():
        have = {r.experiment: r.arm for r in s.scalars(select(ExperimentAssignmentRow).where(
            ExperimentAssignmentRow.user_id == user_id))}
        out = {}
        for exp in experiments:
            arm = have.get(exp.name)
            if arm not in exp.arms:          # new experiment, or an arm that was removed: (re)assign
                arm = pick_arm(exp, user_id)
                row = s.get(ExperimentAssignmentRow, (user_id, exp.name))
                if row is None:
                    s.add(ExperimentAssignmentRow(user_id=user_id, experiment=exp.name, arm=arm, assigned_at=clock()))
                else:
                    row.arm, row.assigned_at = arm, clock()
            out[exp.name] = arm
    return out


def apply_overrides(config: FeedConfig, overrides: dict[str, Any]) -> FeedConfig:
    """A new config with dotted-path values replaced and the whole config validated again (a typo in a
    path or a bad value fails here, not silently)."""
    if not overrides:
        return config
    data = copy.deepcopy(config.model_dump())
    for path, value in overrides.items():
        node = data
        *parents, leaf = path.split(".")
        for key in parents:
            if not isinstance(node, dict) or key not in node:
                raise ValueError(f"unknown config path: {path}")
            node = node[key]
        if not isinstance(node, dict) or leaf not in node:
            raise ValueError(f"unknown config path: {path}")
        node[leaf] = value
    return type(config).model_validate(data)


def arm_settings(config: FeedConfig, arms: dict[str, str]) -> tuple[FeedConfig, tuple[str | None, int | None]]:
    """The config with every assigned arm's overrides, and the (ranker, model id) an arm asks for."""
    overrides: dict[str, Any] = {}
    ranker: tuple[str | None, int | None] = (None, None)
    for exp in config.evaluation.experiments:
        arm = exp.arms.get(arms.get(exp.name, ""))
        if arm is None:
            continue
        overrides |= arm.overrides
        if arm.ranker:
            ranker = (arm.ranker, arm.model_id)
    return apply_overrides(config, overrides), ranker


def _z_two_proportions(x1: int, n1: int, x2: int, n2: int) -> float | None:
    if not n1 or not n2:
        return None
    p = (x1 + x2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    return (x2 / n2 - x1 / n1) / se if se else None


def ab_report(store: FeedStore, posts: PostsReader, config: FeedConfig, experiment: str, now: datetime,
              days: int = 7, control: str = "control") -> dict:
    """Per-arm metrics and the ship verdict for each non-control arm."""
    exp = next((e for e in config.evaluation.experiments if e.name == experiment), None)
    with store.session() as s:
        rows = list(s.scalars(select(ExperimentAssignmentRow).where(ExperimentAssignmentRow.experiment == experiment)))
    arms: dict[str, list[str]] = {}
    for r in rows:
        arms.setdefault(r.arm, []).append(r.user_id)
    if exp is not None:
        for name in exp.arms:
            arms.setdefault(name, [])
    if control not in arms:
        control = next(iter(arms), control)
    out = {"experiment": experiment, "control": control, "arms": {}}
    for name, users in arms.items():
        m = weekly_metrics(store, posts, now, days, users=users)
        out["arms"][name] = {"users": m["window"]["users"], "north_star_builders": m["product"]["north_star_builders"],
                             "north_star_share": m["product"]["north_star_share"],
                             "not_for_me_rate": m["product"]["not_for_me_rate"],
                             "items_shown": m["window"]["items_shown"],
                             "distinct_niches_per_20": m["feed_health"]["distinct_niches_per_20"],
                             "saves_per_100_items": m["product"]["saves_per_100_items"]}
    base = out["arms"].get(control)
    for name, arm in out["arms"].items():
        if name == control or base is None:
            continue
        z_north = _z_two_proportions(base["north_star_builders"], base["users"], arm["north_star_builders"], arm["users"])
        nfm_base = round((base["not_for_me_rate"] or 0) * base["items_shown"])
        nfm_arm = round((arm["not_for_me_rate"] or 0) * arm["items_shown"])
        z_nfm = _z_two_proportions(nfm_base, base["items_shown"], nfm_arm, arm["items_shown"])
        reasons = []
        if z_north is None or z_north < 1.96:
            reasons.append(f"north star not significantly better (z = {None if z_north is None else round(z_north, 2)})")
        if z_nfm is not None and z_nfm > 1.96:
            reasons.append(f"'Not for me' rate significantly higher (z = {round(z_nfm, 2)})")
        if (base["distinct_niches_per_20"] or 0) and (arm["distinct_niches_per_20"] or 0) < 0.95 * base["distinct_niches_per_20"]:
            reasons.append("diversity down more than 5%")
        arm["verdict"] = "ship" if not reasons else "don't ship"
        arm["reasons"] = reasons
    return out
