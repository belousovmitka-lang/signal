"""Tagging-quality check (FEED_ALGORITHM.md section 15, Phase 1): a labeled sample of posts and a
scorer that compares the tagger's output with the labels.

Labels live in evals/enrichment_labels.yaml: post ids, links and labels only, never post text.
"""

from __future__ import annotations

import random
from collections import Counter, defaultdict
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from signal_connectors.models import RawPost

from ..db import EnrichedPostRow
from ..settings import REPO_ROOT
from ..config import TaggedPostType

LABELS_FILE = REPO_ROOT / "evals" / "enrichment_labels.yaml"

# Share of the sample per source type: every type is represented, the noisy ones a little more.
SAMPLE_PLAN = {"community": 40, "social": 35, "news": 35, "government": 40, "research": 15, "review": 15,
               "startup": 20}

Level = Literal["low", "medium", "high"]


class Label(BaseModel):
    model_config = ConfigDict(extra="forbid")

    post_id: str
    source: str
    post_type: TaggedPostType
    industry: str
    money: bool                      # the post states money (budget, rate, salary, price, contract value)
    spam: bool                       # would be dropped as spam (ads, self-promotion, bait, adult content)
    intensity: Level
    buyer: Level
    who_has_pain: str | None = None
    pain_statement: str | None = None
    unsure: bool = False             # the labeler was not confident: review these first
    note: str | None = None
    reviewed: bool = False           # a human checked (and maybe corrected) this label


class LabelFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    labeler: str
    guide: str
    labels: list[Label] = Field(default_factory=list)


def load_labels(path: Path = LABELS_FILE) -> LabelFile:
    return LabelFile.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def draw_sample(posts: list[RawPost], skip_sources: list[str], plan: dict[str, int] = SAMPLE_PLAN,
                seed: int = 7) -> list[RawPost]:
    """Per source type, spread picks round-robin across sources so no single feed dominates."""
    rng = random.Random(seed)
    by_type: dict[str, dict[str, list[RawPost]]] = defaultdict(lambda: defaultdict(list))
    for p in posts:
        if not any(p.source.startswith(s) for s in skip_sources):
            by_type[p.source_type][p.source].append(p)
    sample = []
    for source_type, want in plan.items():
        pools = [rng.sample(v, len(v)) for _, v in sorted(by_type.get(source_type, {}).items())]
        rng.shuffle(pools)
        picked: list[RawPost] = []
        while len(picked) < want and any(pools):
            for pool in pools:
                if pool and len(picked) < want:
                    picked.append(pool.pop())
        sample.extend(picked)
    return sample


def level(value: float | None) -> Level | None:
    if value is None:
        return None
    return "low" if value < 0.34 else "medium" if value < 0.67 else "high"


@dataclass
class Score:
    n: int = 0
    missing: int = 0                     # labeled posts with no usable enrichment yet
    post_type: Counter = field(default_factory=Counter)        # (label, predicted) -> count
    industry_hits: int = 0
    money: Counter = field(default_factory=Counter)            # tp / fp / fn / tn
    spam: Counter = field(default_factory=Counter)
    feed: Counter = field(default_factory=Counter)             # kept in the feed (status ok)
    intensity_hits: int = 0
    buyer_hits: int = 0
    disagreements: list[tuple[Label, EnrichedPostRow]] = field(default_factory=list)

    def accuracy(self, hits: int) -> float:
        return hits / self.n if self.n else 0.0

    @property
    def post_type_accuracy(self) -> float:
        return self.accuracy(sum(c for (a, b), c in self.post_type.items() if a == b))

    @staticmethod
    def prf(c: Counter) -> tuple[float, float]:
        precision = c["tp"] / (c["tp"] + c["fp"]) if c["tp"] + c["fp"] else 1.0
        recall = c["tp"] / (c["tp"] + c["fn"]) if c["tp"] + c["fn"] else 1.0
        return precision, recall


def _confusion(c: Counter, truth: bool, predicted: bool) -> None:
    c[("tp" if predicted else "fn") if truth else ("fp" if predicted else "tn")] += 1


def score(labels: list[Label], enriched: dict[str, EnrichedPostRow], spam_threshold: float,
          drop_types: Collection[str] = ()) -> Score:
    s = Score()
    for label in labels:
        row = enriched.get(label.post_id)
        if row is None or row.post_type is None:
            s.missing += 1
            continue
        s.n += 1
        s.post_type[(label.post_type, row.post_type)] += 1
        industry = row.industry_path[0] if row.industry_path else None
        s.industry_hits += industry == label.industry
        _confusion(s.money, label.money, row.money_signal is not None)
        _confusion(s.spam, label.spam, (row.spam_score or 0) > spam_threshold)
        _confusion(s.feed, label.post_type not in drop_types and not label.spam, row.status == "ok")
        s.intensity_hits += level(row.intensity) == label.intensity
        s.buyer_hits += level(row.buyer_likelihood) == label.buyer
        if label.post_type != row.post_type or industry != label.industry:
            s.disagreements.append((label, row))
    return s


def report(s: Score) -> str:
    lines = [f"scored {s.n} labeled posts ({s.missing} not enriched yet)"]
    if not s.n:
        return lines[0]
    mp, mr = Score.prf(s.money)
    sp, sr = Score.prf(s.spam)
    fp, fr = Score.prf(s.feed)
    lines += [
        f"kept in feed        precision {fp:6.1%}  recall {fr:6.1%}",
        f"post_type accuracy  {s.post_type_accuracy:6.1%}",
        f"industry accuracy   {s.accuracy(s.industry_hits):6.1%}",
        f"money signal        precision {mp:6.1%}  recall {mr:6.1%}",
        f"spam drop           precision {sp:6.1%}  recall {sr:6.1%}",
        f"intensity level     {s.accuracy(s.intensity_hits):6.1%}  (low <0.34 <= medium < 0.67 <= high)",
        f"buyer level         {s.accuracy(s.buyer_hits):6.1%}",
        "", "post_type confusion (rows: label, columns: tagger):",
    ]
    types = sorted({t for pair in s.post_type for t in pair})
    lines.append(" " * 13 + "".join(f"{t[:11]:>12}" for t in types))
    for a in types:
        lines.append(f"{a[:12]:<13}" + "".join(f"{s.post_type.get((a, b), 0):>12}" for b in types))
    return "\n".join(lines)
