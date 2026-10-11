"""On-demand industries: when a user names an industry, reuse a map or build one right away.

    result = await ensure_industry(services, IndustryRequest("Dog grooming", "I run a mobile grooming van"))

1. Plan. One Claude call (metered as "on_demand") either matches the request to an existing map or
   writes seeds for a new one: roles, search keywords, incumbent tools, tasks, pains, jargon,
   subreddits. Without ANTHROPIC_API_KEY, a word-overlap match and the request's own fields are used.
2. Discover. The Phase 6 agent (discovery_agent.py) finds communities, publications and
   associations; search results stay pointers, every site is read from its own pages, robots first.
3. Check feeds automatically (assess_feed): recent, enough items, not a page farm, no spam,
   headlines that match the industry's vocabulary. Passing feeds are enabled; the rest are held
   (status unverified, locked, with the reason) for a human.
4. Forums are never enabled automatically (rule 2): their terms must be read by a person. They wait
   in the review queue (scripts/review_queue.py) with terms: unchecked, unless a platform's or an
   earlier review's terms already decided them.
5. Write industries/<key>.yaml (discovery.on_demand: true) and run a first fill of its feeds over the
   last `on_demand_first_fill_days`, recorded as "rss:<key>" so the regular schedule is untouched.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import feedparser

from .config import load_sources
from .discovery_agent import Seeds, discover
from .industries import INDUSTRIES_DIR, Feed, Industry, load_industries, save_industry
from .parsing import parse_date
from .runner import run_source
from .services import Services

log = logging.getLogger(__name__)

MAX_AGE_DAYS = 120         # newest item older than this: the feed is asleep
MIN_ITEMS = 3
MAX_ITEMS = 500            # SEO page farms publish thousands of generated pages in one feed
MIN_RELEVANT_SHARE = 0.2   # share of headlines that must mention the industry's vocabulary
SPAM = re.compile(r"\b(casino|porn|torrent|viagra|escort|betting|xxx|3gp|crack(ed)? download)\b", re.I)
_GENERIC = set("""business businesses owner owners manager managers management service services software tool tools
company companies professional professionals industry small team staff work worker workers client clients customer
customers system systems online digital best guide tips with from your their about that this have more help""".split())
FIRST_FILL_SOURCES = ("rss", "classic_forum")
# Verdicts that need no human: the feed is simply asleep, empty or unreachable.
_DEAD = re.compile(r"^(no new items|only \d+ items|HTTP \d+|fetch failed|robots\.txt)")
LOCK_DIR = Path(__file__).resolve().parent.parent / "data"


@dataclass
class IndustryRequest:
    name: str                                   # what the user called their industry
    description: str = ""                       # what they do, in their words
    roles: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)


@dataclass
class Plan:
    match: str | None
    key: str
    name: str
    roles: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    tasks: list[str] = field(default_factory=list)
    pain_phrases: list[str] = field(default_factory=list)
    jargon: list[str] = field(default_factory=list)
    subreddits: list[str] = field(default_factory=list)


@dataclass
class OnDemandResult:
    key: str
    action: str                                 # "matched" | "exists" | "created"
    name: str = ""
    feeds_enabled: list[str] = field(default_factory=list)
    feeds_held: dict[str, str] = field(default_factory=dict)       # feed url -> reason; a human decides
    feeds_skipped: dict[str, str] = field(default_factory=dict)    # asleep or unreachable; no review needed
    forums_queued: list[str] = field(default_factory=list)          # waiting for a terms review
    first_fill: dict[str, Any] = field(default_factory=dict)        # source -> {status, new}
    cost_usd: float = 0.0

    def to_json(self) -> str:
        return json.dumps(self.__dict__, indent=2)


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:40] or "industry"


def _words(text: str) -> set[str]:
    out = set()
    for w in re.findall(r"[a-z][a-z0-9'-]+", text.lower()):
        w = w.strip("'-").removesuffix("'s")
        if len(w) >= 4 and w not in _GENERIC:
            out.add(w[:-1] if w.endswith("s") and len(w) > 4 else w)
    return out


def vocabulary_terms(industry: Industry) -> set[str]:
    v = industry.vocabulary
    parts = [industry.name, *v.roles, *v.tasks, *v.pain_phrases, *v.jargon, *v.tools]
    return set().union(*(_words(p) for p in parts)) if parts else set()


# ---- 1. plan --------------------------------------------------------------------------------------

def lexical_match(request: IndustryRequest, industries: dict[str, Industry], threshold: float = 0.34) -> str | None:
    """Fallback matcher: share of the request's name words found in a map's name, key and roles."""
    want = _words(request.name)
    if not want:
        return None
    best, score = None, 0.0
    for key, ind in industries.items():
        have = _words(" ".join([key.replace("_", " "), ind.name, *ind.vocabulary.roles]))
        s = len(want & have) / len(want)
        if s > score:
            best, score = key, s
    return best if score >= threshold else None


def fallback_plan(request: IndustryRequest, industries: dict[str, Industry]) -> Plan:
    return Plan(match=lexical_match(request, industries), key=slug(request.name), name=request.name.strip(),
                roles=request.roles or [f"{request.name.strip().lower()} owner"],
                keywords=request.keywords or [request.name.strip().lower()], tools=request.tools)


PLAN_TOOL = {
    "name": "plan_industry",
    "description": "Match the request to an existing industry map, or describe a new industry for source discovery.",
    "input_schema": {
        "type": "object",
        "properties": {
            "match_key": {"type": ["string", "null"],
                          "description": "Key of an existing map that already serves this user well, else null."},
            "key": {"type": "string", "description": "snake_case key for a new map, e.g. dog_grooming"},
            "name": {"type": "string", "description": "Plural business-type name, e.g. 'Dog grooming businesses'"},
            "roles": {"type": "array", "items": {"type": "string"}, "description": "5-8 self-descriptions people in it use"},
            "keywords": {"type": "array", "items": {"type": "string"}, "description": "3-6 search phrases for its trade press and communities"},
            "tools": {"type": "array", "items": {"type": "string"}, "description": "4-10 incumbent software products"},
            "tasks": {"type": "array", "items": {"type": "string"}, "description": "6-12 recurring operational tasks"},
            "pain_phrases": {"type": "array", "items": {"type": "string"}, "description": "6-10 complaints in their words"},
            "jargon": {"type": "array", "items": {"type": "string"}, "description": "8-14 trade terms and acronyms"},
            "subreddits": {"type": "array", "items": {"type": "string"}, "description": "2-5 subreddit names without r/"},
        },
        "required": ["match_key", "key", "name", "roles", "keywords", "tools", "tasks", "pain_phrases", "jargon"],
    },
}


async def claude_plan(services: Services, request: IndustryRequest, industries: dict[str, Industry]) -> Plan:
    from anthropic import AsyncAnthropic

    d = services.sources.defaults
    catalog = "\n".join(f"- {k}: {i.name} (roles: {', '.join(i.vocabulary.roles[:5])})" for k, i in industries.items())
    prompt = (f"A builder using Signal named their industry. Existing industry maps:\n{catalog}\n\n"
              f"Request name: {request.name}\nDescription: {request.description or '(none)'}\n"
              f"Roles they gave: {', '.join(request.roles) or '(none)'}\n\n"
              "If an existing map covers the same kind of business (same buyers, workflows and communities), "
              "return its key as match_key. A close but different trade (a dog groomer is not a veterinary clinic) "
              "gets a new map. Always fill the other fields for the request itself.")
    prices = d.on_demand_prices_per_mtok
    estimate = (3000 * prices["input"] + 1500 * prices["output"]) / 1e6
    with services.costs.metered("on_demand", usd=estimate, note=f"plan {request.name}") as charge:
        client = AsyncAnthropic()
        message = await client.messages.create(
            model=d.on_demand_model, max_tokens=1500, tools=[PLAN_TOOL],
            tool_choice={"type": "tool", "name": "plan_industry"},
            messages=[{"role": "user", "content": prompt}])
        charge.usd = (message.usage.input_tokens * prices["input"] + message.usage.output_tokens * prices["output"]) / 1e6
    args = next(b.input for b in message.content if getattr(b, "type", "") == "tool_use")
    match = args.get("match_key")
    return Plan(match=match if match in industries else None, key=slug(args.get("key") or request.name),
                name=args.get("name") or request.name,
                roles=args.get("roles") or request.roles, keywords=args.get("keywords") or request.keywords,
                tools=args.get("tools") or request.tools, tasks=args.get("tasks", []),
                pain_phrases=args.get("pain_phrases", []), jargon=args.get("jargon", []),
                subreddits=[s.removeprefix("r/") for s in args.get("subreddits", [])])


# ---- 3. automatic feed checks -------------------------------------------------------------------

def judge_feed(parsed: Any, terms: set[str], today: date) -> str | None:
    """None if the feed may be enabled without a human, else the reason to hold it."""
    entries = parsed.entries
    if len(entries) < MIN_ITEMS:
        return f"only {len(entries)} items"
    if len(entries) > MAX_ITEMS:
        return f"{len(entries)} items in one feed: looks like a page farm"
    titles = [e.get("title", "") for e in entries]
    if sum(bool(SPAM.search(t)) for t in titles):
        return "spam in headlines"
    dates = [d for d in (parse_date(e.get("published") or e.get("updated")) for e in entries) if d]
    if dates and max(dates).date() < today - timedelta(days=MAX_AGE_DAYS):
        return f"no new items since {max(dates).date()}"
    meta = f"{parsed.feed.get('title', '')} {parsed.feed.get('subtitle', '')}"
    if _words(meta) & terms:
        return None
    relevant = sum(bool(_words(f"{t} {e.get('summary', '')[:300]}") & terms) for t, e in zip(titles, entries))
    if relevant / len(entries) < MIN_RELEVANT_SHARE:
        return f"only {relevant} of {len(entries)} headlines match the industry"
    return None


async def assess_feed(services: Services, feed: Feed, terms: set[str], today: date) -> str | None:
    try:
        if not await services.robots.allowed(feed.url):
            return "robots.txt disallows the feed"
        response = await services.http.get(feed.url)
    except Exception as exc:
        return f"fetch failed: {type(exc).__name__}"
    if response.status_code != 200:
        return f"HTTP {response.status_code}"
    return judge_feed(feedparser.parse(response.content), terms, today)


# ---- 2, 4, 5. build the map ----------------------------------------------------------------------

Planner = Callable[[Services, IndustryRequest, dict[str, Industry]], Awaitable[Plan]]
Discoverer = Callable[..., Awaitable[Industry]]


class _KeyLock:
    """One build per industry key at a time, across processes (two users asking at once)."""

    def __init__(self, key: str, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / f"on_demand.{key}.lock"

    def __enter__(self) -> _KeyLock:
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc: object) -> None:
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)


async def ensure_industry(services: Services, request: IndustryRequest, *, collect: bool = True,
                          industries_dir: Path = INDUSTRIES_DIR, planner: Planner | None = None,
                          discoverer: Discoverer = discover, discovery_budget_usd: float = 0.15,
                          today: date | None = None) -> OnDemandResult:
    today = today or datetime.now(timezone.utc).date()
    industries = {k: v for k, v in load_industries(industries_dir).items()
                  if not (v.discovery and v.discovery.needs_review)}
    spent_before = services.costs.spent_this_month("on_demand") + services.costs.spent_this_month("discovery")
    if planner is None:
        planner = claude_plan if os.environ.get("ANTHROPIC_API_KEY") else (lambda s, r, i: _async(fallback_plan(r, i)))
    try:
        plan = await planner(services, request, industries)
    except Exception as exc:          # a planner outage must not block the user: fall back to the request itself
        log.warning("on_demand: planner failed (%s); using the request's own fields", exc)
        plan = fallback_plan(request, industries)
    if plan.match:
        return OnDemandResult(key=plan.match, action="matched", name=industries[plan.match].name)

    with _KeyLock(plan.key, LOCK_DIR):
        path = industries_dir / f"{plan.key}.yaml"
        if path.exists():               # built meanwhile, or a key collision with an existing map
            return OnDemandResult(key=plan.key, action="exists", name=industries.get(plan.key, plan).name)
        seeds = Seeds(plan.key, plan.name, plan.roles, plan.keywords, plan.tools)
        industry = await discoverer(services, seeds, budget_usd=discovery_budget_usd, today=today)
        v = industry.vocabulary
        v.roles, v.tools = plan.roles, plan.tools
        v.tasks, v.pain_phrases = plan.tasks, plan.pain_phrases
        v.jargon = plan.jargon or v.jargon
        industry.subreddits = plan.subreddits

        result = OnDemandResult(key=plan.key, action="created", name=plan.name)
        terms = vocabulary_terms(industry).union(*(_words(k) for k in plan.keywords))
        feeds = industry.live_feeds()
        reasons = await asyncio.gather(*(assess_feed(services, f, terms, today) for f in feeds))
        for feed, reason in zip(feeds, reasons):
            if reason and _DEAD.match(reason):
                feed.status, feed.locked, feed.notes = "dead", True, f"automatic check: {reason}"
                result.feeds_skipped[feed.url] = reason
            elif reason:
                feed.status, feed.locked, feed.notes = "unverified", True, f"held for review: {reason}"
                result.feeds_held[feed.url] = reason
            else:
                result.feeds_enabled.append(feed.url)
        result.forums_queued = [f.url for f in industry.forums if f.status == "live" and f.terms == "unchecked"]

        info = industry.discovery
        info.needs_review, info.on_demand, info.request = False, True, request.name
        info.notes = (f"Built on demand {today} for the request {request.name!r}. {len(result.feeds_enabled)} feeds "
                      f"passed the automatic checks and are collected; {len(result.feeds_held)} are held for review and "
                      f"{len(result.feeds_skipped)} skipped (asleep or unreachable). "
                      f"{len(result.forums_queued)} forums wait for a terms review (scripts/review_queue.py). "
                      f"Vocabulary drafted by the planner; review when convenient.")
        save_industry(industry, path)

    if collect:
        sources = load_sources(services.settings.sources_file, industries_dir=industries_dir)
        since = datetime.now(timezone.utc) - timedelta(days=services.sources.defaults.on_demand_first_fill_days)
        for name in FIRST_FILL_SOURCES:
            if name not in sources.sources or sources.sources[name].blocked_reason():
                continue
            out = await run_source(services, sources.sources[name], industry=plan.key, since=since)
            result.first_fill[name] = {"status": out.result.status, "new": out.result.new}
    spent = services.costs.spent_this_month("on_demand") + services.costs.spent_this_month("discovery")
    result.cost_usd = round(spent - spent_before, 4)
    return result


async def _async(value: Any) -> Any:
    return value


def review_queue(industries_dir: Path = INDUSTRIES_DIR) -> list[dict[str, str]]:
    """What a human still has to look at: forums with unread terms, and feeds held by the checks."""
    rows = []
    for key, ind in load_industries(industries_dir).items():
        for f in ind.forums:
            if f.status == "live" and f.terms == "unchecked":
                rows.append({"industry": key, "kind": "forum terms", "url": f.url, "detail": f.terms_url or "find terms page"})
        for f in ind.rss_feeds:
            if (f.notes or "").startswith("held for review"):
                rows.append({"industry": key, "kind": "held feed", "url": f.url, "detail": f.notes})
    return rows
