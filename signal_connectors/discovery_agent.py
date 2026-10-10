"""Phase 6: draft an industry seed map for a new industry, for a human to review.

Pipeline:
1. Ask Exa and Brave a small, budgeted set of queries built from the seeds (roles, keywords).
   Results are pointers only (Brave 3(b)(i), Exa 4.2(a)): URLs are held in memory and reduced
   to site origins; nothing from a result is written.
2. Visit each origin's homepage (robots.txt first) and keep what the site itself says: its
   final URL, <title>, advertised feeds, forum software, and a link to its terms page.
3. Classify: forum (forum software or forum-like path), publication (a live feed),
   association (its own title says association, society, institute...).
4. Run the normal verification (verify.py) on every entry: liveness, robots.txt, login walls,
   bot challenges, moved feeds.
5. Suggest jargon from the most frequent phrases in the publications' headlines.
6. Write industries/<key>.yaml with discovery.needs_review: true. Every forum gets
   terms: unchecked, so nothing is collected until a human reads its terms.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urljoin, urlsplit

import feedparser
from selectolax.lexbor import LexborHTMLParser

from .discovery import Discovery
from .forum_detect import advertised_feeds, detect_software, wall_of
from .industries import DiscoveryInfo, Feed, Forum, Industry, Link, Vocabulary, load_industries
from .services import Services
from .verify import verify_industry

log = logging.getLogger(__name__)

# Platforms with their own connectors, or that we can't read: never candidates.
EXCLUDED = [
    "reddit.com", "facebook.com", "linkedin.com", "youtube.com", "x.com", "twitter.com", "instagram.com",
    "tiktok.com", "wikipedia.org", "amazon.com", "quora.com", "pinterest.com", "medium.com", "substack.com",
    "github.com", "news.ycombinator.com", "stackexchange.com", "stackoverflow.com", "bsky.app", "threads.net",
    "apple.com", "google.com", "glassdoor.com", "indeed.com", "yelp.com",
]
FORUM_HINT = re.compile(r"/(forums?|community|communities|board|boards|discuss|talk)(/|$)|^(forums?|community|discuss)\.", re.I)
ASSOCIATION_HINT = re.compile(r"\b(association|society|institute|federation|council|chamber|guild|alliance|"
                              r"academy|college of|board of|union)\b", re.I)
TERMS_LINK = re.compile(r"terms|tos|conditions|user[- ]agreement|legal", re.I)
STOP = set("""a an and the of to in for on with at by from is are was were be been it its this that these those as or
not no new how why what when who your you our we they their his her he she i me my us can will may more most all any
about after over into than then out up down off just also amid says said say report reports news update updates vs via
2024 2025 2026 2027 one two three first year years day week month
post posts blog blogs forum forums re rss feed comment comments reply replies thread threads member members activity
activities read more click here episode podcast webinar newsletter summary weekly monthly daily
january february march april may june july august september october november december""".split())

COST_PER_QUERY = {"exa": 0.007, "brave": 0.005}


@dataclass
class Seeds:
    key: str
    name: str
    roles: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)


@dataclass
class Candidate:
    origin: str
    hits: int = 0
    paths: set[str] = field(default_factory=set)


def build_queries(seeds: Seeds) -> list[tuple[str, str]]:
    """(provider, query) pairs: semantic queries for Exa, keyword queries for Brave."""
    roles = seeds.roles[:3] or [seeds.name]
    q: list[tuple[str, str]] = []
    for role in roles:
        q.append(("exa", f"online forum or community where {role}s discuss their work and problems"))
    q += [
        ("exa", f"{seeds.name} industry trade publication and news site"),
        ("exa", f"{seeds.name} professional association or trade group"),
        ("exa", f"newsletter or blog for people working in {seeds.name}"),
    ]
    for kw in (seeds.keywords or [seeds.name])[:2]:
        q.append(("brave", f'"{kw}" forum'))
    q.append(("brave", f'"{seeds.name}" association'))
    return q


_TWO_LABEL_SUFFIXES = {"co.uk", "org.uk", "ac.uk", "gov.uk", "com.au", "org.au", "co.nz", "co.za", "com.br", "co.jp"}


def base_domain(host: str) -> str:
    """Registrable domain, approximately: forums.studentdoctor.net -> studentdoctor.net."""
    labels = (host or "").lower().removeprefix("www.").split(".")
    keep = 3 if ".".join(labels[-2:]) in _TWO_LABEL_SUFFIXES else 2
    return ".".join(labels[-keep:])


def origin_of(url: str) -> str | None:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not host or any(host == d or host.endswith("." + d) for d in EXCLUDED):
        return None
    return f"{parts.scheme or 'https'}://{host}"


def group_origins(urls: list[str]) -> dict[str, Candidate]:
    out: dict[str, Candidate] = {}
    for url in urls:
        origin = origin_of(url)
        if not origin:
            continue
        cand = out.setdefault(origin, Candidate(origin))
        cand.hits += 1
        path = urlsplit(url).path
        m = FORUM_HINT.search(path)
        if m:
            cand.paths.add(path[: m.end()].rstrip("/") + "/")
    return out


def jargon_candidates(titles: list[str], seeds: Seeds, n: int = 15) -> list[str]:
    """Most frequent 2-word phrases (then words) in headlines, minus stopwords and the seeds themselves."""
    seen_words = {w.lower() for s in (seeds.name, *seeds.roles, *seeds.keywords) for w in s.split()}
    bigrams: Counter[str] = Counter()
    words: Counter[str] = Counter()
    for t in titles:
        toks = [w for w in re.findall(r"[a-z][a-z0-9'-]+", t.lower()) if w not in STOP]
        words.update(w for w in toks if w not in seen_words and len(w) > 3)
        bigrams.update(f"{a} {b}" for a, b in zip(toks, toks[1:]) if not ({a, b} <= seen_words))
    picks = [p for p, c in bigrams.most_common(n * 2) if c >= 2][:n]
    if len(picks) < n:
        picks += [w for w, c in words.most_common(n * 2) if c >= 3 and not any(w in p for p in picks)][: n - len(picks)]
    return picks


@dataclass
class Visit:
    final_url: str
    title: str
    software: str
    feeds: list[str]
    terms_url: str | None


async def visit(services: Services, origin: str, forum_path: str | None) -> Visit | None:
    """Fetch a homepage (or forum path) the polite way and keep only what the site says about itself."""
    url = origin + (forum_path or "/")
    if not await services.robots.allowed(url):
        return None
    try:
        r = await services.http.get(url)
    except Exception:  # an unreachable site is just not a candidate
        return None
    if r.status_code != 200 or wall_of(str(r.url)) or "html" not in r.headers.get("content-type", "html"):
        return None
    tree = LexborHTMLParser(r.text)
    title_node = tree.css_first("title")
    title = " ".join((title_node.text() if title_node else "").split())[:120] or urlsplit(str(r.url)).hostname or origin
    terms = None
    for a in tree.css("a[href]"):
        if TERMS_LINK.search(a.text() or "") or TERMS_LINK.search(a.attributes.get("href") or ""):
            terms = urljoin(str(r.url), a.attributes["href"])
            break
    return Visit(str(r.url), title, detect_software(r.text), advertised_feeds(r.text, str(r.url)), terms)


async def discover(services: Services, seeds: Seeds, *, budget_usd: float = 0.25, max_sites: int = 40,
                   today: date | None = None) -> Industry:
    today = today or date.today()
    disc = Discovery(services.http, services.costs, "discovery")
    spent_before = services.costs.spent_this_month("discovery")
    queries = build_queries(seeds)
    urls: list[str] = []
    spent, ran = 0.0, 0
    for provider, text in queries:
        cost = COST_PER_QUERY[provider]
        if spent + cost > budget_usd:
            log.info("discovery budget reached after %d queries", ran)
            break
        try:
            found = await disc.urls(provider, text, [], None, 10 if provider == "exa" else 20, exclude=EXCLUDED)
        except Exception as exc:
            log.warning("discovery query failed (%s): %s", provider, exc)
            continue
        urls += found       # pointers only; reduced to origins below and then dropped
        spent += cost
        ran += 1

    candidates = sorted(group_origins(urls).values(), key=lambda c: -c.hits)[:max_sites]
    # Terms already read for another industry carry over (by host), so nobody reviews them twice.
    known: dict[str, Forum] = {}
    for other in load_industries().values():
        for f in other.forums:
            if f.terms != "unchecked":
                known[base_domain(urlsplit(f.url).hostname or "")] = f
    forums: list[Forum] = []
    feeds: list[Feed] = []
    associations: list[Link] = []
    for cand in candidates:
        v = await visit(services, cand.origin, next(iter(sorted(cand.paths)), None))
        if v is None:
            continue
        host = urlsplit(v.final_url).hostname or cand.origin
        is_forum = v.software not in ("unknown", "custom") or bool(cand.paths) or bool(FORUM_HINT.search(v.final_url))
        if is_forum:
            prior = known.get(base_domain(host))   # terms belong to the organization, so match by domain
            forums.append(Forum(name=v.title, url=v.final_url, software=v.software,
                                rss=v.feeds[0] if v.feeds else None,
                                terms=prior.terms if prior else "unchecked",
                                terms_url=prior.terms_url if prior else v.terms_url,
                                terms_notes=(prior.terms_notes + " (carried over from an existing map)")
                                if prior and prior.terms_notes else None))
        elif v.feeds:
            feeds.append(Feed(name=host.removeprefix("www.").split(".")[0], url=v.feeds[0]))
        elif ASSOCIATION_HINT.search(v.title):
            associations.append(Link(name=v.title, url=v.final_url))

    industry = Industry(
        industry=seeds.key, name=seeds.name,
        vocabulary=Vocabulary(roles=seeds.roles, tools=seeds.tools, pain_phrases=[], jargon=[]),
        forums=forums, rss_feeds=feeds, associations=associations,
    )
    counts = await verify_industry(services.http, services.robots, industry, today)

    titles: list[str] = []
    for f in industry.live_feeds()[:10]:
        try:
            parsed = feedparser.parse((await services.http.get(f.url)).content)
            titles += [e.get("title", "") for e in parsed.entries]
        except Exception:
            continue
    industry.vocabulary.jargon = jargon_candidates(titles, seeds)
    industry.discovery = DiscoveryInfo(
        generated_at=today, queries=ran, candidates_checked=len(candidates),
        cost_usd=round(services.costs.spent_this_month("discovery") - spent_before, 4),
        notes=(f"Draft from {ran} searches. Verify each site, read every forum's terms (terms_url) and set "
               f"terms: allowed only if they permit automated, commercial use. Jargon is auto-extracted from "
               f"headlines. Statuses: {dict(counts)}. Subreddits not searched (reddit.com is unreadable)."),
    )
    return industry
