"""An alternative's profile on the problem page (founder, 2026-11-08: "click on the alternative and read
about it"). One Claude call per problem and tool, cached: what the tool is, who it's for, what it costs when
that is publicly known, what it does well, and where it falls short for this problem's buyers. It comes from
the model's knowledge, so it says "not publicly listed" rather than guess a price, and its website is shown
only when Signal confirms it (outreach.verify_site).
"""

from __future__ import annotations

import httpx
from pydantic import BaseModel, Field

from .config import ModelCall
from .outreach import USER_AGENT, verify_site

CALL = ModelCall(model="claude-sonnet-5-5", effort="low", max_tokens=3000)


class ToolProfile(BaseModel):
    what_it_is: str = Field(description="One or two plain sentences: what the product does")
    made_by: str = Field(description="The company behind it, or 'open source' / 'unknown'")
    who_its_for: str = Field(description="The customers it is built for, specifically")
    pricing: str = Field(description="Public pricing if you know it ('from $X/month'), else 'not publicly listed'")
    strengths: list[str] = Field(description="2-4 things it does well, short")
    falls_short: list[str] = Field(description="2-4 ways it falls short for THIS problem's buyers, short and specific")
    opening: str = Field(description="One sentence: where a new product could beat it for these buyers")
    website: str = Field(description="Its official website domain, or empty if not sure")
    confidence: float = Field(description="0-1: how sure you are about this tool")


SYSTEM = """You explain an existing product to a startup builder who is looking at a problem and wants to know
what people use today. Be factual and plain. Use only what you know about the product; if you are not sure
of something (price, features), say so instead of guessing. If you don't recognise the product, say that in
what_it_is and keep the rest short. Judge where it falls short against the problem and buyers given, not in
general."""


def tool_prompt(problem: dict, name: str) -> str:
    gap = next((t["gap"] for t in problem.get("existing_tools", []) if t["name"] == name), "")
    return (f"Product: {name}\nProblem: {problem['title']}\nBuyers: {problem.get('segment') or '-'}\n"
            f"Industry: {problem.get('industry') or '-'}\nWhat we noted so far: {gap or '-'}")


def verify_tool_site(name: str, website: str) -> str | None:
    if not website:
        return None
    with httpx.Client(timeout=8, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
        return verify_site(name, website, client)
