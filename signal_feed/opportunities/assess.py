"""A problem's assessment (founder, 2026-11-08): one Claude call per job cluster that rates the problem on
the dimensions the problem page shows, says who could buy a fix and where to find them, and how to validate
it. The problem's score is then computed from those same ratings (`composite`), so the score and its tiles
can't disagree: before this, the score was the best post's project score (how buildable the idea is) while
the tiles were averages of per-post tags, and a problem could show 85 over tiles of 2/10.

The ratings come from the evidence (Signal's own summaries and tags of the posts, never Reddit or web search
results) plus the model's general knowledge of the market, which each reason must say it is using. Example
companies are the model's knowledge too, shown to the builder as names to verify.

    python scripts/assess_problems.py --dry-run      # what would be assessed and the worst-case cost
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import select

from signal_connectors.models import utcnow

from ..config import FeedConfig, ModelCall
from ..db import FeedStore, ProblemAssessmentRow
from ..enrichment.costs import BudgetExceededError, CostLedger, claude_usd, claude_worst_case_usd
from ..enrichment.llm import Tagger, TransientError
from ..enrichment.pii import scrub

log = logging.getLogger(__name__)

VERSION = "a1"
PURPOSE = "problem_assessment"
CALL = ModelCall(model="claude-sonnet-5-5", effort="medium", max_tokens=6000)

# The score is these ratings, weighted. Shown on the problem page next to the tiles.
WEIGHTS = {"pain": 0.20, "frequency": 0.15, "will_pay": 0.25, "market": 0.15, "competition": 0.10, "evidence": 0.15}
COMPETITION_POINTS = {"low": 8, "medium": 5, "high": 2}

METHODS = Literal["interviews", "ai_panel", "presell", "smoke_test", "survey", "concierge", "competitor_reviews",
                  "spend_check"]


class Rating(BaseModel):
    score: int = Field(description="1-10, on this dimension's scale in the instructions")
    reason: str = Field(description="One short line a builder can check: the evidence it rests on, or "
                                    "'general knowledge:' when it comes from what you know of the market")


class ExistingTool(BaseModel):
    name: str
    gap: str = Field(description="Why it doesn't solve this need for these buyers, in a few words")


class Prospect(BaseModel):
    title: str = Field(description="Job title of a person who has the problem or decides to buy a fix")
    org_type: str = Field(description="The kind of organization they work at, specific (size, sector)")
    why: str = Field(description="Why they are worth reaching first")
    where: list[str] = Field(description="Concrete places to find them: named associations, directories, "
                                         "communities, events, job titles to search on LinkedIn")


class Company(BaseModel):
    name: str = Field(description="A real US organization you are confident exists, of the buyer type")
    why: str


class Method(BaseModel):
    method: METHODS
    plan: str = Field(description="What exactly to do for this problem, in one or two sentences")


class ProblemAssessment(BaseModel):
    pain: Rating
    frequency: Rating
    frequency_label: Literal["daily", "weekly", "monthly", "quarterly", "rarely"]
    will_pay: Rating
    market: Rating
    competition: Literal["low", "medium", "high"]
    competition_reason: str
    existing_tools: list[ExistingTool] = Field(description="Up to 5 products people could use today")
    who_to_reach: list[Prospect] = Field(description="2 to 4 kinds of people to contact first")
    example_companies: list[Company] = Field(description="Up to 6; leave empty rather than guess")
    validation: list[Method] = Field(description="The 3 to 5 best ways to validate this problem, best first")
    confidence: float = Field(description="0-1: how well the evidence supports these ratings")


SYSTEM = """You assess a problem for a startup builder (18-25, building software or a device to sell to many
US customers). You get the problem as Signal clustered it and summaries of the posts that are evidence for it.

Rate each dimension 1-10 on these scales. Use the whole scale; most problems are not 8+ on everything.
- pain: 1 mild annoyance; 4 costs a few hours a month; 6 costs hours every week or real money now and then;
  8 costs a person's job-share of time, lost revenue, or denied payments; 10 blocks revenue or creates legal
  or safety risk every day.
- frequency: 1 once a year or less; 4 monthly; 6 weekly; 8 daily; 10 many times a day. frequency_label must
  match the rating.
- will_pay: 1 people expect it free (consumers, hobbyists, open-source volunteers); 3 individuals might pay
  under $10/month; 5 businesses already pay for workarounds or tools under $50/month; 7 there is a budget,
  they pay staff, contractors or tools $100-500/month for this; 9-10 an existing budget line over $500/month,
  or a mandated cost.
- market: how many US organizations or people have this need and could buy. 1 under 1,000; 3 about 10,000;
  5 about 50,000; 7 about 300,000; 9 several million; 10 most businesses or consumers.
competition: low when no product fits these buyers; medium when products exist but leave a clear gap; high
when good products already serve them.

Every reason is one short line. Base it on the evidence when you can ("3 posts: re-keying takes an hour a
day"); when you use your own knowledge, start with "general knowledge:". Never invent a number the
evidence doesn't give.

who_to_reach: the people a builder should contact first to validate and sell, not the post authors: who
feels the pain and who signs the check, at specific kinds of organizations, with concrete places to find
them (named associations, directories, conferences, communities, LinkedIn titles).
example_companies: only real US organizations you are confident exist and are of the buyer type; if you
aren't sure, return none. Prefer small and mid-size ones over giants when the buyers are small.
validation: the best ways to test this problem before building, specific to it. Methods: interviews (talk
to buyers), ai_panel (simulated buyers react to the idea, to sharpen questions), presell (letters of intent
or paid pilots), smoke_test (a landing page with a price and a sign-up), survey (in a named community),
concierge (do the job by hand for a few customers), competitor_reviews (read reviews of the existing tools
for unmet needs), spend_check (find public spending, job posts or contracts that show money going to it)."""


def members_hash(problem: dict) -> str:
    ids = sorted(e["post_id"] for e in problem["evidence"])
    return hashlib.sha256(("|".join(ids)).encode()).hexdigest()[:16]


def user_prompt(problem: dict) -> str:
    lines = [f"Problem: {problem['title']}", f"Product idea: {problem.get('statement') or '-'}",
             f"Buyers: {problem.get('segment') or '-'} ({problem.get('buyer') or '-'})",
             f"Industry: {problem.get('industry') or '-'} / {problem.get('niche') or '-'}",
             f"Evidence: {problem['posts']} posts from {problem['buyers']} distinct buyers", "", "Posts:"]
    for i, e in enumerate(problem["evidence"][:10], 1):
        t = e.get("tags") or {}
        extra = "; ".join(f"{k}: {v}" for k, v in t.items() if v)
        lines.append(f"{i}. [{e['group']}, {e['role']}] {e['summary']}" + (f" ({extra})" if extra else ""))
    if problem.get("price"):
        lines.append(f"Stated USD amounts: {problem['price']}")
    return "\n".join(lines)


def evidence_points(buyers: int) -> int:
    """1 buyer 4, 2 buyers 6, 3 buyers 8, 4 or more 10."""
    return max(1, min(10, 2 + 2 * buyers))


def composite(r: dict[str, int | None]) -> int | None:
    """The score: the weighted ratings, 0-100. None when a rating is missing."""
    if any(r.get(k) is None for k in WEIGHTS):
        return None
    return round(10 * sum(WEIGHTS[k] * r[k] for k in WEIGHTS))


def load(store: FeedStore, ids: Sequence[str]) -> dict[str, ProblemAssessmentRow]:
    if not ids:
        return {}
    with store.session() as s:
        return {r.problem_id: r for r in s.scalars(select(ProblemAssessmentRow).where(
            ProblemAssessmentRow.problem_id.in_(list(ids)), ProblemAssessmentRow.version == VERSION))}


def todo(store: FeedStore, problems: Sequence[dict]) -> list[dict]:
    """Problems with no assessment of this version, or whose posts changed since."""
    have = load(store, [p["id"] for p in problems])
    return [p for p in problems if p["id"] not in have or have[p["id"]].members_hash != members_hash(p)]


def worst_case_usd(problems: Sequence[dict], config: FeedConfig) -> float:
    return sum(claude_worst_case_usd(CALL, len(SYSTEM) + len(user_prompt(p)), config.enrichment) for p in problems)


def assess(store: FeedStore, config: FeedConfig, tagger: Tagger, problems: Sequence[dict], threads: int = 4,
           clock: Callable[[], datetime] = utcnow) -> tuple[int, float]:
    """Assess each problem; returns (assessed, USD). Stops asking once the budget or credit runs out."""
    ledger, ecfg = CostLedger(store), config.enrichment
    stopped: list[str] = []

    def one(p: dict) -> float:
        if stopped:
            return 0.0
        user = user_prompt(p)
        try:
            ledger.check("anthropic", claude_worst_case_usd(CALL, len(SYSTEM) + len(user), ecfg),
                         ecfg.monthly_budget_usd, clock())
            attempt = tagger.tag(CALL, SYSTEM, user, output_model=ProblemAssessment)
        except BudgetExceededError as exc:
            stopped.append(str(exc))
            return 0.0
        except TransientError as exc:
            log.warning("assessment of %s: %s", p["id"], exc)
            return 0.0
        usd = 0.0
        for usage in attempt.usages:
            cost = claude_usd(usage, ecfg)
            usd += cost
            ledger.record(provider="anthropic", purpose=PURPOSE, usage=usage, usd=cost, post_id=p["id"],
                          request_id=attempt.request_id, at=clock())
        if attempt.output is None:
            log.warning("assessment of %s failed: %s", p["id"], attempt.error)
            return usd
        data = attempt.output.model_dump()
        for k in ("pain", "frequency", "will_pay", "market"):
            data[k]["score"] = max(1, min(10, int(data[k]["score"])))
            data[k]["reason"] = scrub(data[k]["reason"])
        with store.session() as s, s.begin():
            s.merge(ProblemAssessmentRow(problem_id=p["id"], members_hash=members_hash(p), version=VERSION,
                                         data=data, model=attempt.model, cost_usd=usd, created_at=clock()))
        return usd

    with ThreadPoolExecutor(threads) as pool:
        costs = list(pool.map(one, problems))
    if stopped:
        log.warning("stopped: %s", stopped[0])
    return sum(1 for c in costs if c > 0), sum(costs)


# --- The AI buyer panel (Validate tab): simulated buyers react to the idea --------------------------

PANEL_CALL = ModelCall(model="claude-sonnet-5-5", effort="low", max_tokens=5000)


class PanelBuyer(BaseModel):
    persona: str = Field(description="Who they are: title, organization type and size, one telling detail")
    today: str = Field(description="How they handle the problem today")
    verdict: Literal["would_buy", "maybe", "would_not_buy"]
    price_reaction: str = Field(description="Their reaction to the price, or what they'd expect to pay")
    objection: str = Field(description="Their strongest objection or condition")
    ask_real_buyers: str = Field(description="One question a builder should ask real buyers because of this")


class Panel(BaseModel):
    buyers: list[PanelBuyer] = Field(description="Exactly 5, deliberately different from each other")
    summary: str = Field(description="Two sentences: where they agreed, and the biggest open question")


PANEL_SYSTEM = """You simulate a panel of 5 different potential US buyers reacting to a startup idea, so a
builder can sharpen what to ask real buyers. Make the five genuinely different (size, role, budget,
tech comfort, how much the problem hurts them), including at least one skeptic. Be realistic about money
and switching costs; don't be agreeable. Ground them in the evidence given. This is a simulation, not
evidence, and the builder knows it."""


def panel_prompt(problem: dict, pitch: str | None, price: str | None) -> str:
    return (user_prompt(problem) + f"\n\nThe idea: {pitch or problem.get('statement') or problem['title']}"
            + f"\nPrice: {price or 'not set; ask what they would expect to pay'}")
