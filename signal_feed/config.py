"""Schema and loader for config/feed.yaml: every tunable weight, threshold and quota."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, get_args

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

EventType = Literal[
    "impression", "scroll_past", "read", "expand", "open_source", "save", "share",
    "id_build", "idea", "outreach", "useful_yes", "useful_no", "not_for_me", "hide_source", "report",
    "interested",
]
EVENT_TYPES: tuple[str, ...] = get_args(EventType)

# Post types the tagger picks. The first five are what the feed is for; the rest say what a
# non-actionable post is instead of lumping it into one "noise" bucket. `builder` (posts by Signal
# users) never comes from the connectors, so the tagger can't pick it.
TaggedPostType = Literal[
    "pain", "paid_demand", "tool_gap", "change", "idea",
    "research", "listing", "company_news", "general_news", "notice",
    "opinion", "guide", "praise", "spam", "off_topic",
]
TAGGED_POST_TYPES: tuple[str, ...] = get_args(TaggedPostType)

Fraction = float


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


Effort = Literal["low", "medium", "high", "xhigh", "max"]


class ModelCall(_Strict):
    model: str
    effort: Effort
    max_tokens: int = Field(gt=0)


class EscalationCall(ModelCall):
    below_confidence: Fraction = Field(ge=0, le=1)
    server_fallbacks: bool


class Prefilter(_Strict):
    min_chars: int = Field(ge=0)
    skip_sources: list[str] = Field(default_factory=list)   # prefixes of RawPost.source


class TokenPrice(_Strict):
    input: float = Field(ge=0)
    output: float = Field(ge=0)


class EnrichmentConfig(_Strict):
    prompt_version: str = Field(min_length=1, max_length=32)
    spam_drop_threshold: Fraction = Field(ge=0, le=1)
    monthly_budget_usd: float = Field(ge=0)
    max_retries_invalid_json: int = Field(ge=0)
    max_input_chars: int = Field(gt=0)
    drop_post_types: list[TaggedPostType]  # stored for audits, kept out of ranking
    tagger: ModelCall
    escalation: EscalationCall
    batch_min_posts: int = Field(gt=0)
    prefilter: Prefilter
    prices_per_mtok: dict[str, TokenPrice]
    batch_discount: Fraction = Field(ge=0, le=1)
    cache_write_multiplier: float = Field(ge=0)
    cache_read_multiplier: float = Field(ge=0)

    @model_validator(mode="after")
    def _models_priced(self) -> EnrichmentConfig:
        for call in (self.tagger, self.escalation):
            if call.model not in self.prices_per_mtok:
                raise ValueError(f"no price for {call.model} in enrichment.prices_per_mtok")
        return self


class EmbeddingsConfig(_Strict):
    provider: Literal["voyage"]
    model: str
    dimensions: int = Field(gt=0)
    price_per_mtok: float = Field(ge=0)
    monthly_budget_usd: float = Field(ge=0)
    max_input_chars: int = Field(gt=0)
    batch_size: int = Field(gt=0, le=1000)
    requests_per_minute: int | None = Field(default=None, gt=0)  # None = no pacing
    tokens_per_minute: int | None = Field(default=None, gt=0)


class NichesConfig(_Strict):
    split_min_support_90d: int = Field(gt=0)
    child_min_posts: int = Field(gt=0)
    child_min_authors: int = Field(gt=0)
    child_min_sources: int = Field(gt=0)
    retire_after_weeks_below: int = Field(gt=0)
    support_window_days: int = Field(gt=0)
    authorless_counts_as_distinct: bool
    pca_dims: int = Field(gt=1)
    hdbscan_min_samples: int = Field(gt=0)
    merge_cosine: Fraction = Field(ge=0, le=1)
    assign_percentile: float = Field(ge=0, le=100)
    child_min_cosine: Fraction = Field(ge=0, le=1)
    child_min_cohesion: Fraction = Field(ge=0, le=1)
    seed_match_margin: Fraction = Field(ge=0, le=1)
    grow_trees: list[Literal["industry", "skill"]]
    name_samples: int = Field(gt=0)


class ProblemScoreWeights(_Strict):
    frequency: float
    trend: float
    intensity: float
    money_signal: float
    tool_gap: float
    source_diversity: float
    buyer_likelihood: float

    @model_validator(mode="after")
    def _sum_to_one(self) -> ProblemScoreWeights:
        total = sum(self.model_dump().values())
        if abs(total - 1) > 1e-6:
            raise ValueError(f"problem score weights must sum to 1, got {total:.3f}")
        return self


class DemandScoreWeights(_Strict):
    frequency: float
    money_signal: float
    trend: float
    source_diversity: float

    @model_validator(mode="after")
    def _sum_to_one(self) -> DemandScoreWeights:
        total = sum(self.model_dump().values())
        if abs(total - 1) > 1e-6:
            raise ValueError(f"demand score weights must sum to 1, got {total:.3f}")
        return self


class ProblemsConfig(_Strict):
    cluster_cosine_threshold: Fraction = Field(ge=0, le=1)
    borderline_cosine: Fraction = Field(ge=0, le=1)
    merge_checks_per_run: int = Field(ge=0)
    match_min_jaccard: Fraction = Field(ge=0, le=1)
    frequency_full_at_voices: int = Field(gt=0)
    source_types_full_at: int = Field(gt=0)
    trend_window_days: int = Field(gt=0)
    buyer_min_likelihood: Fraction = Field(ge=0, le=1)
    confidence_full_at_posts: int = Field(gt=0)
    early_signal_below: Fraction = Field(ge=0, le=1)
    score_weights: ProblemScoreWeights
    demand_types: list[TaggedPostType] = Field(min_length=1)
    demand_score_weights: DemandScoreWeights

    @model_validator(mode="after")
    def _borderline_below_threshold(self) -> ProblemsConfig:
        if self.borderline_cosine > self.cluster_cosine_threshold:
            raise ValueError("problems.borderline_cosine must not exceed cluster_cosine_threshold")
        return self


class Propagation(_Strict):
    parent: Fraction = Field(ge=0, le=1)
    grandparent: Fraction = Field(ge=0, le=1)


class Narrowing(_Strict):
    child_share_of_positive: Fraction = Field(ge=0, le=1)
    child_min_positive_events: int = Field(ge=0)
    lookback_weeks: int = Field(gt=0)
    promoted_affinity_factor: Fraction = Field(ge=0, le=1)
    fade_min_negative_events: int = Field(ge=0)
    fade_weeks_without_positive: int = Field(gt=0)


class Rings(_Strict):
    core_min: Fraction = Field(ge=0, le=1)
    regular_min: Fraction = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _ordered(self) -> Rings:
        if self.regular_min >= self.core_min:
            raise ValueError("rings.regular_min must be below rings.core_min")
        return self


class UserModelConfig(_Strict):
    prior_strength: float = Field(gt=0)
    default_affinity: Fraction = Field(ge=0, le=1)
    evidence_per_weight: float = Field(gt=0)
    inherit: list[Fraction] = Field(min_length=1, max_length=3)
    type_preference_rate: float = Field(ge=0)
    source_preference_rate: float = Field(ge=0)
    source_preference_cap: Fraction = Field(ge=0, le=1)
    similar_builders_min_jaccard: Fraction = Field(ge=0, le=1)
    too_small_step: Fraction = Field(ge=0, le=1)
    quiz_prior_affinity: Fraction = Field(ge=0, le=1)
    resume_prior_affinity: Fraction = Field(ge=0, le=1)
    calibration_cards: int = Field(ge=0)
    calibration_wildcards: int = Field(ge=0)
    similar_builders_full_until_events: int = Field(ge=0)
    similar_builders_zero_at_events: int = Field(ge=0)
    propagation: Propagation
    evidence_half_life_days: float = Field(gt=0)
    drop_in_affinity: Fraction = Field(ge=0, le=1)
    drop_in_exposure_budget: int = Field(ge=0)
    removed_retest_after_days: int = Field(ge=0)
    committed_other_cap: Fraction = Field(ge=0, le=1)
    narrowing: Narrowing
    rings: Rings


class MapConfig(_Strict):
    new_days: int = Field(ge=0)
    rising_delta: Fraction = Field(ge=0, le=1)
    child_angle_spread_deg: float = Field(gt=0, lt=180)
    nudge_weight: float = Field(gt=0)
    discover_min_positive_events: int = Field(gt=0)


class EventsConfig(_Strict):
    weights: dict[EventType, float]
    scroll_past_max_dwell_ms: int = Field(gt=0)
    read_min_share_of_expected: Fraction = Field(ge=0, le=1)
    reading_words_per_minute: int = Field(gt=0)
    reading_min_expected_ms: int = Field(ge=0)
    max_batch_size: int = Field(gt=0)
    max_clock_skew_seconds: int = Field(ge=0)
    max_age_days: int = Field(gt=0)

    @model_validator(mode="after")
    def _every_event_weighted(self) -> EventsConfig:
        missing = set(EVENT_TYPES) - set(self.weights)
        if missing:
            raise ValueError(f"events.weights is missing: {', '.join(sorted(missing))}")
        return self


class CandidateShares(_Strict):
    niche_match: Fraction = Field(ge=0, le=1)
    skill_match: Fraction = Field(ge=0, le=1)
    similar_builders: Fraction = Field(ge=0, le=1)
    trending_nearby: Fraction = Field(ge=0, le=1)
    exploration: Fraction = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _at_most_one(self) -> CandidateShares:
        # The rest goes to pins, drop-ins and followed builders "as needed".
        if sum(self.model_dump().values()) > 1 + 1e-6:
            raise ValueError("candidate shares must not sum above 1")
        return self


class CandidatesConfig(_Strict):
    per_session: int = Field(gt=0)
    shares: CandidateShares
    exclude_seen_days: int = Field(ge=0)


class QualityFactor(_Strict):
    base: float
    problem_score: float
    toxicity_penalty: Fraction = Field(ge=0, le=1)


RankAction = Literal["read", "save", "id_build", "idea", "outreach", "not_for_me", "scroll_past"]
RankFeature = Literal["intercept", "affinity", "skill", "save_sim", "nfm_sim", "type_pref", "source_pref",
                      "problem", "intensity", "money", "buyer", "quality", "fresh", "social"]


class RankerV1(_Strict):
    coefficients: dict[RankAction, dict[RankFeature, float]]
    similarity_low: Fraction = Field(ge=0, le=1)
    similarity_high: Fraction = Field(ge=0, le=1)
    similar_save_cosine: Fraction = Field(ge=0, le=1)
    fresh_half_life_hours: float = Field(gt=0)
    recent_saves: int = Field(gt=0)

    @model_validator(mode="after")
    def _every_action(self) -> RankerV1:
        missing = set(get_args(RankAction)) - set(self.coefficients)
        if missing:
            raise ValueError(f"ranking.v1.coefficients is missing: {', '.join(sorted(missing))}")
        if self.similarity_low >= self.similarity_high:
            raise ValueError("ranking.v1.similarity_low must be below similarity_high")
        return self


class RankingConfig(_Strict):
    quality_factor: QualityFactor
    survey_every_n_items: int = Field(gt=0)
    value_model_blend: Fraction = Field(ge=0, le=1)
    v2_min_labeled_impressions: int = Field(gt=0)
    v1: RankerV1


class Quotas(_Strict):
    core_regular: Fraction = Field(ge=0, le=1)
    paid_demand_min: Fraction = Field(ge=0, le=1)
    change_max: Fraction = Field(ge=0, le=1)
    exploration_min: Fraction = Field(ge=0, le=1)
    exploration_max: Fraction = Field(ge=0, le=1)
    builder_max: Fraction = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _exploration_range(self) -> Quotas:
        if self.exploration_min > self.exploration_max:
            raise ValueError("quotas.exploration_min must not exceed exploration_max")
        return self


class CompositionConfig(_Strict):
    window: int = Field(gt=0)
    quotas: Quotas
    max_same_leaf_per_10: int = Field(gt=0)
    max_same_type_in_a_row: int = Field(gt=0)
    fresh_share_first_10: Fraction = Field(ge=0, le=1)
    fresh_hours: int = Field(gt=0)
    caught_up_score_floor: float
    page_size: int = Field(gt=0, le=100)
    pins_per_page: int = Field(ge=0)


class LearningConfig(_Strict):
    bubble_leaf_share: Fraction = Field(ge=0, le=1)
    bubble_window_impressions: int = Field(gt=0)
    bubble_exploration: Fraction = Field(ge=0, le=1)


class FeedConfig(_Strict):
    version: int = Field(ge=1)
    enrichment: EnrichmentConfig
    embeddings: EmbeddingsConfig
    niches: NichesConfig
    problems: ProblemsConfig
    user_model: UserModelConfig
    map: MapConfig
    events: EventsConfig
    candidates: CandidatesConfig
    ranking: RankingConfig
    composition: CompositionConfig
    learning: LearningConfig


def load_feed_config(path: Path) -> FeedConfig:
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return FeedConfig.model_validate(data)
