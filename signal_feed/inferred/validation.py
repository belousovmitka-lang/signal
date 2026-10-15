"""Section 17.7: the validation loop.

- Insiders (users who say they work in an industry) get at most one prompt per week about a published
  inferred problem in their industry: "Is this real where you work? Yes / No / Partly + one line."
- Insider confirmations raise confidence (2 make it high, 17.6); insider rejections lower it one level and,
  for a low-confidence problem, reject it. "Not true in my experience" (17.8) records a "no" the same way.
- Builder field notes attach as evidence, only with the builder's permission.
- Learning: per detector and evidence-source combination, the share of problems that hold up (an insider
  confirmed it, or someone saved, turned it into an idea, or reached out) becomes a weight on their score.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy import func, select

from signal_connectors.models import utcnow

from ..config import FeedConfig
from ..db import (
    DetectorWeightRow,
    EventRow,
    FeedStore,
    InferredEvidenceRow,
    InferredProblemRow,
    InsiderPromptRow,
    InsiderVoteRow,
    NicheRow,
    UserQuizRow,
)
from ..enrichment.pii import scrub
from .common import Evidence, confidence

VOTES = ("yes", "no", "partly")
LEVELS = ("low", "medium", "high")
QUESTION = "Is this real where you work?"


class ValidationError(ValueError):
    pass


def _work_nodes(s, user_id: str) -> set[int]:
    """Industry nodes (roots or branches) the user says they work in."""
    quiz = s.get(UserQuizRow, user_id)
    out: set[int] = set()
    for label in (quiz.works_in or []) if quiz else []:
        parent = None
        node = None
        for part in [p.strip().lower() for p in label.split(">")]:
            q = select(NicheRow).where(NicheRow.tree == "industry", func.lower(NicheRow.label) == part)
            q = q.where(NicheRow.parent_id.is_(None)) if parent is None else q.where(NicheRow.parent_id == parent)
            node = s.scalars(q).first()
            if node is None:
                break
            parent = node.id
        if node is not None:
            out.add(node.id)
    return out


def _chain(s, niche_id: int | None) -> list[int]:
    out = []
    while niche_id is not None:
        out.append(niche_id)
        n = s.get(NicheRow, niche_id)
        niche_id = n.parent_id if n else None
    return out


def is_insider(s, user_id: str, niche_id: int | None) -> bool:
    """Works in the niche, one of its ancestors (the industry), or a branch inside it."""
    work = _work_nodes(s, user_id)
    return bool(work and (set(_chain(s, niche_id)) & work or any(niche_id in _chain(s, w) for w in work)))


def _evidence(s, inferred_id: int) -> list[InferredEvidenceRow]:
    return [e for e in s.scalars(select(InferredEvidenceRow).where(InferredEvidenceRow.inferred_id == inferred_id))
            if e.supported is not False]


def recompute(s, row: InferredProblemRow, config: FeedConfig) -> None:
    """Votes and field notes -> validation counts -> confidence (and rejection by insiders)."""
    vcfg, icfg = config.inferred.validation, config.inferred
    votes = list(s.scalars(select(InsiderVoteRow).where(InsiderVoteRow.inferred_id == row.id,
                                                        InsiderVoteRow.insider.is_(True))))
    counts = {v: sum(x.vote == v for x in votes) for v in VOTES}
    notes = sum(1 for e in _evidence(s, row.id) if e.source_type == "field_note")
    row.validation = {**counts, "field_notes": notes,
                      "non_insider_no": s.scalar(select(func.count()).select_from(InsiderVoteRow).where(
                          InsiderVoteRow.inferred_id == row.id, InsiderVoteRow.insider.is_(False),
                          InsiderVoteRow.vote == "no")) or 0}
    if row.status not in ("published", "candidate"):
        return
    ev = [Evidence(e.post_id, e.source, e.source_type, e.url, e.what_it_shows, counts=e.counts is not False)
          for e in _evidence(s, row.id)]
    critic = row.critic_notes or {}
    existing = any(c.get("post_id") for c in row.counter_evidence or []) or bool(critic.get("existing_solution"))
    level = confidence(ev, icfg, critic.get("verdict"), existing, insider_confirmations=counts["yes"])
    net_no = counts["no"] - counts["yes"] - 0.5 * counts["partly"]
    if level and net_no >= vcfg.downgrade_after_net_no:
        level = LEVELS[max(0, LEVELS.index(level) - 1)]            # rejections lower it one level...
        if level == "low" and net_no >= vcfg.reject_after_net_no:  # ...and reject it once it is low
            row.status, level = "rejected", None
            row.critic_notes = {**critic, "rejected_by_insiders": counts}
    row.confidence = level


def vote_in_session(s, config: FeedConfig, user_id: str, inferred_id: int, vote: str, note: str | None,
                    source: str, now: datetime) -> bool:
    if vote not in VOTES:
        raise ValidationError(f"vote must be one of {', '.join(VOTES)}")
    row = s.get(InferredProblemRow, inferred_id)
    if row is None:
        raise ValidationError(f"unknown inferred problem {inferred_id}")
    insider = is_insider(s, user_id, row.niche_id)
    for old in s.scalars(select(InsiderVoteRow).where(InsiderVoteRow.inferred_id == inferred_id,
                                                      InsiderVoteRow.user_id == user_id)):
        s.delete(old)
    s.flush()
    s.add(InsiderVoteRow(inferred_id=inferred_id, user_id=user_id, vote=vote, insider=insider, source=source,
                         note=scrub(note.strip()[:300]) if note else None, at=now))
    s.flush()
    recompute(s, row, config)
    return insider


def record_vote(store: FeedStore, config: FeedConfig, user_id: str, inferred_id: int, vote: str,
                note: str | None = None, source: str = "prompt", clock: Callable[[], datetime] = utcnow) -> bool:
    """Store a vote; returns whether the voter counted as an insider. A user's later vote replaces theirs."""
    with store.session() as s, s.begin():
        return vote_in_session(s, config, user_id, inferred_id, vote, note, source, clock())


def add_field_note(store: FeedStore, config: FeedConfig, user_id: str, inferred_id: int, note: str, share: bool,
                   clock: Callable[[], datetime] = utcnow) -> None:
    """A builder's customer-conversation note becomes evidence, only if they allow it (17.7)."""
    if not share:
        raise ValidationError("field notes attach only with the builder's permission (share = true)")
    with store.session() as s, s.begin():
        row = s.get(InferredProblemRow, inferred_id)
        if row is None:
            raise ValidationError(f"unknown inferred problem {inferred_id}")
        s.add(InferredEvidenceRow(inferred_id=inferred_id, post_id=None, record_id=f"note:{user_id}", source="field_note",
                                  source_type="field_note", url="", supported=True,
                                  what_it_shows="From a builder's customer conversation: " + scrub(note.strip()[:400])))
        s.flush()
        recompute(s, row, config)


def next_prompt(store: FeedStore, config: FeedConfig, user_id: str, clock: Callable[[], datetime] = utcnow) -> dict | None:
    """The week's insider question, or None (not an insider, asked recently, or nothing to ask)."""
    now = clock()
    every = timedelta(days=config.inferred.validation.prompt_every_days)
    with store.session() as s, s.begin():
        work = _work_nodes(s, user_id)
        if not work:
            return None
        last = s.scalar(select(func.max(InsiderPromptRow.prompted_at)).where(InsiderPromptRow.user_id == user_id))
        if last is not None and now - last < every:
            return None
        asked = set(s.scalars(select(InsiderPromptRow.inferred_id).where(InsiderPromptRow.user_id == user_id)))
        voted = set(s.scalars(select(InsiderVoteRow.inferred_id).where(InsiderVoteRow.user_id == user_id)))
        options = [p for p in s.scalars(select(InferredProblemRow).where(InferredProblemRow.status == "published")
                                        .order_by(InferredProblemRow.score.desc()))
                   if p.id not in asked | voted and is_insider(s, user_id, p.niche_id)]
        if not options:
            return None
        p = options[0]
        s.add(InsiderPromptRow(user_id=user_id, inferred_id=p.id, prompted_at=now))
        ev = _evidence(s, p.id)
        return {"inferred_id": p.id, "question": QUESTION, "hypothesis": p.hypothesis, "answers": list(VOTES),
                "evidence": [{"url": e.url, "source": e.source, "what_it_shows": e.what_it_shows} for e in ev[:3]],
                "evidence_more": max(0, len(ev) - 3)}


def combo_of(evidence: list[InferredEvidenceRow]) -> str:
    return "+".join(sorted({e.source_type for e in evidence})) or "none"


def update_weights(store: FeedStore, config: FeedConfig, clock: Callable[[], datetime] = utcnow) -> list[DetectorWeightRow]:
    """Weekly: per (detector, evidence combination), weight = 2 x (held up + prior/2) / (judged + prior),
    clamped. Held up: an insider confirmed it, or someone saved it, turned it into an idea or reached out.
    Judged: held up, or rejected by insiders. No data -> weight 1."""
    vcfg = config.inferred.validation
    now = clock()
    with store.session() as s, s.begin():
        acted = set(s.scalars(select(EventRow.item_id).where(
            EventRow.item_type == "inferred", EventRow.event_type.in_(("save", "idea", "outreach")), EventRow.weight > 0)))
        stats: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for p in s.scalars(select(InferredProblemRow).where(InferredProblemRow.status.in_(("published", "rejected")))):
            if p.status == "rejected" and not (p.critic_notes or {}).get("rejected_by_insiders"):
                continue                       # the critic's rejections say nothing about holding up in the field
            st = stats[(p.detector, combo_of(_evidence(s, p.id)))]
            v = p.validation or {}
            confirmed, did = v.get("yes", 0) > v.get("no", 0), str(p.id) in acted
            st["published"] += 1
            st["confirmed"] += confirmed
            st["acted"] += did
            st["held"] += confirmed or did                         # once per problem, whichever way
            st["rejected"] += p.status == "rejected" and not did
        out = []
        for (detector, combo), st in stats.items():
            held = st["held"]
            judged = held + st["rejected"]
            weight = 2 * (held + vcfg.weight_prior / 2) / (judged + vcfg.weight_prior)
            row = s.get(DetectorWeightRow, (detector, combo)) or DetectorWeightRow(detector=detector, combo=combo)
            row.weight = round(min(vcfg.weight_max, max(vcfg.weight_min, weight)), 4)
            row.published, row.confirmed, row.rejected, row.acted = (st["published"], st["confirmed"], st["rejected"],
                                                                     st["acted"])
            row.updated_at = now
            s.merge(row)
            out.append(row)
    return out


def live_detectors(s, config: FeedConfig) -> set[str]:
    """Detectors whose problems may enter the feed: `live` in config, or a `trial` detector whose published
    problems insiders have rated enough and confirmed often enough (17.11 step 5)."""
    icfg = config.inferred
    live = {d for d, stage in icfg.stages.items() if stage == "live"}
    for detector in (d for d, stage in icfg.stages.items() if stage == "trial"):
        rated = confirmed = 0
        for p in s.scalars(select(InferredProblemRow).where(InferredProblemRow.detector == detector,
                                                            InferredProblemRow.status.in_(("published", "rejected")))):
            v = p.validation or {}
            if v.get("yes", 0) + v.get("no", 0) + v.get("partly", 0):
                rated += 1
                confirmed += v.get("yes", 0) > v.get("no", 0)
        if rated >= icfg.promote_min_rated and confirmed / rated >= icfg.promote_min_confirmation:
            live.add(detector)
    return live
