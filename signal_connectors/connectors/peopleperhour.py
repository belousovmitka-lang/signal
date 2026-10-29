"""PeoplePerHour: newest freelance jobs posted by buyers, from the public job listing (paid demand).

Access (checked 2026-10-29): robots.txt allows /freelance-jobs for all agents; the Terms
(peopleperhour.com/static/terms) have no clause on robots, scraping or automated access, and the job
listing is public and logged-out. No API or feed exists (/freelance-jobs.rss and /rss/jobs are 404).

`/freelance-jobs?sort=latest&page=N` lists 20 jobs per page, each card with the job's full description,
budget ("$239", "$2K", "$25/hr"), age ("16 hours ago") and proposal count, so no job page is fetched.
About 30 jobs a day (2026-10-29), so a run reads pages until it reaches the cursor (max_pages cap).
The buyer is stored only as a hash of their profile slug; the card's name and avatar are dropped.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any

from selectolax.lexbor import LexborHTMLParser

from ..base import BaseConnector
from ..models import RawPost, SourceQuery, utcnow
from ..privacy import hash_author, redact_contacts
from . import register

BASE = "https://www.peopleperhour.com"
LISTING = BASE + "/freelance-jobs"
AGE = re.compile(r"(\d+|an?|one)\s+(second|minute|hour|day|week|month|year)s?\s+ago", re.I)
UNITS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 7 * 86400, "month": 30 * 86400,
         "year": 365 * 86400}
PRICE = re.compile(r"([$£€])\s?([\d.,]+)\s*([KkMm]?)\s*(/\s*hr|per hour)?")
CURRENCIES = {"$": "USD", "£": "GBP", "€": "EUR"}


def parse_age(text: str, now: datetime) -> datetime | None:
    if re.search(r"\bjust now\b", text, re.I):
        return now
    if re.search(r"\byesterday\b", text, re.I):
        return now - timedelta(days=1)
    m = AGE.search(text)
    if not m:
        return None
    n = 1 if m.group(1).lower() in ("a", "an", "one") else int(m.group(1))
    return now - timedelta(seconds=n * UNITS[m.group(2).lower()])


def parse_price(text: str) -> dict[str, Any] | None:
    m = PRICE.search(text or "")
    if not m:
        return None
    amount = float(m.group(2).replace(",", ""))
    amount *= {"k": 1_000, "m": 1_000_000}.get(m.group(3).lower(), 1)
    return {"amount": amount, "currency": CURRENCIES[m.group(1)], "type": "hourly" if m.group(4) else "fixed",
            "stated": m.group(0).strip()}


@register
class PeoplePerHourConnector(BaseConnector):
    connector = "peopleperhour"
    route = "public_pages"
    source_type = "paid_demand"
    default_cadence = "hourly"

    async def fetch(self, query: SourceQuery) -> AsyncIterator[RawPost]:
        max_pages = int(self.source.options.get("max_pages", 5))
        fetched = 0
        for page in range(1, max_pages + 1):
            response = await self.fetch_page(LISTING, params={"sort": "latest", "page": page})
            response.raise_for_status()
            now = utcnow()
            cards = LexborHTMLParser(response.text).css("li[class^='list__item']")
            posts = [p for card in cards if (p := self._to_post(card, now, query))]
            for post in posts:
                yield post
                fetched += 1
                if fetched >= query.limit:
                    return
            if not posts or (query.since and min(p.published_at for p in posts) < query.since):
                return

    def _to_post(self, card: Any, now: datetime, query: SourceQuery) -> RawPost | None:
        link = card.css_first("h6 a")
        if link is None or not (href := link.attributes.get("href")):
            return None
        url = href if href.startswith("http") else BASE + href
        footer = card.css_first("[class*='card__footer-left']")
        footer_text = footer.text(separator=" ", strip=True) if footer else ""
        published = parse_age(footer_text, now)
        if published is None:
            return None
        desc = card.css_first("[class^='item__desc']")
        price = card.css_first("[class*='card__price']")
        user = card.css_first("a[class*='card__user-link']")
        proposals = re.search(r"(\d+)\s+proposals?", footer_text)
        parts = [p for p in url.removeprefix(LISTING + "/").split("/")[:-1] if p]
        money = parse_price(price.text(strip=True)) if price else None
        tags = [t.text(strip=True) for t in card.css("[class*='JobEtiquette']") if t.text(strip=True)]
        return self.make_post(
            url=url,
            title=link.text(strip=True),
            text=redact_contacts(desc.text(separator=" ", strip=True)) if desc else "",
            is_snippet=False,
            author_hash=hash_author(user.attributes.get("href", ""), "peopleperhour") if user else None,
            community=" / ".join(parts) or None,
            published_at=published,
            language=None,
            engagement={"proposals": int(proposals.group(1))} if proposals else {},
            money=money,
            industry_hint=query.industry_hint,
            raw={"job_id": url.rsplit("-", 1)[-1], "category": parts, "age": footer_text.split(" ago")[0] + " ago",
                 "labels": tags, "remote": "Remote" in footer_text},
        )
