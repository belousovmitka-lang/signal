"""Detect a forum's software and its feeds from one logged-out page (used by G4/G5 and discovery).

Respects robots.txt: the page and each candidate feed path are checked before fetching.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

import feedparser
from selectolax.lexbor import LexborHTMLParser

from .base import HttpClient
from .robots import RobotsChecker

# Default feed paths per platform, tried when the page doesn't advertise a feed.
DEFAULT_FEEDS = {
    "discourse": ["/latest.rss"],
    "xenforo": ["/forums/-/index.rss", "/forums/index.rss"],
    "vbulletin": ["/external.php?type=RSS2", "/forum/external.php?type=RSS2"],
    "phpbb": ["/app.php/feed", "/feed.php"],
    "invision": [],
    "khoros": [],
}

_SIGNATURES: list[tuple[str, re.Pattern[str]]] = [
    ("discourse", re.compile(r'<meta name="generator" content="Discourse|data-discourse-|discourse-cdn', re.I)),
    ("xenforo", re.compile(r'<html[^>]+id="XF"|XenForo|data-xf-init', re.I)),
    ("vbulletin", re.compile(r'content="vBulletin|vbulletin_css|vBulletin\.version', re.I)),
    ("phpbb", re.compile(r'phpBB|<body[^>]+id="phpbb"', re.I)),
    ("invision", re.compile(r'Invision Community|ipsApp|ips4', re.I)),
    ("khoros", re.compile(r'lia-component|khoros|lithium\.', re.I)),
]

_PRIVATE = re.compile(r"circle\.so|skool\.com|discord\.(gg|com)/invite", re.I)
_LOGIN = re.compile(r"/(log-?in|sign-?in|sso|auth|account/login)\b|[?&](returnurl|return_to)=", re.I)
_CHALLENGE = re.compile(r"challenge|captcha|cdn-cgi/|/\.well-known/sgcaptcha|px-captcha", re.I)


def wall_of(final_url: str) -> str | None:
    """"login" or "challenge" when a request landed on a login page or a bot check (rule 1: never bypass)."""
    path = final_url.split("://", 1)[-1].split("/", 1)[-1]
    if _CHALLENGE.search(path):
        return "challenge"
    if _LOGIN.search("/" + path):
        return "login"
    return None


@dataclass
class Detection:
    url: str
    final_url: str | None = None
    status_code: int | None = None
    software: str = "unknown"
    feeds: list[str] = field(default_factory=list)
    robots_allowed: bool = True
    private: bool = False
    wall: str | None = None          # "login" or "challenge"
    error: str | None = None

    @property
    def feed(self) -> str | None:
        return self.feeds[0] if self.feeds else None


def detect_software(html: str) -> str:
    for name, pattern in _SIGNATURES:
        if pattern.search(html):
            return name
    return "unknown"


def advertised_feeds(html: str, base_url: str) -> list[str]:
    tree = LexborHTMLParser(html)
    feeds = []
    for link in tree.css('link[rel="alternate"]'):
        kind = (link.attributes.get("type") or "").lower()
        href = link.attributes.get("href")
        if href and ("rss" in kind or "atom" in kind):
            feeds.append(urljoin(base_url, href))
    return list(dict.fromkeys(feeds))


async def is_feed(http: HttpClient, robots: RobotsChecker, url: str) -> bool:
    if not await robots.allowed(url):
        return False
    response = await http.get(url)
    if response.status_code != 200:
        return False
    parsed = feedparser.parse(response.content)
    return bool(parsed.entries) or (not parsed.bozo and bool(parsed.feed.get("title")))


async def detect_forum(http: HttpClient, robots: RobotsChecker, url: str) -> Detection:
    result = Detection(url=url)
    try:
        if not await robots.allowed(url):
            result.robots_allowed = False
            return result
        response = await http.get(url)
        result.status_code = response.status_code
        result.final_url = str(response.url)
        if response.status_code != 200:
            return result
        result.wall = wall_of(result.final_url)
        if result.wall:
            return result
        html = response.text
        if _PRIVATE.search(result.final_url) or _PRIVATE.search(urlsplit(result.final_url).netloc):
            result.private = True
        result.software = detect_software(html)
        candidates = advertised_feeds(html, result.final_url)
        origin = "{0.scheme}://{0.netloc}".format(urlsplit(result.final_url))
        base = result.final_url.rstrip("/")
        for path in DEFAULT_FEEDS.get(result.software, []):
            candidates += [base + path, origin + path]
        for candidate in dict.fromkeys(candidates):
            if await is_feed(http, robots, candidate):
                result.feeds.append(candidate)
                break
    except Exception as exc:  # network errors are a result here, not a failure
        result.error = f"{type(exc).__name__}: {exc}"[:200]
    return result
