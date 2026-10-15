"""What the tagging model returns (validated with pydantic) and the skill vocabulary it draws on.

`TagOutput` is built from the industry seed maps so the top level of `industry_path` is always
one of the 15 seeded industries (or "Other"): Phase 2 hangs posts on the niche tree by that name.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, create_model

from signal_connectors.industries import load_industries

from ..config import TaggedPostType
from ..settings import REPO_ROOT

SKILLS_FILE = REPO_ROOT / "config" / "skills.yaml"
NICHES_FILE = REPO_ROOT / "config" / "niches.yaml"

PostType = Literal[TaggedPostType, "builder"]

Score = float  # 0-1; range checked by the model fields below


def _score(description: str) -> Any:
    return Field(ge=0, le=1, description=description)


class MoneySignal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["budget", "rate", "salary", "price", "willing_to_pay", "contract_value", "lost_revenue"]
    amount_min: float | None = Field(description="Lower or only amount, as a number; null if no figure is stated")
    amount_max: float | None = Field(description="Upper amount for a range, else null")
    currency: str | None = Field(description="ISO 4217 code such as USD, EUR, GBP; null if unknown")
    period: Literal["once", "hour", "day", "week", "month", "year"] | None
    quote: str = Field(description="The few words of the post that state it, without names or contact details")


class DataMoved(BaseModel):
    """Data someone moves between two tools by hand (section 17, Detector 3)."""
    model_config = ConfigDict(extra="forbid")

    from_tool: str = Field(description="Product the data comes out of, e.g. 'QuickBooks'")
    to_tool: str = Field(description="Product it goes into, e.g. 'Excel'")
    data: str = Field(description="What is moved, in 2-5 words, e.g. 'vendor invoice totals'")


class Obligation(BaseModel):
    """What a rule document makes someone do (section 17, Detector 2). Only from rules and proposed rules."""
    model_config = ConfigDict(extra="forbid")

    who: str = Field(description="Who must comply, as a role or business type, e.g. 'small trucking fleets'")
    what: str = Field(description="What they must now do, as a verb phrase, e.g. 'keep electronic driver logs'")
    deadline: str | None = Field(description="Compliance or effective date as YYYY-MM-DD if stated, else null")
    industries: list[str] = Field(max_length=3, description="Industries affected, from the industry list")


@cache
def industry_names() -> tuple[str, ...]:
    return tuple(sorted(ind.name for ind in load_industries().values()))


@cache
def niche_seeds() -> dict[str, tuple[tuple[str, str], ...]]:
    """Seeded branches per industry (config/niches.yaml): industry -> ((name, covers), ...)."""
    data = yaml.safe_load(Path(NICHES_FILE).read_text(encoding="utf-8"))["industries"]
    unknown = set(data) - set(industry_names())
    if unknown:
        raise ValueError(f"config/niches.yaml names unknown industries: {', '.join(sorted(unknown))}")
    out = {}
    for industry, branches in data.items():
        names = [b["name"] for b in branches]
        if len({n.lower() for n in names}) != len(names):
            raise ValueError(f"config/niches.yaml: duplicate branch under {industry}")
        out[industry] = tuple((b["name"], b["covers"]) for b in branches)
    return out


@cache
def skill_vocabulary() -> tuple[tuple[str, str], ...]:
    data = yaml.safe_load(Path(SKILLS_FILE).read_text(encoding="utf-8"))
    return tuple((s["name"], s["covers"]) for s in data["skills"])


@cache
def tag_output_model() -> type[BaseModel]:
    industry = Literal[(*industry_names(), "Other")]  # type: ignore[valid-type]
    return create_model(
        "TagOutput",
        __config__=ConfigDict(extra="forbid"),
        post_type=(TaggedPostType, ...),
        industry=(industry, Field(description="The one seeded industry this post is most about, or Other")),
        industry_subpath=(list[str], Field(
            max_length=2, description="0-2 narrower niches inside the industry, broad to narrow; the first is one of "
                                     "the industry's listed branches when one fits")),
        skills_needed=(list[str], Field(max_length=5, description="Skills a builder would need; prefer vocabulary names")),
        who_has_pain=(str | None, Field(description="Role of the person with the problem, e.g. 'litigation paralegal'; "
                                                    "null if no one has a problem")),
        pain_statement=(str | None, Field(description="One plain sentence describing the problem or demand; "
                                                      "null when no one has a problem or demand")),
        current_workaround=(str | None, Field(description="How they cope today, if stated or clearly implied")),
        tools_mentioned=(list[str], Field(max_length=5, description="Existing products, apps or services the post "
                                                                    "names, e.g. 'QuickBooks'; empty if none")),
        task=(str | None, Field(description="The repetitive task the post is about, as a short normalized verb "
                                            "phrase, e.g. 'reconcile vendor invoices against POs'; null if none")),
        task_automatable=(Score | None, Field(ge=0, le=1,
                                              description="How much of that task software could do: 0 = physical or "
                                                          "judgment work, 1 = routine information work; null if no task")),
        data_moved=(list[DataMoved], Field(max_length=3, description="Data the author moves between two named tools "
                                                                     "by hand; empty if none")),
        obligations=(list[Obligation], Field(max_length=3, description="Only for a rule or proposed rule: what it "
                                                                       "makes someone do; empty otherwise")),
        intensity=(Score, _score("Frustration, urgency, repetition")),
        money_signal=(MoneySignal | None, Field(description="Only money the post states; null otherwise")),
        buyer_likelihood=(Score, _score("Is the person with the problem likely the buyer or decision maker")),
        quality=(Score, _score("Specific and credible = high; vague, recycled or off-topic = low")),
        spam_score=(Score, _score("Self-promotion, ads, engagement or outrage bait, adult content")),
        toxicity=(Score, _score("Insults, harassment, hate")),
        language=(str, Field(description="ISO 639-1 code of the post's language")),
        confidence=(Score, _score("How sure you are of these tags overall")),
    )
