"""robots.txt checks for public-pages connectors (RFC 9309), cached per origin for 24 hours.

The standard library's urllib.robotparser ignores `*` and `$` wildcards, which most large
sites use, so this module parses rules itself:
- the group for our product token wins over `*`; groups naming the same agent are merged
- the longest matching rule wins; on a tie, Allow wins
- 4xx on robots.txt means no restrictions; 5xx or unreachable means disallow everything
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit

import httpx

from .base import HttpClient

log = logging.getLogger(__name__)

DEFAULT_TTL = 24 * 3600.0
ERROR_TTL = 3600.0          # re-try a failing robots.txt sooner than a good one
MAX_ROBOTS_BYTES = 500 * 1024


class RobotsDisallowed(Exception):
    pass


@dataclass
class Rule:
    allow: bool
    path: str
    _regex: re.Pattern[str] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        anchored = self.path.endswith("$")
        body = self.path[:-1] if anchored else self.path
        pattern = ".*".join(re.escape(part) for part in unquote(body).split("*"))
        self._regex = re.compile(pattern + ("$" if anchored else ""))

    def matches(self, target: str) -> bool:
        return self._regex.match(target) is not None

    @property
    def specificity(self) -> int:
        return len(self.path)


@dataclass
class RobotsRules:
    rules: list[Rule] = field(default_factory=list)
    crawl_delay: float | None = None
    allow_all: bool = False
    disallow_all: bool = False
    unavailable: str | None = None   # why robots.txt couldn't be read, when that forced disallow-all

    def allowed(self, path_and_query: str) -> bool:
        if self.disallow_all:
            return False
        if self.allow_all:
            return True
        target = unquote(path_and_query) or "/"
        if target == "/robots.txt":
            return True
        best: Rule | None = None
        for rule in self.rules:
            if not rule.path or not rule.matches(target):
                continue
            if best is None or rule.specificity > best.specificity or (
                rule.specificity == best.specificity and rule.allow and not best.allow
            ):
                best = rule
        return best is None or best.allow


def parse_robots(text: str, agent: str) -> RobotsRules:
    """Pick the rules that apply to `agent` (our product token, e.g. "SignalBot")."""
    agent = agent.lower()
    groups: dict[str, RobotsRules] = defaultdict(RobotsRules)
    current_agents: list[str] = []
    in_rules = False
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = (s.strip() for s in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if in_rules:
                current_agents, in_rules = [], False
            current_agents.append(value.lower())
        elif key in {"allow", "disallow"}:
            in_rules = True
            for a in current_agents:
                groups[a].rules.append(Rule(allow=key == "allow", path=value))
        elif key == "crawl-delay":
            in_rules = True
            try:
                delay = float(value)
            except ValueError:
                continue
            for a in current_agents:
                groups[a].crawl_delay = delay
    for name, rules in groups.items():
        if name == agent:
            return rules
    return groups.get("*", RobotsRules(allow_all=True))


@dataclass
class _Cached:
    rules: RobotsRules
    expires: float


class RobotsChecker:
    def __init__(self, http: HttpClient, agent: str = "SignalBot", ttl: float = DEFAULT_TTL) -> None:
        self.http = http
        self.agent = agent
        self.ttl = ttl
        self._cache: dict[str, _Cached] = {}
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    @staticmethod
    def _origin(url: str) -> str:
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}".lower()

    async def rules_for(self, url: str) -> RobotsRules:
        origin = self._origin(url)
        async with self._locks[origin]:
            cached = self._cache.get(origin)
            if cached and cached.expires > time.monotonic():
                return cached.rules
            rules, ttl = await self._fetch(origin)
            self._cache[origin] = _Cached(rules, time.monotonic() + ttl)
            if rules.crawl_delay:
                host = urlsplit(origin).hostname or ""
                rps = min(self.http.limiter.rate(host), 1.0 / rules.crawl_delay)
                self.http.limiter.set_rate(host, rps)
            return rules

    async def _fetch(self, origin: str) -> tuple[RobotsRules, float]:
        url = f"{origin}/robots.txt"
        try:
            response = await self.http.get(url)
        except httpx.HTTPError as exc:
            log.warning("robots.txt unreachable at %s (%s): treating as disallow-all", url, exc)
            return RobotsRules(disallow_all=True, unavailable=type(exc).__name__), ERROR_TTL
        if response.status_code >= 500:
            log.warning("robots.txt at %s returned %s: treating as disallow-all", url, response.status_code)
            return RobotsRules(disallow_all=True, unavailable=f"HTTP {response.status_code}"), ERROR_TTL
        if response.status_code >= 400:
            return RobotsRules(allow_all=True), self.ttl
        return parse_robots(response.text[:MAX_ROBOTS_BYTES], self.agent), self.ttl

    async def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        target = parts.path or "/"
        if parts.query:
            target += "?" + parts.query
        return (await self.rules_for(url)).allowed(target)

    async def require(self, url: str) -> None:
        if not await self.allowed(url):
            raise RobotsDisallowed(f"robots.txt disallows {url} for {self.agent}")

    async def crawl_delay(self, url: str) -> float | None:
        return (await self.rules_for(url)).crawl_delay
