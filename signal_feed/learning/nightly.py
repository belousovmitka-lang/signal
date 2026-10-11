"""The nightly job (section 10): retrain the value model and the v2 ranking models when there is enough
data. Similar-builder neighbours are computed per request (from quiz overlap), so nothing to rebuild."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from signal_connectors.models import utcnow

from ..config import FeedConfig
from ..db import FeedStore
from .models import NotEnoughData, train_v2, train_value_model


@dataclass
class NightlyReport:
    value: dict = field(default_factory=dict)
    v2: dict = field(default_factory=dict)


def run_nightly(store: FeedStore, config: FeedConfig, clock: Callable[[], datetime] = utcnow,
                min_surveys: int | None = None, min_impressions: int | None = None) -> NightlyReport:
    """`min_*` override the config thresholds (simulations only)."""
    report = NightlyReport()
    try:
        row = train_value_model(store, config, clock, min_surveys)
        report.value = {"model_id": row.id, "status": row.status, **row.metrics}
    except NotEnoughData as exc:
        report.value = {"skipped": str(exc)}
    try:
        row = train_v2(store, config, clock, min_impressions)
        report.v2 = {"model_id": row.id, "status": row.status, **row.metrics}
    except NotEnoughData as exc:
        report.v2 = {"skipped": str(exc)}
    return report
