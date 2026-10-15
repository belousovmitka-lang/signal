"""Shared pieces: evidence records, buyer identity, confidence (17.6), catalog search for existing tools."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from signal_connectors.models import RawPost

from ..config import InferredConfig
from ..db import EnrichedPostRow


@dataclass
class Evidence:
    post_id: str | None
    source: str
    source_type: str
    url: str
    what_it_shows: str
    record_id: str | None = None
    supported: bool | None = None
    counts: bool = True              # False for a computed summary line: shown, never counted as evidence


@dataclass
class Candidate:
    detector: str
    hypothesis: str
    who: str | None
    task: str | None
    niche_id: int | None
    evidence: list[Evidence]
    counter_evidence: list[dict] = field(default_factory=list)
    derived: bool = False                      # built from pain statements, not v9 tasks
    rows: list[EnrichedPostRow] = field(default_factory=list)
    posts: list[RawPost] = field(default_factory=list)


def buyer_key(post: RawPost) -> str:
    """Who is paying: the author when known; for tenders and job posts the buying organization (SAM.gov's
    department is too coarse, so its agency path); otherwise the post itself."""
    if post.author_hash:
        return f"author:{post.author_hash}"
    path = (post.raw or {}).get("agency_path")
    if path:
        return f"org:{path if isinstance(path, str) else '/'.join(map(str, path))}"
    if post.community:
        return f"org:{post.community.strip().lower()}"
    return f"post:{post.id}"


def buyer_label(post: RawPost, row: EnrichedPostRow) -> str:
    """How an evidence line names the payer: the organization for tenders and job posts (public bodies and
    companies), otherwise the author's role, never a person."""
    if not post.author_hash and post.community:
        return post.community.strip()
    return f"a {row.who_has_pain}" if row.who_has_pain else "a poster"


def counted(evidence: list[Evidence]) -> list[Evidence]:
    return [e for e in evidence if e.counts]


def source_types(evidence: list[Evidence]) -> int:
    return len({e.source_type for e in counted(evidence)})


def meets_minimum(evidence: list[Evidence], cfg: InferredConfig) -> bool:
    return len(counted(evidence)) >= cfg.min_evidence and source_types(evidence) >= cfg.min_source_types


def confidence(evidence: list[Evidence], cfg: InferredConfig, verdict: str | None, existing_solution: bool,
               insider_confirmations: int = 0) -> str | None:
    """17.6. None when the minimum isn't met (no inferred problem then)."""
    if not meets_minimum(evidence, cfg):
        return None
    if insider_confirmations >= cfg.high_insider_confirmations:
        return "high"
    if (verdict == "keep" and len(counted(evidence)) >= cfg.high_evidence and source_types(evidence) >= cfg.high_source_types
            and not existing_solution):
        return "high"
    if verdict in ("keep", "revise"):
        return "medium"
    return "low"


def plural(who: str) -> str:
    """'public procurement office' -> 'Public procurement offices' (template filling, nothing clever)."""
    who = who.strip()
    head = who if who.endswith("s") else who + "s"
    return head[:1].upper() + head[1:]


def counter_evidence(centre: np.ndarray, vectors: np.ndarray, rows: list[EnrichedPostRow], posts: dict[str, RawPost],
                     cfg: InferredConfig, exclude: set[str]) -> list[dict]:
    """Catalog posts that suggest a tool already does this: positive reviews, tool listings, launches and
    complaints about a named tool, close to the cluster in meaning."""
    if not len(rows):
        return []
    sims = vectors @ centre
    out = []
    for k in np.argsort(-sims):
        if sims[k] < cfg.counter_evidence_cosine or len(out) >= cfg.counter_evidence_max:
            break
        row = rows[k]
        post = posts.get(row.post_id)
        if post is None or row.post_id in exclude:
            continue
        tools = ", ".join(row.tools_mentioned or []) or (post.title or "").strip()[:80]
        kind = {"praise": "a positive review", "listing": "a tool listing", "tool_gap": "a complaint about a tool",
                "change": "a launch or change"}.get(row.post_type, row.post_type)
        out.append({"post_id": row.post_id, "source": post.source, "url": post.url,
                    "what_it_shows": f"Possible existing tool ({kind}): {tools}", "similarity": round(float(sims[k]), 3)})
    return out
