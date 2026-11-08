"""Targeted search: fill a thin industry right away, from free official APIs (founder, 2026-11-08).

    counts = await targeted_search(services, [industry], days=30)

When a user's industries have too few opportunities in the bank (signal_feed/opportunities/targeted.py
decides), this asks Hacker News (Algolia), GitHub (issues and discussions), Stack Exchange and Lemmy for
that industry's own words: its pain phrases, its tasks and its roles next to "software". Posts go through
the normal store path (dedupe, privacy hashing) and are tagged and scored by the scheduled feed jobs like
any other post, so the next user with that industry finds them in the bank.

Measured 2026-11-08: almost nothing for non-tech industries, even with anchored phrases and GitHub
titles only (restaurants 2 posts, real estate 5, accounting 20, mostly unrelated). Practitioners in those
industries post on Reddit, Facebook groups and trade forums (blocked) or X (paid). So the onboarding
trigger is off by default (SIGNAL_TARGETED=on turns it on); X is the next source to try here.

Each source runs as "<source>:<industry>" (run_source's industry mode), so the source's own schedule and
cursors are untouched. Only free APIs: X is paid and stays a separate, opt-in call (x_search.py); Reddit
and web search results can't be stored or analysed (CLAUDE.md 7.2, G1).
"""

from __future__ import annotations

import logging
from datetime import timedelta

from .config import QueryConfig
from .industries import Industry
from .models import utcnow
from .runner import run_source
from .services import Services

log = logging.getLogger(__name__)

SOURCES = ("hackernews", "github", "stackexchange", "lemmy")
STACKEXCHANGE_SITES = ["softwarerecs", "webapps", "workplace", "money", "law", "pm", "freelancing"]


GENERIC = {"shops", "businesses", "practices", "centers", "studios", "clinics", "homes"}


def anchor(industry: Industry) -> str:
    """The industry's own words: "Restaurants and hospitality" -> "restaurant", "Real estate" -> "real
    estate", "Auto repair shops" -> "auto repair". Every query pairs it with the need, because a task alone
    ("inventory", "retainage") matched unrelated posts on every source (2026-11-08)."""
    words = industry.name.split(" and ")[0].split()
    while len(words) > 1 and words[-1].lower() in GENERIC:
        words.pop()
    if len(words) == 1 and len(words[0]) <= 2:      # "IT and developer tools", "HR and recruiting": the first role
        return (industry.vocabulary.roles[:1] or [industry.name])[0].lower()
    phrase = " ".join(words).lower()
    if len(words) == 1 and phrase.endswith("s") and not phrase.endswith(("ss", "ics", "us")):
        phrase = phrase[:-1]
    return phrase


def queries(industry: Industry, source: str, limit: int) -> list[QueryConfig]:
    """The industry's anchor with each of its pain phrases and tasks; its roles next to "software"."""
    v = industry.vocabulary
    key, a = industry.industry, anchor(industry)
    needs = [n for n in (*v.pain_phrases[:4], *v.tasks[:3]) if a not in n.lower()]
    roles = v.roles[:2] or [industry.name]
    if source == "hackernews":                      # each keyword is its own search; quoted terms are all required
        return [QueryConfig(keywords=[*(f'"{a}" "{n}"' for n in needs), *(f'"{r}" software' for r in roles)],
                            limit=limit, industry_hint=key)]
    if source == "github":                          # one search per query: both quoted terms required
        # In titles only: anywhere in a body, both words matched travel guides and unrelated code (2026-11-08).
        return [QueryConfig(params={"kind": kind}, keywords=[f'"{a}" "{n}" in:title'], limit=max(10, limit // 4),
                            industry_hint=key)
                for n in needs[:5] for kind in ("issues", "discussions")]
    if source == "stackexchange":                   # every site x keyword costs quota: few phrases
        return [QueryConfig(communities=STACKEXCHANGE_SITES, keywords=[f"{a} {n}" for n in v.tasks[:2]] or [a],
                            limit=limit, industry_hint=key)]
    if source == "lemmy":                           # Crawl-delay 60: each phrase is a minute
        return [QueryConfig(keywords=[f"{a} {n}" for n in needs[:2]], limit=limit, industry_hint=key)]
    raise ValueError(f"no targeted queries for {source}")


async def targeted_search(services: Services, industries: list[Industry], *, days: int = 30, limit: int = 40,
                          sources: tuple[str, ...] = SOURCES) -> dict[str, int]:
    """New posts stored per "<source>:<industry>". A failing source is logged and skipped."""
    since = utcnow() - timedelta(days=days)
    out: dict[str, int] = {}
    for industry in industries:
        for name in sources:
            source = services.sources.sources.get(name)
            if source is None or source.blocked_reason():
                continue
            cfg = source.model_copy(update={"queries": queries(industry, name, limit)})
            try:
                run = await run_source(services, cfg, since=since, incremental=False, industry=industry.industry)
            except Exception:
                log.exception("targeted %s for %s failed", name, industry.industry)
                continue
            out[f"{name}:{industry.industry}"] = run.result.new
    return out
