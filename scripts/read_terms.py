"""Help a human read a forum's terms: find the terms page and print the clauses that decide rule 2.

    python scripts/read_terms.py https://thecleaningstandard.com/forum/ [more urls...]

For each URL it tries the page itself, then the usual terms paths for forum software (XenForo
/help/terms/, phpBB ucp.php?mode=terms, Discourse /tos, MyBB misc.php?action=help, generic /terms),
robots.txt first, and prints word count plus every sentence about automated access, scraping,
commercial use, copying, AI, or the network that owns the site (VerticalScope, Internet Brands...).
Deciding stays with the reader: set terms: allowed|forbidden and terms_notes in the industry map.
"""

import argparse
import asyncio
import html
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from signal_connectors.base import HttpClient  # noqa: E402
from signal_connectors.robots import RobotsChecker  # noqa: E402
from signal_connectors.settings import Settings  # noqa: E402

PATHS = ["help/terms/", "ucp.php?mode=terms", "tos", "terms", "terms-of-service", "terms-of-use", "legal",
         "misc.php?action=help", "help/terms-of-service/"]
CLAUSE = re.compile(r"[^.]{0,220}\b(scrap\w*|crawl\w*|robots?|spider|automat\w*|harvest\w*|data mining|extract\w*|"
                    r"non-?commercial|personal use|commercial (use|purpose)\w*|reproduc\w*|copy(ing)?|republish\w*|"
                    r"artificial intelligence|machine learning|train\w*|verticalscope|internet brands|"
                    r"written (consent|permission))\b[^.]{0,220}\.", re.I)


TERMS_LINK = re.compile(r'<a\b[^>]*href="([^"#]+)"[^>]*>([^<]{0,60})</a>', re.I)
TERMS_WORDS = re.compile(r"terms|tos\b|conditions|user agreement|legal|rules", re.I)


def terms_links(page: str, base: str) -> list[str]:
    return [urljoin(base, html.unescape(href)) for href, label in TERMS_LINK.findall(page)
            if TERMS_WORDS.search(label) or TERMS_WORDS.search(href.rsplit("/", 2)[-2] if "/" in href else href)]


def text_of(page: str) -> str:
    page = re.sub(r"(?s)<(script|style|nav|header|footer)\b.*?</\1>", " ", page)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", page))).strip()


async def read(http: HttpClient, robots: RobotsChecker, url: str) -> None:
    base = url if url.endswith("/") else url.rsplit("/", 1)[0] + "/"
    origin = "{0.scheme}://{0.netloc}/".format(urlsplit(url))
    print(f"===== {url}")
    linked: list[str] = []
    try:
        if await robots.allowed(url):
            home = await http.get(url, attempts=1)
            if home.status_code == 200:
                linked = terms_links(home.text, str(home.url))
    except Exception:
        pass
    candidates = linked + [urljoin(base, p) for p in PATHS] + [urljoin(origin, p) for p in PATHS]
    if TERMS_WORDS.search(urlsplit(url).path + "?" + urlsplit(url).query):
        candidates.insert(0, url)
    for cand in dict.fromkeys(candidates):
        try:
            if not await robots.allowed(cand):
                continue
            r = await http.get(cand, attempts=1)
        except Exception:
            continue
        if r.status_code != 200 or "html" not in r.headers.get("content-type", "html"):
            continue
        body = text_of(r.text)
        if not re.search(r"\b(terms|rules|agreement|conditions)\b", body[:3000], re.I):
            continue
        hits = list(dict.fromkeys(m.group(0).strip() for m in CLAUSE.finditer(body)))
        print(f"  terms page: {r.url}  ({len(body.split())} words)")
        for h in hits[:10]:
            print(f"    .. {h[:440]}")
        if not hits:
            print("    (no clause on automation, commercial use, copying or AI)")
        return
    print("  no terms page found")


async def main(urls: list[str]) -> int:
    settings = Settings.from_env()
    async with HttpClient(user_agent=settings.user_agent, max_attempts=1, timeout=20) as http:
        robots = RobotsChecker(http)
        for url in urls:
            await read(http, robots, url)
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("urls", nargs="+")
    sys.exit(asyncio.run(main(p.parse_args().urls)))
