"""Who to reach for a saved problem, for this builder (founder, 2026-11-08): "it should be very clear who
and why to reach out to... validate everything before putting it there."

The builder tells Signal about themselves (where they live, what they do, their background, who they know).
One Claude call per request then proposes groups of named organizations chosen for this problem (not a
fixed list of categories), each with the role to contact there, why, the first concrete step, and any warm
path from the builder's own background. Every organization is then checked before it is shown: its official
website must answer and name it (robots.txt respected, one homepage request, nothing stored but the
verdict). Organizations that can't be confirmed are dropped, never shown. No individual's name is asked for
or kept (CLAUDE.md rule 4): the builder gets the role and the place, and finds the person.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Literal
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx
from pydantic import BaseModel, Field

from .config import ModelCall

log = logging.getLogger(__name__)

CALL = ModelCall(model="claude-sonnet-5-5", effort="medium", max_tokens=14000)
USER_AGENT = "SignalBot/0.1 (website check for a builder's outreach list)"
DAILY_LIMIT = 10


class BuilderContext(BaseModel):
    location: str = Field(default="", max_length=120)
    now: str = Field(default="", max_length=300, description="What they do now: student, job, ...")
    background: str = Field(default="", max_length=6000, description="Experience or a pasted resume")
    network: str = Field(default="", max_length=1000, description="People, companies and communities they know")


CHANNELS = Literal["email", "contact_form", "linkedin", "community_post", "phone", "in_person", "intro"]


class Target(BaseModel):
    name: str = Field(description="A specific, real, named organization, association, community, event or publication")
    kind: Literal["company", "association", "community", "event", "publication", "agency", "school", "other"]
    website: str = Field(description="Its official website domain, e.g. nacsonline.com")
    contact_role: str = Field(description="Exactly who to contact there (a job title or member type), never a person's name")
    why: str = Field(description="Why this one, tied to the problem: specific, one or two sentences")
    how: str = Field(description="The first concrete step, e.g. 'Use the contact form to ask the membership "
                                 "director if you can post a 3-question survey in their newsletter'")
    warm_path: str = Field(default="", description="How the builder's own background or network gets them in; "
                                                   "empty if nothing in their context helps")
    near_builder: bool = Field(description="True when it is in or near where the builder lives")
    channel: CHANNELS = Field(description="The best way to reach this contact, by who they are (rules in the instructions)")
    channel_detail: str = Field(description="Where exactly: e.g. 'the general email on their contact page', "
                                            "'#vuln-disclosure channel in the OpenSSF Slack', 'the front desk, weekday mornings'")


class Group(BaseModel):
    name: str = Field(description="A group chosen for this problem, e.g. 'Independent grocers in Chicago'")
    why: str = Field(description="One sentence: what reaching this group tells or gets the builder")
    targets: list[Target] = Field(description="4 to 8")


class OutreachPlan(BaseModel):
    groups: list[Group] = Field(description="4 to 6 groups, the most useful first")


SYSTEM = """You build an outreach list for a startup builder (18-25) who wants to validate a problem and find
first customers. You get the problem, the evidence, and what the builder told you about themselves.

Choose 4 to 6 groups, with 4 to 8 targets each that make sense for THIS problem and THIS builder, most useful first: for example
buyers who have the problem, the people who decide on purchases, associations or communities that gather
them, partners who already sell to them, places near the builder. Don't use generic categories.

Every target must be a real, specific, named organization, association, community, event or publication
that you are confident exists, with its official website domain. Signal checks each website and drops any
it can't confirm, so never guess a name or domain. Prefer smaller and local organizations when the buyers
are small, and ones near the builder when they can meet in person. Use the builder's background and
network for warm paths (a former employer, their school's alumni, a club), and say so only when their
context supports it. Never assume facts about the builder they didn't state: no school, employer, city or
title they didn't give you, in warm_path, how or anywhere else. Where their context is missing, write the
step so it works for anyone.

Pick each target's channel by who they are:
- email: universities, schools, associations, agencies, nonprofits, publications and other professional
  organizations, and companies that list a general or department email. Formal and short.
- contact_form: companies that only take messages through a form on their site.
- linkedin: a specific role at a mid-size or large company that has no public way in.
- community_post: Slack, Discord, forums and mailing lists: post a question to the group, within its rules.
- phone: small local businesses (shops, clinics, restaurants, contractors) that answer the phone.
- in_person: events, meetups and local businesses near the builder that they can walk into.
- intro: when the builder's own network gives a warm path; they ask that person for an introduction.

Never name individual people. contact_role says exactly which role to ask for. why must be specific to
the problem, not "they might be interested". how is the very first step the builder can take today."""


def prompt(problem: dict, ctx: BuilderContext, exclude: list[str] | None = None,
           groups: list[str] | None = None) -> str:
    ev = "\n".join(f"- [{e.get('group')}] {e.get('summary')}" for e in problem.get("evidence", [])[:8])
    reach = "\n".join(f"- {x['title']} at {x['org_type']}" for x in problem.get("who_to_reach", []))
    return (f"Problem: {problem['title']}\nProduct idea: {problem.get('statement') or '-'}\n"
            f"Buyers: {problem.get('segment') or '-'}\nIndustry: {problem.get('industry') or '-'}\n"
            f"Evidence:\n{ev or '-'}\nRoles that have it:\n{reach or '-'}\n\n"
            f"The builder:\nLives in: {ctx.location or 'not given'}\nDoes now: {ctx.now or 'not given'}\n"
            f"Background: {ctx.background or 'not given'}\nKnows: {ctx.network or 'not given'}"
            + (f"\n\nAlready on the list (find different organizations, and new groups where useful): "
               f"{'; '.join(exclude)}" if exclude else "")
            + (f"\nExisting groups (put an organization in one of these, with the same name, when it fits; "
               f"start a new group only for a different kind): {'; '.join(groups)}" if groups else ""))


def _domain(website: str) -> str | None:
    w = website.strip().lower()
    host = urlparse(w if "://" in w else f"https://{w}").hostname or ""
    return host if re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", host) else None


def _tokens(name: str) -> set[str]:
    stop = {"the", "and", "of", "for", "inc", "llc", "co", "company", "association", "national", "american", "group"}
    return {t for t in re.findall(r"[a-z0-9]{3,}", name.lower()) if t not in stop}


def verify_site(name: str, website: str, client: httpx.Client) -> str | None:
    """The confirmed homepage URL, or None: the site must answer, allow us in robots.txt, and name the org."""
    found = check_site(name, website, client)
    return found[0] if found else None


CONTACT_HREF = re.compile(r"""href=["']([^"'#]*(?:contact|get-in-touch|about/contact)[^"'#]*)["']""", re.I)


def check_site(name: str, website: str, client: httpx.Client) -> tuple[str, str | None] | None:
    """(homepage URL, contact page URL or None) when the site is confirmed. Only a link to the contact page
    is kept, never an email or phone number from it (CLAUDE.md rule 4)."""
    host = _domain(website)
    if host is None:
        return None
    base = f"https://{host}"
    try:
        rp = RobotFileParser()
        r = client.get(f"{base}/robots.txt")
        rp.parse(r.text.splitlines() if r.status_code < 400 else [])
        if not rp.can_fetch(USER_AGENT, base + "/"):
            return None
        page = client.get(base + "/")
    except httpx.HTTPError:
        return None
    if page.status_code >= 400:
        return None
    text = page.text[:300_000].lower()
    toks = _tokens(name)
    hits = sum(t in text for t in toks)
    if not toks or hits < max(1, min(2, len(toks))):
        return None
    contact = None
    m = CONTACT_HREF.search(page.text[:300_000])
    if m and not m.group(1).lower().startswith(("mailto:", "tel:", "javascript:")):
        u = str(page.url.join(m.group(1)))
        if urlparse(u).hostname and urlparse(u).hostname.endswith(host.removeprefix("www.")):
            contact = u
    return str(page.url), contact


def verify(plan: OutreachPlan, fetch=None) -> tuple[list[dict], int]:
    """Groups with only confirmed targets (each with its `url`), and how many were dropped."""
    targets = [(gi, t) for gi, g in enumerate(plan.groups) for t in g.targets]
    with httpx.Client(timeout=8, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
        check = fetch or (lambda t: check_site(t.name, t.website, client))
        with ThreadPoolExecutor(8) as pool:
            urls = list(pool.map(check, [t for _, t in targets]))
    groups = [{"name": g.name, "why": g.why, "targets": []} for g in plan.groups]
    for (gi, t), found in zip(targets, urls, strict=True):
        if found:
            url, contact = found if isinstance(found, tuple) else (found, None)
            groups[gi]["targets"].append(t.model_dump() | {"url": url, "contact_url": contact})
    kept = [g for g in groups if g["targets"]]
    return kept, sum(1 for u in urls if not u)
