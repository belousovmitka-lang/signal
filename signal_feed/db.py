"""Feed tables (FEED_ALGORITHM.md section 11) and the FeedStore that writes them.

Feed tables have their own metadata and, for now, their own database. Links to raw posts
(`post_id`) are plain indexed columns, not foreign keys: posts can be purged at any time
when a platform's terms require it, and the feed must never block that.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    func,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from signal_connectors.models import utcnow
from signal_connectors.store import UTCDateTime


class FeedBase(DeclarativeBase):
    pass


# Store Python None as SQL NULL (plain JSON stores the JSON literal null, which IS NULL never matches).
NullableJSON = JSON(none_as_null=True)


# --- Stage A: enrichment -------------------------------------------------------

class EnrichedPostRow(FeedBase):
    """1:1 with raw posts (section 3.1)."""

    __tablename__ = "enriched_posts"

    post_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # ok: ranked | dropped: a drop_post_types type or spam, kept for audits | skipped: pre-filtered, no model call
    # needs_review: no valid tags after retries (fields hold the best partial result, if any)
    status: Mapped[str] = mapped_column(String(16), index=True)
    status_reason: Mapped[str | None] = mapped_column(String(64))
    post_type: Mapped[str | None] = mapped_column(String(16), index=True)
    industry_path: Mapped[list[str]] = mapped_column(JSON, default=list)
    skills_needed: Mapped[list[str]] = mapped_column(JSON, default=list)
    who_has_pain: Mapped[str | None] = mapped_column(String(200))
    pain_statement: Mapped[str | None] = mapped_column(Text)
    current_workaround: Mapped[str | None] = mapped_column(Text)
    intensity: Mapped[float | None] = mapped_column(Float)
    money_signal: Mapped[dict[str, Any] | None] = mapped_column(NullableJSON)
    buyer_likelihood: Mapped[float | None] = mapped_column(Float)
    quality: Mapped[float | None] = mapped_column(Float)
    spam_score: Mapped[float | None] = mapped_column(Float)
    toxicity: Mapped[float | None] = mapped_column(Float)
    embedding: Mapped[list[float] | None] = mapped_column(NullableJSON)   # JSON on SQLite; a vector column on Postgres
    pain_embedding: Mapped[list[float] | None] = mapped_column(NullableJSON)  # pain_statement alone (problem clustering)
    tools_mentioned: Mapped[list[str] | None] = mapped_column(NullableJSON)  # existing products named in the post
    # Section 17 (inferred problems), prompt v9+:
    task: Mapped[str | None] = mapped_column(Text)                        # normalized verb phrase
    task_automatable: Mapped[float | None] = mapped_column(Float)         # 0 physical/judgment .. 1 routine info work
    task_embedding: Mapped[list[float] | None] = mapped_column(NullableJSON)
    data_moved: Mapped[list[dict[str, Any]] | None] = mapped_column(NullableJSON)   # [{from_tool, to_tool, data}]
    obligation_ids: Mapped[list[int] | None] = mapped_column(NullableJSON)
    language: Mapped[str | None] = mapped_column(String(16))
    confidence: Mapped[float | None] = mapped_column(Float)
    escalated: Mapped[bool] = mapped_column(Boolean, default=False)
    prompt_version: Mapped[str] = mapped_column(String(32), index=True)
    prompt_hash: Mapped[str | None] = mapped_column(String(16))     # fingerprint of the exact prompt (tagged rows)
    model: Mapped[str | None] = mapped_column(String(64))           # the model whose tags were kept
    embedding_model: Mapped[str | None] = mapped_column(String(64))
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    enriched_at: Mapped[datetime] = mapped_column(UTCDateTime)


class ObligationRow(FeedBase):
    """What a rule document makes someone do (section 17, Detector 2), extracted at tagging."""

    __tablename__ = "obligations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    post_id: Mapped[str] = mapped_column(String(64), index=True)
    who: Mapped[str] = mapped_column(Text)
    what: Mapped[str] = mapped_column(Text)
    deadline: Mapped[date | None] = mapped_column(Date)
    industries: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    embedding: Mapped[list[float] | None] = mapped_column(NullableJSON)   # of "who: what", for role matching


class EnrichmentBatchRow(FeedBase):
    """A Claude Message Batch in flight (backfills). Results are collected by a later run."""

    __tablename__ = "enrichment_batches"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)    # Anthropic batch id
    model: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str] = mapped_column(String(32))
    post_ids: Mapped[list[str]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), index=True)      # open | collected
    reserved_usd: Mapped[float] = mapped_column(Float)              # worst-case cost held against the budget
    submitted_at: Mapped[datetime] = mapped_column(UTCDateTime)
    collected_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class FeedCostRow(FeedBase):
    """Every paid call the feed makes (Claude tagging, Voyage embeddings)."""

    __tablename__ = "feed_costs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    provider: Mapped[str] = mapped_column(String(16), index=True)   # anthropic | voyage
    purpose: Mapped[str] = mapped_column(String(16))                # tag | escalate | embed
    model: Mapped[str] = mapped_column(String(64))
    batch: Mapped[bool] = mapped_column(Boolean, default=False)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0)
    usd: Mapped[float] = mapped_column(Float)
    post_id: Mapped[str | None] = mapped_column(String(64))
    batch_id: Mapped[str | None] = mapped_column(String(64))
    request_id: Mapped[str | None] = mapped_column(String(64))


# --- Stage B: niche tree -------------------------------------------------------

class NicheRow(FeedBase):
    __tablename__ = "niches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tree: Mapped[str] = mapped_column(String(16), index=True)      # industry | skill
    label: Mapped[str] = mapped_column(String(200))
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("niches.id"), index=True)
    level: Mapped[int] = mapped_column(Integer)
    centroid_embedding: Mapped[list[float] | None] = mapped_column(NullableJSON)
    support: Mapped[int] = mapped_column(Integer, default=0)       # posts in the last 90 days
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | merged | retired
    merged_into_id: Mapped[int | None] = mapped_column(ForeignKey("niches.id"))
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    # A post descends into this child when its cosine to the centroid is at least this (set at creation
    # from the cluster's own spread); None for seeded roots, which posts reach by their tagged name.
    assign_min_cosine: Mapped[float | None] = mapped_column(Float)
    # Seeded nodes (roots and config/niches.yaml branches) always exist: never retired or merged away.
    seeded: Mapped[bool] = mapped_column(Boolean, default=False)
    # Seeded branches only: an embedding of "Industry > Branch: what it covers", so posts whose tag names
    # no branch can still be placed by meaning; label_text detects edits to config/niches.yaml.
    label_embedding: Mapped[list[float] | None] = mapped_column(NullableJSON)
    label_text: Mapped[str | None] = mapped_column(Text)


class PostNicheRow(FeedBase):
    """The deepest node a post sits in, per tree: one industry row per post, one skill row per skill."""

    __tablename__ = "post_niches"

    post_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    niche_id: Mapped[int] = mapped_column(ForeignKey("niches.id"), primary_key=True, index=True)
    tree: Mapped[str] = mapped_column(String(16), index=True)
    similarity: Mapped[float | None] = mapped_column(Float)   # cosine to the node's centroid


class NicheSupportRow(FeedBase):
    """Weekly support per node, for "retire after N weeks below threshold" (section 4.2 step 4)."""

    __tablename__ = "niche_support"

    niche_id: Mapped[int] = mapped_column(ForeignKey("niches.id"), primary_key=True)
    week: Mapped[date] = mapped_column(Date, primary_key=True)   # Monday of the ISO week
    support: Mapped[int] = mapped_column(Integer)


class NicheVersionRow(FeedBase):
    """Every tree change, so user affinities can be migrated (section 4.2 step 5)."""

    __tablename__ = "niche_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    niche_id: Mapped[int] = mapped_column(ForeignKey("niches.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    change: Mapped[str] = mapped_column(String(16))   # created | renamed | split | merged | retired
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    at: Mapped[datetime] = mapped_column(UTCDateTime)


# --- Stage C: problems ---------------------------------------------------------

class ProblemRow(FeedBase):
    __tablename__ = "problems"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    niche_id: Mapped[int | None] = mapped_column(ForeignKey("niches.id"), index=True)
    title: Mapped[str] = mapped_column(Text)
    distinct_authors: Mapped[int] = mapped_column(Integer, default=0)
    sources: Mapped[list[str]] = mapped_column(JSON, default=list)
    first_seen: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_seen: Mapped[datetime | None] = mapped_column(UTCDateTime)
    money_signals: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    workarounds: Mapped[list[str]] = mapped_column(JSON, default=list)
    competitors: Mapped[list[str]] = mapped_column(JSON, default=list)
    score: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[float | None] = mapped_column(Float)
    score_breakdown: Mapped[dict[str, float]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)
    # Problems are rebuilt each run; ids are kept by post overlap. A problem with no successor is
    # marked gone, not deleted, because events and users may point at it.
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)   # active | gone
    kind: Mapped[str] = mapped_column(String(16), default="pain", index=True)       # pain | demand
    post_count: Mapped[int] = mapped_column(Integer, default=0)
    roles: Mapped[list[str]] = mapped_column(JSON, default=list)        # most common who_has_pain
    post_types: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)


class ProblemPostRow(FeedBase):
    __tablename__ = "problem_posts"

    problem_id: Mapped[int] = mapped_column(ForeignKey("problems.id"), primary_key=True)
    post_id: Mapped[str] = mapped_column(String(64), primary_key=True, index=True)
    similarity: Mapped[float | None] = mapped_column(Float)


# --- Stage D: users ------------------------------------------------------------

class UserRow(FeedBase):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    mode: Mapped[str] = mapped_column(String(16), default="exploring")   # exploring | committed
    committed_problem_id: Mapped[int | None] = mapped_column(ForeignKey("problems.id"))
    auto_publish_profile: Mapped[bool] = mapped_column(Boolean, default=False)
    type_preference: Mapped[dict[str, float]] = mapped_column(JSON, default=dict)
    source_preference: Mapped[dict[str, float]] = mapped_column(JSON, default=dict)
    # hidden_sources, served_problems ("already exists"), min_problem_score ("too small"), see section 6.3
    feed_prefs: Mapped[dict[str, Any] | None] = mapped_column(NullableJSON)


class UserQuizRow(FeedBase):
    __tablename__ = "user_quiz"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    industries: Mapped[list[str]] = mapped_column(JSON, default=list)
    skills: Mapped[list[str]] = mapped_column(JSON, default=list)
    resume_skills: Mapped[list[str]] = mapped_column(JSON, default=list)
    works_in: Mapped[list[str] | None] = mapped_column(NullableJSON)   # industries the user works in (insiders, 17.7)
    answered_at: Mapped[datetime] = mapped_column(UTCDateTime)


class EventRow(FeedBase):
    """Append-only. `weight` is the value the event counted for, under `config_version`."""

    __tablename__ = "events"
    __table_args__ = (UniqueConstraint("user_id", "client_event_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    client_event_id: Mapped[str] = mapped_column(String(64))
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    item_type: Mapped[str] = mapped_column(String(16))    # post | problem
    item_id: Mapped[str] = mapped_column(String(64), index=True)
    event_type: Mapped[str] = mapped_column(String(16), index=True)
    weight: Mapped[float] = mapped_column(Float)
    weight_note: Mapped[str | None] = mapped_column(String(64))   # why a weight was discounted to 0
    dwell_ms: Mapped[int | None] = mapped_column(Integer)
    fully_visible: Mapped[bool | None] = mapped_column(Boolean)
    reason: Mapped[str | None] = mapped_column(String(32))
    position: Mapped[int | None] = mapped_column(Integer)
    impression_id: Mapped[int | None] = mapped_column(Integer)
    at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)          # client time
    received_at: Mapped[datetime] = mapped_column(UTCDateTime)
    config_version: Mapped[int] = mapped_column(Integer)


class ImpressionRow(FeedBase):
    __tablename__ = "impressions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    item_type: Mapped[str] = mapped_column(String(16))
    item_id: Mapped[str] = mapped_column(String(64), index=True)
    position: Mapped[int] = mapped_column(Integer)
    candidate_source: Mapped[str | None] = mapped_column(String(32))
    explanation: Mapped[str | None] = mapped_column(Text)
    score: Mapped[float | None] = mapped_column(Float)
    score_breakdown: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    shown_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    config_version: Mapped[int] = mapped_column(Integer)


class _AffinityMixin:
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("niches.id"), primary_key=True)
    alpha: Mapped[float] = mapped_column(Float)
    beta: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class UserNicheAffinityRow(_AffinityMixin, FeedBase):
    __tablename__ = "user_niche_affinity"


class UserSkillAffinityRow(_AffinityMixin, FeedBase):
    __tablename__ = "user_skill_affinity"


class UserPinRow(FeedBase):
    __tablename__ = "user_pins"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("niches.id"), primary_key=True)
    affinity: Mapped[float] = mapped_column(Float)      # implied by where the user placed it
    angle: Mapped[float | None] = mapped_column(Float)
    pinned_at: Mapped[datetime] = mapped_column(UTCDateTime)


class UserDropInRow(FeedBase):
    __tablename__ = "user_drop_ins"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("niches.id"), primary_key=True)
    exposures_left: Mapped[int] = mapped_column(Integer)
    added_at: Mapped[datetime] = mapped_column(UTCDateTime)


class UserRemovedRow(FeedBase):
    __tablename__ = "user_removed"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("niches.id"), primary_key=True)
    retest_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    removed_at: Mapped[datetime] = mapped_column(UTCDateTime)


class UserMapNodeRow(FeedBase):
    """Which niches are on a user's map (section 6.5-6.6). Affinities exist for many more nodes (events
    propagate upward); membership is explicit: quiz choices, pins, drop-ins, promoted children and
    niches discovered through exploration."""

    __tablename__ = "user_map_nodes"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("niches.id"), primary_key=True)
    # active | context (a parent whose child was promoted) | fading | off (left the map, kept for history)
    status: Mapped[str] = mapped_column(String(16), default="active")
    source: Mapped[str] = mapped_column(String(16))     # quiz | pin | drop_in | promoted | discovered
    angle: Mapped[float] = mapped_column(Float)          # degrees, fixed when the node joins
    added_at: Mapped[datetime] = mapped_column(UTCDateTime)
    status_since: Mapped[datetime] = mapped_column(UTCDateTime)


class UserMapSnapshotRow(FeedBase):
    __tablename__ = "user_map_snapshots"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    week: Mapped[date] = mapped_column(Date, primary_key=True)   # Monday of the ISO week
    data: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class MapChangeLogRow(FeedBase):
    __tablename__ = "map_change_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    node_id: Mapped[int | None] = mapped_column(ForeignKey("niches.id"))
    kind: Mapped[str] = mapped_column(String(16))      # promoted | split | fading | removed | pinned ...
    message: Mapped[str] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)


class ProfilePendingUpdateRow(FeedBase):
    __tablename__ = "profile_pending_updates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("niches.id"))
    action: Mapped[str] = mapped_column(String(8))     # add | remove
    status: Mapped[str] = mapped_column(String(16), default="pending")   # pending | approved | rejected
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class UserProfileNicheRow(FeedBase):
    """Niches published on the user's profile (section 6.6 / 14): only ever after approval, or with
    auto-publish on."""

    __tablename__ = "user_profile_niches"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("niches.id"), primary_key=True)
    published_at: Mapped[datetime] = mapped_column(UTCDateTime)


class SurveyRow(FeedBase):
    """"Was this useful?" answers (section 8.4)."""

    __tablename__ = "surveys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    item_type: Mapped[str] = mapped_column(String(16))
    item_id: Mapped[str] = mapped_column(String(64), index=True)
    impression_id: Mapped[int | None] = mapped_column(ForeignKey("impressions.id"))
    useful: Mapped[bool] = mapped_column(Boolean)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), unique=True)
    answered_at: Mapped[datetime] = mapped_column(UTCDateTime)


# --- Section 17: inferred problems -----------------------------------------------

class InferredProblemRow(FeedBase):
    """A problem nobody stated, built from real linked evidence (section 17.2). Never published without
    a critic pass unless `inferred.publish_without_critic` is set."""

    __tablename__ = "inferred_problems"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    detector: Mapped[str] = mapped_column(String(32), index=True)          # paid_workarounds | rule_change | ...
    hypothesis: Mapped[str] = mapped_column(Text)                          # filled from the detector's template
    who: Mapped[str | None] = mapped_column(String(200))
    task: Mapped[str | None] = mapped_column(Text)
    niche_id: Mapped[int | None] = mapped_column(ForeignKey("niches.id"), index=True)
    counter_evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    confidence: Mapped[str | None] = mapped_column(String(8))               # high | medium | low
    critic_notes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    validation: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # insider votes, field notes (17.7)
    status: Mapped[str] = mapped_column(String(16), index=True)             # candidate | published | rejected | expired
    derived: Mapped[bool] = mapped_column(Boolean, default=False)           # built from pain statements, not v9 tasks
    score: Mapped[float | None] = mapped_column(Float)
    score_breakdown: Mapped[dict[str, float]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    refreshed_at: Mapped[datetime] = mapped_column(UTCDateTime)


class InferredEvidenceRow(FeedBase):
    """One link in an inferred problem's evidence chain: a real post or record and what it shows."""

    __tablename__ = "inferred_evidence"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    inferred_id: Mapped[int] = mapped_column(ForeignKey("inferred_problems.id"), index=True)
    post_id: Mapped[str | None] = mapped_column(String(64), index=True)
    record_id: Mapped[str | None] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(64))
    source_type: Mapped[str] = mapped_column(String(16))
    url: Mapped[str] = mapped_column(Text)
    what_it_shows: Mapped[str] = mapped_column(Text)
    supported: Mapped[bool | None] = mapped_column(Boolean)    # the critic's check; None until it runs


class TaskClusterRow(FeedBase):
    """Paid work grouped by task within a niche (Detector 1, step 2); kept for inspection and stats."""

    __tablename__ = "task_clusters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    niche_id: Mapped[int | None] = mapped_column(ForeignKey("niches.id"), index=True)
    task: Mapped[str] = mapped_column(Text)
    post_ids: Mapped[list[str]] = mapped_column(JSON)
    buyers: Mapped[int] = mapped_column(Integer)
    qualifies: Mapped[bool] = mapped_column(Boolean)
    computed_at: Mapped[datetime] = mapped_column(UTCDateTime)


class DetectorStatsRow(FeedBase):
    """Per detector run (section 17.10): how many candidates, how the critic judged them."""

    __tablename__ = "detector_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    detector: Mapped[str] = mapped_column(String(32), index=True)
    run_at: Mapped[datetime] = mapped_column(UTCDateTime)
    clusters: Mapped[int] = mapped_column(Integer, default=0)
    candidates: Mapped[int] = mapped_column(Integer, default=0)
    published: Mapped[int] = mapped_column(Integer, default=0)
    rejected: Mapped[int] = mapped_column(Integer, default=0)
    expired: Mapped[int] = mapped_column(Integer, default=0)
    critic_runs: Mapped[int] = mapped_column(Integer, default=0)
    with_counter_evidence: Mapped[int] = mapped_column(Integer, default=0)


class InsiderVoteRow(FeedBase):
    """"Is this real where you work?" (section 17.7), or "Not true in my experience" (17.8)."""

    __tablename__ = "insider_votes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    inferred_id: Mapped[int] = mapped_column(ForeignKey("inferred_problems.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    vote: Mapped[str] = mapped_column(String(8))            # yes | no | partly
    note: Mapped[str | None] = mapped_column(Text)          # one line, personal data scrubbed
    insider: Mapped[bool] = mapped_column(Boolean)          # the user works in this niche's industry
    source: Mapped[str] = mapped_column(String(16))         # prompt | not_for_me
    at: Mapped[datetime] = mapped_column(UTCDateTime)


class InsiderPromptRow(FeedBase):
    """When an insider was asked about an inferred problem (one prompt per week, 17.7)."""

    __tablename__ = "insider_prompts"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    inferred_id: Mapped[int] = mapped_column(ForeignKey("inferred_problems.id"), primary_key=True)
    prompted_at: Mapped[datetime] = mapped_column(UTCDateTime)


class DetectorWeightRow(FeedBase):
    """17.7 learning: per detector and evidence-source combination, how inferred problems hold up."""

    __tablename__ = "detector_weights"

    detector: Mapped[str] = mapped_column(String(32), primary_key=True)
    combo: Mapped[str] = mapped_column(String(128), primary_key=True)    # sorted evidence source types, "+"-joined
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    published: Mapped[int] = mapped_column(Integer, default=0)
    confirmed: Mapped[int] = mapped_column(Integer, default=0)
    rejected: Mapped[int] = mapped_column(Integer, default=0)
    acted: Mapped[int] = mapped_column(Integer, default=0)               # problems with a save, idea or outreach
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class ExperimentAssignmentRow(FeedBase):
    """Sticky A/B arms (section 13): a user keeps their arm even if the shares are edited later."""

    __tablename__ = "experiment_assignments"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    experiment: Mapped[str] = mapped_column(String(64), primary_key=True)
    arm: Mapped[str] = mapped_column(String(64))
    assigned_at: Mapped[datetime] = mapped_column(UTCDateTime)


class ModelRow(FeedBase):
    """Trained models (section 8.2, 8.4). `value` models keep their coefficients here; `v2` ranking models
    keep a file path. A model starts as `candidate`; only one passing the offline comparison (Phase 7)
    becomes `active`."""

    __tablename__ = "models"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)        # value | v2
    status: Mapped[str] = mapped_column(String(16), index=True)      # candidate | active | retired
    trained_at: Mapped[datetime] = mapped_column(UTCDateTime)
    examples: Mapped[int] = mapped_column(Integer)
    features: Mapped[list[str]] = mapped_column(JSON)
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


# --- store ---------------------------------------------------------------------

def _sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


def _add_missing_columns(engine: Engine) -> None:
    """create_all makes new tables but never alters old ones: add columns introduced since (all nullable
    or defaulted). A real migration tool can replace this once the feed moves to Postgres."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    with engine.begin() as conn:
        for table in FeedBase.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                ddl = col.type.compile(dialect=engine.dialect)
                default = col.default.arg if col.default is not None and not callable(col.default.arg) else None
                if isinstance(default, (dict, list)):
                    default = __import__("json").dumps(default)
                clause = f" DEFAULT '{default}'" if isinstance(default, str) else (
                    f" DEFAULT {int(default) if isinstance(default, bool) else default}" if default is not None else "")
                conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {ddl}{clause}'))


class FeedStore:
    def __init__(self, url: str) -> None:
        if url.startswith("sqlite:///") and not url.endswith(":memory:"):
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        self.engine: Engine = create_engine(url)
        if self.engine.dialect.name == "sqlite":
            event.listen(self.engine, "connect", _sqlite_pragmas)
        FeedBase.metadata.create_all(self.engine)
        _add_missing_columns(self.engine)

    def session(self) -> Session:
        return Session(self.engine, expire_on_commit=False)

    # --- users -----------------------------------------------------------------

    def create_user(self, user_id: str | None = None) -> str:
        user_id = user_id or uuid.uuid4().hex
        with self.session() as s, s.begin():
            s.add(UserRow(id=user_id, created_at=utcnow()))
        return user_id

    def user_exists(self, user_id: str) -> bool:
        with self.session() as s:
            return s.get(UserRow, user_id) is not None

    def existing_inferred_ids(self, ids: Collection[int]) -> set[int]:
        if not ids:
            return set()
        with self.session() as s:
            return set(s.scalars(select(InferredProblemRow.id).where(InferredProblemRow.id.in_(set(ids)))))

    def existing_problem_ids(self, ids: Collection[int]) -> set[int]:
        if not ids:
            return set()
        with self.session() as s:
            return set(s.scalars(select(ProblemRow.id).where(ProblemRow.id.in_(set(ids)))))

    def owned_impression_ids(self, user_id: str, ids: Collection[int]) -> set[int]:
        if not ids:
            return set()
        stmt = select(ImpressionRow.id).where(ImpressionRow.user_id == user_id, ImpressionRow.id.in_(set(ids)))
        with self.session() as s:
            return set(s.scalars(stmt))

    # --- events ----------------------------------------------------------------

    def seen_client_event_ids(self, user_id: str, client_ids: Collection[str]) -> set[str]:
        if not client_ids:
            return set()
        stmt = select(EventRow.client_event_id).where(
            EventRow.user_id == user_id, EventRow.client_event_id.in_(set(client_ids)))
        with self.session() as s:
            return set(s.scalars(stmt))

    def add_events(self, rows: Sequence[EventRow]) -> None:
        """Insert events in one transaction; "Was this useful?" answers also go to surveys."""
        if not rows:
            return
        with self.session() as s, s.begin():
            s.add_all(rows)
            s.flush()
            for row in rows:
                if row.event_type in {"useful_yes", "useful_no"}:
                    s.add(SurveyRow(user_id=row.user_id, item_type=row.item_type, item_id=row.item_id,
                                    impression_id=row.impression_id, useful=row.event_type == "useful_yes",
                                    event_id=row.id, answered_at=row.at))

    def count_events(self, user_id: str | None = None) -> int:
        stmt = select(func.count()).select_from(EventRow)
        if user_id:
            stmt = stmt.where(EventRow.user_id == user_id)
        with self.session() as s:
            return s.scalar(stmt) or 0

    def events_for(self, user_id: str) -> list[EventRow]:
        with self.session() as s:
            return list(s.scalars(select(EventRow).where(EventRow.user_id == user_id).order_by(EventRow.id)))
