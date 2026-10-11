"""Problem fields and the problem score (section 5.2). Each component is 0-1; the score is their
weighted sum. Confidence (how much evidence there is) is reported next to it, never mixed in."""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta

from signal_connectors.models import RawPost

from ..config import ProblemsConfig
from ..db import EnrichedPostRow


@dataclass
class Evidence:
    row: EnrichedPostRow
    post: RawPost


def voices(evidence: list[Evidence], authorless_distinct: bool) -> int:
    keys = {e.post.author_hash or (e.post.id if authorless_distinct else None) for e in evidence}
    return len(keys - {None})


PAIN, DEMAND = "pain", "demand"


def components(evidence: list[Evidence], cfg: ProblemsConfig, now: datetime, authorless_distinct: bool,
               kind: str = PAIN) -> dict[str, float]:
    """Score components for a pain problem (section 5.2) or a demand item (tenders, jobs, gigs)."""
    n = len(evidence)
    window = timedelta(days=cfg.trend_window_days)
    current = sum(now - e.post.published_at <= window for e in evidence)
    previous = sum(window < now - e.post.published_at <= 2 * window for e in evidence)
    z = (current - previous) / math.sqrt(current + previous + 1)       # Poisson-style z of the change
    common = {
        "frequency": min(1.0, math.log1p(voices(evidence, authorless_distinct)) / math.log1p(cfg.frequency_full_at_voices)),
        "trend": 1 / (1 + math.exp(-z)),                                 # 0.5 = flat
        "money_signal": sum(e.row.money_signal is not None for e in evidence) / n,
        "source_diversity": min(1.0, len({e.post.source_type for e in evidence}) / cfg.source_types_full_at),
    }
    if kind == DEMAND:
        return common
    return common | {
        "intensity": sum(e.row.intensity or 0 for e in evidence) / n,
        "tool_gap": sum(e.row.post_type == "tool_gap" for e in evidence) / n,
        "buyer_likelihood": sum((e.row.buyer_likelihood or 0) >= cfg.buyer_min_likelihood for e in evidence) / n,
    }


def score(parts: dict[str, float], cfg: ProblemsConfig, kind: str = PAIN) -> float:
    weights = (cfg.demand_score_weights if kind == DEMAND else cfg.score_weights).model_dump()
    return round(sum(weights[k] * parts[k] for k in weights), 4)


def confidence(evidence: list[Evidence], cfg: ProblemsConfig) -> float:
    return round(min(1.0, math.log1p(len(evidence)) / math.log1p(cfg.confidence_full_at_posts)), 4)


def summary_fields(evidence: list[Evidence]) -> dict:
    """The descriptive fields a problem stores (section 5.1)."""
    dates = [e.post.published_at for e in evidence]
    tools = Counter(t for e in evidence for t in (e.row.tools_mentioned or []))
    roles = Counter(e.row.who_has_pain for e in evidence if e.row.who_has_pain)
    workarounds = list(dict.fromkeys(e.row.current_workaround for e in evidence if e.row.current_workaround))
    return {
        "sources": sorted({e.post.source for e in evidence}),
        "first_seen": min(dates), "last_seen": max(dates),
        "money_signals": [e.row.money_signal for e in evidence if e.row.money_signal][:10],
        "workarounds": workarounds[:5],
        "competitors": [t for t, _ in tools.most_common(5)],
        "roles": [r for r, _ in roles.most_common(3)],
        "post_types": dict(Counter(e.row.post_type for e in evidence)),
        "post_count": len(evidence),
    }
