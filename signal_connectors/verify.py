"""Live checks for industry seed maps: is each feed, forum and site live and readable?

Statuses written back:
  live         fetched fine (feeds: parsed with entries)
  dead         404/410, DNS or connection failure, empty or unparseable feed
  blocked      robots.txt disallows us, or the site answers 401/403 to our bot
  private      the community lives on a private platform (Circle, Skool, Discord)
"""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import date
from urllib.parse import urlsplit

import feedparser
import httpx

from .base import HttpClient
from .forum_detect import advertised_feeds, detect_forum, wall_of
from .industries import Feed, Forum, Industry, Link
from .robots import RobotsChecker

BLOCKED_CODES = {401, 403, 451}


async def robots_verdict(robots: RobotsChecker, url: str) -> str | None:
    """None if we may fetch; "dead: ..." if the site is unreachable; "blocked" if robots.txt says no."""
    if await robots.allowed(url):
        return None
    rules = await robots.rules_for(url)
    return f"dead: site unreachable ({rules.unavailable})" if rules.unavailable else "blocked"


async def check_feed(http: HttpClient, robots: RobotsChecker, feed: Feed, today: date) -> None:
    if feed.locked:
        return
    await _check_feed_url(http, robots, feed, today)
    if feed.status != "dead" or (feed.notes or "").startswith("site unreachable"):
        return
    # The feed may have moved: look for the one the site's homepage advertises.
    origin = "{0.scheme}://{0.netloc}/".format(urlsplit(feed.url))
    try:
        if not await robots.allowed(origin):
            return
        home = await http.get(origin)
    except httpx.HTTPError:
        return
    if home.status_code != 200 or wall_of(str(home.url)):
        return
    old_url, old_notes = feed.url, feed.notes
    for candidate in advertised_feeds(home.text, str(home.url)):
        if "comments" in candidate.lower():
            continue
        feed.url = candidate
        await _check_feed_url(http, robots, feed, today)
        if feed.status == "live":
            feed.notes = f"moved from {old_url} (found on homepage)"
            return
    feed.url, feed.status, feed.notes = old_url, "dead", old_notes


async def _check_feed_url(http: HttpClient, robots: RobotsChecker, feed: Feed, today: date) -> None:
    feed.checked_at = today
    try:
        if verdict := await robots_verdict(robots, feed.url):
            feed.status, feed.notes = ("dead", verdict[6:]) if verdict.startswith("dead") else ("blocked", "robots.txt disallows")
            return
        r = await http.get(feed.url)
    except httpx.HTTPError as exc:
        feed.status, feed.notes = "dead", type(exc).__name__
        return
    if r.status_code in BLOCKED_CODES:
        feed.status, feed.notes = "blocked", f"HTTP {r.status_code} to our bot"
        return
    if r.status_code != 200:
        feed.status, feed.notes = "dead", f"HTTP {r.status_code}"
        return
    parsed = feedparser.parse(r.content)
    feed.entries = len(parsed.entries)
    feed.full_text = any(e.get("content") for e in parsed.entries)
    if feed.entries:
        feed.status, feed.notes = "live", None
        if str(r.url) != feed.url:
            feed.notes = f"redirects to {r.url}"
    else:
        feed.status, feed.notes = "dead", "no entries" if not parsed.bozo else "not a feed"


async def check_forum(http: HttpClient, robots: RobotsChecker, forum: Forum, today: date) -> None:
    if forum.locked:
        return
    forum.checked_at = today
    if (verdict := await robots_verdict(robots, forum.url)) and verdict.startswith("dead"):
        forum.status, forum.notes, forum.robots = "dead", verdict[6:], "unchecked"
        return
    d = await detect_forum(http, robots, forum.url)
    forum.robots = "allowed" if d.robots_allowed else "disallowed"
    forum.software = d.software if d.software != "unknown" or forum.software == "unknown" else forum.software
    forum.rss = d.feed or forum.rss
    if not d.robots_allowed:
        forum.status, forum.notes = "search_only", "robots.txt disallows; use the search route"
    elif d.wall == "login":
        forum.status, forum.notes = "blocked", f"behind a login ({d.final_url})"
    elif d.wall == "challenge":
        forum.status, forum.notes = "search_only", "bot challenge page; never bypass (rule 1); use the search route"
    elif d.private:
        forum.status, forum.notes = "private", f"redirects to {d.final_url}"
    elif d.error:
        forum.status, forum.notes = "dead", d.error
    elif d.status_code in BLOCKED_CODES:
        forum.status, forum.notes = "search_only", f"HTTP {d.status_code} to our bot; use the search route"
    elif d.status_code != 200:
        forum.status, forum.notes = "dead", f"HTTP {d.status_code}"
    else:
        forum.status = "live"
        forum.notes = None if d.final_url == forum.url else f"redirects to {d.final_url}"


async def check_link(http: HttpClient, robots: RobotsChecker, link: Link, today: date) -> None:
    if link.locked:
        return
    if not link.url:
        return
    link.checked_at = today
    try:
        if verdict := await robots_verdict(robots, link.url):
            link.status, link.notes = ("dead", verdict[6:]) if verdict.startswith("dead") else ("search_only", "robots.txt disallows")
            return
        r = await http.get(link.url)
    except httpx.HTTPError as exc:
        link.status, link.notes = "dead", type(exc).__name__
        return
    wall = wall_of(str(r.url))
    if r.status_code in BLOCKED_CODES or wall:
        link.status, link.notes = "search_only", f"HTTP {r.status_code} to our bot" if not wall else f"{wall} page"
    elif r.status_code >= 400:
        link.status, link.notes = "dead", f"HTTP {r.status_code}"
    else:
        link.status, link.notes = "live", None


async def verify_industry(http: HttpClient, robots: RobotsChecker, industry: Industry,
                          today: date | None = None) -> Counter[str]:
    today = today or date.today()
    jobs = [check_feed(http, robots, f, today) for f in industry.rss_feeds]
    jobs += [check_forum(http, robots, f, today) for f in industry.forums]
    jobs += [check_link(http, robots, link, today)
             for group in (industry.associations, industry.job_boards, industry.podcasts, industry.youtube_channels)
             for link in group]
    await asyncio.gather(*jobs)
    counts: Counter[str] = Counter()
    for group in (industry.rss_feeds, industry.forums, industry.associations, industry.job_boards):
        counts.update(e.status for e in group)
    return counts
