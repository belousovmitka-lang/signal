"""Section 17.4: detectors -> candidate with evidence -> critic -> confidence -> publish or reject.

Re-runs keep an inferred problem's id when its evidence overlaps the old one; one that no longer
qualifies is marked expired. Every run writes a `detector_stats` row (17.10)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import delete, select

from signal_connectors.models import utcnow

from ..config import FeedConfig
from ..db import DetectorStatsRow, FeedStore, InferredEvidenceRow, InferredProblemRow
from ..llm_task import LLMTask
from ..posts import PostsReader
from ..problems.score import PAIN, components, score
from ..problems.score import Evidence as ScoreEvidence
from . import anchored, cross_industry, ignored_requests, labor_pressure, paid_workarounds, rule_change, tool_gaps
from .common import Candidate, confidence, meets_minimum
from .corroborate import Corroborator, Searcher
from .critic import run_critic

# Order matters: cross_industry reads the task clusters paid_workarounds writes.
DETECTORS = {m.NAME: m.find_candidates for m in (paid_workarounds, rule_change, tool_gaps, labor_pressure,
                                                 ignored_requests, cross_industry, anchored)}


@dataclass
class RunReport:
    detector: str
    clusters: int = 0
    candidates: int = 0
    published: int = 0
    rejected: int = 0
    expired: int = 0
    held: int = 0                 # candidates waiting for a critic (no LLM)
    critic_runs: int = 0
    near_misses: int = 0          # short of the evidence minimum even after corroboration (not stored)
    plans: int = 0                # corroboration planning calls
    searches: int = 0             # X searches
    search_stopped: str | None = None
    x_waiting: int = 0            # X posts found but not tagged yet (they count after the next enrich pass)
    items: list[str] = field(default_factory=list)


def run_detector(store: FeedStore, posts: PostsReader, config: FeedConfig, name: str, llm: LLMTask | None = None,
                 clock: Callable[[], datetime] = utcnow, searcher: Searcher | None = None) -> RunReport:
    """`searcher`: the X search used for corroboration (scripts/inferred.py passes the connector's, if built)."""
    now = clock()
    icfg = config.inferred
    raw_found, clusters = DETECTORS[name](store, posts, config, now)
    corr = Corroborator(store, posts, icfg.corroboration, llm, searcher, now)
    found = []
    near = 0
    for cand in raw_found:
        if cand.near_miss or cand.anchor:
            if not corr.corroborate(cand) or not meets_minimum(cand.evidence, icfg):
                near += 1
                continue
        found.append(cand)
    report = RunReport(name, clusters=clusters, candidates=len(found), near_misses=near, plans=corr.plans,
                       searches=corr.searches, search_stopped=corr.stopped,
                       x_waiting=corr.waiting)
    with store.session() as s, s.begin():
        old = {p.id: p for p in s.scalars(select(InferredProblemRow).where(
            InferredProblemRow.detector == name, InferredProblemRow.status.in_(("candidate", "published"))))}
        old_ev = {pid: set(s.scalars(select(InferredEvidenceRow.post_id).where(InferredEvidenceRow.inferred_id == pid)))
                  for pid in old}
    matched: set[int] = set()
    published_now: list[tuple[int, set[str], float, int]] = []     # (id, supported post ids, score, report line)
    for cand in found:
        ids = {e.post_id for e in cand.evidence}
        best, best_j = None, 0.0
        for pid, evs in old_ev.items():
            if pid in matched or not evs:
                continue
            j = len(ids & evs) / len(ids | evs)
            if j > best_j:
                best, best_j = pid, j
        keep_id = best if best_j >= icfg.match_min_jaccard else None
        if keep_id is not None:
            matched.add(keep_id)
        status, conf, notes = _judge(cand, llm, icfg, report)
        evidence_rows = cand.evidence
        parts = components([ScoreEvidence(r, p) for r, p in zip(cand.rows, cand.posts)], config.problems, now,
                           config.niches.authorless_counts_as_distinct, PAIN)
        with store.session() as s, s.begin():
            row = s.get(InferredProblemRow, keep_id) if keep_id is not None else None
            if row is None:
                row = InferredProblemRow(detector=name, created_at=now, status=status, hypothesis=cand.hypothesis,
                                         refreshed_at=now)
                s.add(row)
            if row.status == "published" and status == "candidate":
                status = "published"      # no critic this run: keep the earlier verdict rather than unpublish
                conf, notes = row.confidence, row.critic_notes
            row.hypothesis, row.who, row.task, row.niche_id = cand.hypothesis, cand.who, cand.task, cand.niche_id
            row.counter_evidence, row.derived, row.status = cand.counter_evidence, cand.derived, status
            row.confidence, row.critic_notes, row.refreshed_at = conf, notes, now
            row.score, row.score_breakdown = score(parts, config.problems, PAIN), {k: round(v, 4) for k, v in parts.items()}
            s.flush()
            s.execute(delete(InferredEvidenceRow).where(InferredEvidenceRow.inferred_id == row.id))
            s.add_all(InferredEvidenceRow(inferred_id=row.id, post_id=e.post_id, record_id=e.record_id, source=e.source,
                                          source_type=e.source_type, url=e.url, what_it_shows=e.what_it_shows,
                                          supported=e.supported, counts=e.counts) for e in evidence_rows)
            if status == "published":
                backing = {e.post_id for e in evidence_rows if e.supported and e.counts and e.post_id}
                published_now.append((row.id, backing, row.score or 0.0, len(report.items)))
        report.items.append(f"[{status}{'/' + conf if conf else ''}] {cand.hypothesis} ({len(cand.evidence)} items)")
    with store.session() as s, s.begin():
        # The same problem reached twice in one run (two articles on one event): keep the better-backed one.
        kept: list[tuple[int, set[str]]] = []
        for pid, backing, _, item in sorted(published_now, key=lambda t: (-len(t[1]), -t[2])):
            twin = next((k for k, kb in kept if len(backing & kb) >= icfg.duplicate_min_shared), None)
            if twin is None:
                kept.append((pid, backing))
                continue
            row = s.get(InferredProblemRow, pid)
            row.status, row.confidence = "rejected", None
            row.critic_notes = {**(row.critic_notes or {}), "duplicate_of": twin}
            report.published, report.rejected = report.published - 1, report.rejected + 1
            report.items[item] = report.items[item].replace("[published", f"[duplicate of #{twin}", 1)
        for pid in set(old) - matched:
            s.get(InferredProblemRow, pid).status = "expired"
            report.expired += 1
        s.add(DetectorStatsRow(detector=name, run_at=now, clusters=report.clusters, candidates=report.candidates,
                               published=report.published, rejected=report.rejected, expired=report.expired,
                               critic_runs=report.critic_runs,
                               with_counter_evidence=sum(1 for c in found if c.counter_evidence)))
    return report


def _judge(cand: Candidate, llm: LLMTask | None, icfg, report: RunReport) -> tuple[str, str | None, dict]:
    """Critic, then 17.5's automatic rule, then 17.6 confidence. Mutates evidence `supported` flags."""
    existing = any(c.get("post_id") for c in cand.counter_evidence)
    if llm is None:
        if icfg.publish_without_critic:
            report.published += 1
            return "published", confidence(cand.evidence, icfg, None, existing), {"critic": "not run"}
        report.held += 1
        return "candidate", confidence(cand.evidence, icfg, None, existing), {"critic": "not run"}
    texts = {r.post_id: (r.pain_statement or "") for r in cand.rows} | cand.texts
    verdict = run_critic(llm, cand.hypothesis, cand.evidence, texts, cand.counter_evidence)
    report.critic_runs += 1
    if verdict is None:
        report.held += 1
        return "candidate", confidence(cand.evidence, icfg, None, existing), {"critic": "no valid answer"}
    supported = {c.index: c.supports for c in verdict.checks}
    for k, e in enumerate(cand.evidence, 1):
        e.supported = supported.get(k, False)      # an item the critic didn't vouch for doesn't count
    kept = [e for e in cand.evidence if e.supported]
    notes = {"verdict": verdict.verdict, "reasons": verdict.reasons,
             "alternative_explanation": verdict.alternative_explanation,
             "existing_solution": verdict.existing_solution,
             "dropped": [e.url for e in cand.evidence if not e.supported]}
    if verdict.verdict == "reject" or not meets_minimum(kept, icfg):
        if verdict.verdict != "reject":
            notes["reasons"] = [*verdict.reasons, "too few supported evidence items after the critic's check"]
        report.rejected += 1
        return "rejected", None, notes
    conf = confidence(kept, icfg, verdict.verdict, existing or bool(verdict.existing_solution))
    if verdict.verdict == "revise":
        # Supported, but the wording overreaches: publish the critic's narrower wording, never the original.
        # Without one the claim still overreaches, so it only goes to exploration slots.
        revised = (verdict.revised_claim or "").strip().strip("{}[]\"'` ").strip()
        if revised:
            notes["original_claim"] = cand.hypothesis
            cand.hypothesis = revised if revised.endswith((".", "!", "?")) else revised + "."
        else:
            conf = "low"
    report.published += 1
    return "published", conf, notes
