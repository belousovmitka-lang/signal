"""Live lane: search results shown to a user at view time, never stored.

For platforms we can't collect (Reddit, X, Facebook groups, YouTube; LinkedIn, Quora on request),
the app asks this module when a user opens their feed: one site-limited Brave search per platform, built
from the industry's main role, plus a YouTube search through the YouTube Data API. Results are filtered
to threads and posts (never profile pages). Results go
straight back to the app with links; nothing is written to the database (only the cost ledger).

Terms (read 2026-10-15):
- Brave Search API: 3(a)(ii) licenses use of Search Results "with the Customer Applications";
  3(b)(i) no storing or caching except "transient storage required for operation" (results live in
  memory for CACHE_SECONDS so one feed view and its reloads cost one search); 3(b)(ii) no derivative
  works (titles and snippets are passed through unmodified, in Brave's order); 3(b)(xiii) no AI
  training on them; 4(c)(iii): the app's own terms must bind users to similar restrictions.
  Attribution optional ("POWERED BY BRAVE").
- Exa is not used here: its ToS 4.2(a) forbids displaying information obtained through the service
  without written permission.
- YouTube API Developer Policies: show YouTube as the source with its brand features (III.F.2.a);
  don't modify results (III.C.5) or mix them with other sources as YouTube results (so YouTube is its
  own section); no derived data or metrics from API data (III.E.4.h: not ranked or scored by us);
  Non-Authorized Data may be kept at most 30 days (III.E.4.d; here, minutes in memory).
  search.list has its own quota of about 100 calls a day (metered as `live_youtube`).
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import Any

from .discovery import BRAVE_USD, BRAVE_WEB, site_filter
from .industries import Industry
from .services import Services

YOUTUBE_SEARCH = "https://www.googleapis.com/youtube/v3/search"
CACHE_SECONDS = 15 * 60
BRAVE_SOURCE = "live"
YOUTUBE_SOURCE = "live_youtube"
YOUTUBE_DAILY_SEARCHES = 90            # under the ~100/day search.list bucket
# platform -> (label, site filters, URL pattern a result must match: threads/posts, never profiles or tag pages)
PLATFORMS: dict[str, tuple[str, list[str], str]] = {
    "reddit": ("Reddit", ["reddit.com"], r"/comments/"),
    "x": ("X", ["x.com"], r"/status/\d+"),
    "facebook": ("Facebook groups", ["facebook.com/groups"], r"/groups/[^/]+/(posts|permalink)/"),
    "linkedin": ("LinkedIn", ["linkedin.com"], r"linkedin\.com/(posts|pulse|feed/update)/"),   # never /in/ profiles (rule 4)
    "quora": ("Quora", ["quora.com"], r"quora\.com/(?!profile/)[^/?]+/?$"),
}
# Measured 2026-10-15 (dental, salons): Reddit and YouTube return real posts, X and Facebook groups a few;
# LinkedIn returned only profiles and Quora nothing, so they stay available but off. TikTok dropped (not wanted).
DEFAULT_PLATFORMS = ["reddit", "x", "facebook", "youtube"]
INTENT = 'software "how do you"'       # practitioners asking how others handle something; tested best


@dataclass
class LiveItem:
    title: str
    url: str
    snippet: str                     # as the provider returned it (may contain <strong> highlighting)
    age: str | None = None
    channel: str | None = None       # YouTube only
    thumbnail: str | None = None     # YouTube only


@dataclass
class LiveSection:
    platform: str
    label: str
    provider: str                    # "brave" | "youtube"
    attribution: str
    items: list[LiveItem] = field(default_factory=list)
    error: str | None = None


@dataclass
class LiveResults:
    industry: str
    query: str
    sections: list[LiveSection]
    stored: bool = False             # always False: show, link out, forget

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_query(industry: Industry, extra: str | None = None) -> str:
    """'"salon owner" software "how do you"': the main role, quoted when it is several words.

    Long OR lists of quoted phrases matched almost nothing on Brave (tested 2026-10-15)."""
    role = (industry.vocabulary.roles[:1] or [industry.name])[0]
    who = f'"{role}"' if " " in role else role
    return " ".join(p for p in (who, extra or INTENT) if p)


_cache: dict[tuple[str, ...], tuple[float, LiveSection]] = {}


def _cached(key: tuple[str, ...]) -> LiveSection | None:
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    _cache.pop(key, None)
    return None


def _remember(key: tuple[str, ...], section: LiveSection) -> LiveSection:
    if section.error is None:
        now = time.monotonic()
        for k in [k for k, (t, _) in _cache.items() if now - t >= CACHE_SECONDS]:   # transient only
            del _cache[k]
        _cache[key] = (now, section)
    return section


async def _brave(services: Services, platform: str, query: str, limit: int, freshness: str) -> LiveSection:
    label, domains, pattern = PLATFORMS[platform]
    section = LiveSection(platform, label, "brave", attribution="Search results: POWERED BY BRAVE (optional); "
                          f"links open on {label}")
    key = os.environ.get("BRAVE_API_KEY")
    if not key:
        section.error = "BRAVE_API_KEY is not set"
        return section
    with services.costs.metered(BRAVE_SOURCE, usd=BRAVE_USD, provider="brave", note=f"live {platform}"):
        response = await services.http.get(BRAVE_WEB, attempts=1,          # a user is waiting: no retries
                                           headers={"X-Subscription-Token": key, "Accept": "application/json"},
                                           params={"q": f"{site_filter(domains)} {query}"[:400], "count": 20,
                                                   "freshness": freshness, "result_filter": "web", "search_lang": "en"})
    if response.status_code >= 400:
        section.error = f"Brave HTTP {response.status_code}"
        return section
    for r in response.json().get("web", {}).get("results", []):
        if r.get("url") and re.search(pattern, r["url"]) and len(section.items) < limit:
            section.items.append(LiveItem(title=r.get("title", ""), url=r["url"], snippet=r.get("description", ""),
                                          age=r.get("age") or r.get("page_age")))
    return section


async def _youtube(services: Services, query: str, limit: int, days: int) -> LiveSection:
    section = LiveSection("youtube", "YouTube", "youtube",
                          attribution="Show the YouTube logo with this section (YouTube API policy III.F.2.a); "
                                      "titles, thumbnails and links unmodified")
    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        section.error = "YOUTUBE_API_KEY is not set"
        return section
    after = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days * 86400))
    with services.costs.metered(YOUTUBE_SOURCE, units=1, quota=(YOUTUBE_DAILY_SEARCHES, timedelta(days=1)), note="search.list"):
        response = await services.http.get(YOUTUBE_SEARCH, attempts=1, params={
            "part": "snippet", "q": query, "type": "video", "maxResults": min(limit, 25), "order": "relevance",
            "publishedAfter": after, "relevanceLanguage": "en", "key": key})
    if response.status_code >= 400:
        section.error = f"YouTube HTTP {response.status_code}"
        return section
    for r in response.json().get("items", [])[:limit]:
        vid, sn = (r.get("id") or {}).get("videoId"), r.get("snippet") or {}
        if vid:
            section.items.append(LiveItem(
                title=sn.get("title", ""), url=f"https://www.youtube.com/watch?v={vid}", snippet=sn.get("description", ""),
                age=sn.get("publishedAt"), channel=sn.get("channelTitle"),
                thumbnail=((sn.get("thumbnails") or {}).get("medium") or {}).get("url")))
    return section


async def live_search(services: Services, industry: Industry, *, platforms: list[str] | None = None,
                      extra: str | None = None, limit: int = 8, freshness: str = "py",
                      youtube_days: int = 60) -> LiveResults:
    """Search each platform now and return the results for display. Nothing is stored.

    `freshness`: Brave's window ("pd" day, "pw" week, "pm" month, "py" year). A failing platform
    gets an `error` and an empty section; the others still return.
    """
    query = build_query(industry, extra)
    # YouTube search takes plain keywords: the main role plus its top pain phrase
    v = industry.vocabulary
    yt_query = " ".join(p for p in ((v.roles[:1] or [industry.name])[0], (v.pain_phrases[:1] or [""])[0], extra) if p)
    sections: list[LiveSection] = []
    for platform in platforms or DEFAULT_PLATFORMS:
        key = (industry.industry, platform, query if platform != "youtube" else yt_query, freshness, str(limit))
        if cached := _cached(key):
            sections.append(cached)
            continue
        try:
            if platform == "youtube":
                section = await _youtube(services, yt_query, limit, youtube_days)
            elif platform in PLATFORMS:
                section = await _brave(services, platform, query, limit, freshness)
            else:
                section = LiveSection(platform, platform, "none", "", error="unknown platform")
        except Exception as exc:          # budget, quota, network: this platform is skipped, the rest return
            label = PLATFORMS[platform][0] if platform in PLATFORMS else "YouTube" if platform == "youtube" else platform
            section = LiveSection(platform, label, "youtube" if platform == "youtube" else "brave", "",
                                  error=f"{type(exc).__name__}: {exc}"[:200])
        sections.append(_remember(key, section))
    return LiveResults(industry=industry.industry, query=query, sections=sections)
