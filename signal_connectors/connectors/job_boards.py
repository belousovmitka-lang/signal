"""G6 job boards: company job posts from public ATS posting APIs (Greenhouse, Lever, Ashby), no key.

One query per company board, built from the `job_boards` of every industry map (each entry's url is
the board: boards.greenhouse.io/<token>, jobs.lever.co/<site>, jobs.ashbyhq.com/<name>);
`find_boards()` discovers boards for an industry's incumbent tools.

Access (checked 2026-10-11): robots.txt allows boards-api.greenhouse.io/v1 and api.lever.co (Crawl-delay
1, applied), api.ashbyhq.com answers 401 (RFC 9309: allow). None of the three publishes terms binding
third-party readers of these public APIs; Lever's docs: published postings "may be scraped by third
parties". SmartRecruiters (robots.txt allows only LinkedInBot) and USAJOBS (robots.txt Disallow: /)
are blocked.

Rule 4: recruiters' emails and phone numbers are redacted from descriptions; no applicant data exists here.
Pay is kept in `money` when the post states it.
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from ..base import BaseConnector, HttpClient
from ..models import RawPost, SourceQuery
from ..parsing import html_to_text, parse_date
from ..privacy import redact_contacts
from ..robots import RobotsChecker
from . import register

log = logging.getLogger(__name__)

API = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true",
    "lever": "https://api.lever.co/v0/postings/{token}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true",
}
BOARD_URL = {
    "greenhouse": "https://boards.greenhouse.io/{token}",
    "lever": "https://jobs.lever.co/{token}",
    "ashby": "https://jobs.ashbyhq.com/{token}",
}
_HOSTS = {"boards.greenhouse.io": "greenhouse", "job-boards.greenhouse.io": "greenhouse",
          "jobs.lever.co": "lever", "jobs.ashbyhq.com": "ashby"}
_PAY = re.compile(r"\$\s?(\d{2,3}(?:,\d{3})+|\d+(?:\.\d+)?\s?[kK])\s*(?:-|–|—|to)\s*\$?\s?(\d{2,3}(?:,\d{3})+|\d+(?:\.\d+)?\s?[kK])")


def parse_board(url: str) -> tuple[str, str] | None:
    """('greenhouse', 'acme') from https://boards.greenhouse.io/acme; None for other URLs."""
    parts = urlsplit(url)
    ats = _HOSTS.get(parts.hostname or "")
    token = parts.path.strip("/").split("/")[0] if parts.path.strip("/") else ""
    return (ats, token) if ats and token else None


def _amount(text: str) -> int:
    text = text.replace(",", "").replace(" ", "")
    return int(float(text[:-1]) * 1000) if text[-1] in "kK" else int(float(text))


def pay_from_text(text: str) -> dict[str, Any] | None:
    m = _PAY.search(text)
    if not m:
        return None
    low, high = _amount(m.group(1)), _amount(m.group(2))
    return {"min": low, "max": high, "currency": "USD", "stated": m.group(0)} if 0 < low <= high else None


def _ms(value: Any) -> datetime | None:
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc) if isinstance(value, (int, float)) else None


@register
class JobBoardsConnector(BaseConnector):
    connector = "job_boards"
    route = "own_api"
    source_type = "paid_demand"
    default_cadence = "weekly"
    text_dedupe = False      # one company's posts share long boilerplate
    parallel_queries = 4

    async def fetch(self, query: SourceQuery) -> AsyncIterator[RawPost]:
        board = parse_board(query.params.get("board", ""))
        if not board:
            raise ValueError("job_boards query needs params.board: a Greenhouse, Lever or Ashby board URL")
        ats, token = board
        url = API[ats].format(token=token)
        await self.services.robots.require(url)
        data = await self.http.get_json(url)
        jobs = {"greenhouse": lambda d: d.get("jobs", []), "lever": lambda d: d, "ashby": lambda d: d.get("jobs", [])}[ats](data)
        company = query.params.get("company") or token
        for job in jobs[: query.limit]:
            post = getattr(self, f"_{ats}")(job, company, token, query)
            if post:
                yield post

    def _post(self, *, url: str, title: str, text: str, published: datetime | None, company: str, ats: str,
              token: str, money: dict | None, query: SourceQuery, raw: dict[str, Any]) -> RawPost | None:
        if not url or not published:
            return None
        text = redact_contacts(text)
        return self.make_post(
            url=url, title=title, text=text or title, is_snippet=False, author_hash=None,
            community=company, published_at=published, language=None, engagement={},
            money=money or pay_from_text(text), industry_hint=query.industry_hint, query=None,
            raw={"ats": ats, "board": token, **{k: v for k, v in raw.items() if v not in (None, "", [], {})}},
        )

    def _greenhouse(self, job: dict, company: str, token: str, query: SourceQuery) -> RawPost | None:
        return self._post(
            url=job.get("absolute_url"), title=job.get("title", ""),
            text=html_to_text(html.unescape(job.get("content") or "")),
            published=parse_date(job.get("first_published") or job.get("updated_at")),
            company=job.get("company_name") or company, ats="greenhouse", token=token, money=None, query=query,
            raw={"id": job.get("id"), "location": (job.get("location") or {}).get("name"),
                 "departments": [d.get("name") for d in job.get("departments", [])],
                 "updated_at": job.get("updated_at")})

    def _lever(self, job: dict, company: str, token: str, query: SourceQuery) -> RawPost | None:
        cats = job.get("categories") or {}
        lists = "\n".join(f"{li.get('text', '')}\n{html_to_text(li.get('content'))}" for li in job.get("lists", []))
        salary = job.get("salaryRange") or None
        money = ({"min": salary.get("min"), "max": salary.get("max"), "currency": salary.get("currency"),
                  "interval": salary.get("interval")} if salary else None)
        return self._post(
            url=job.get("hostedUrl"), title=job.get("text", ""),
            text="\n\n".join(p for p in (job.get("descriptionPlain"), lists, job.get("additionalPlain")) if p),
            published=_ms(job.get("createdAt")), company=company, ats="lever", token=token, money=money,
            query=query, raw={"id": job.get("id"), "location": cats.get("location"), "team": cats.get("team"),
                              "commitment": cats.get("commitment"), "workplace": job.get("workplaceType")})

    def _ashby(self, job: dict, company: str, token: str, query: SourceQuery) -> RawPost | None:
        if job.get("isListed") is False:
            return None
        comp = job.get("compensation") or {}
        summary = comp.get("compensationTierSummary") or comp.get("scrapeableCompensationSalarySummary")
        money = (pay_from_text(summary) or {}) | {"stated": summary} if summary else None
        return self._post(
            url=job.get("jobUrl"), title=job.get("title", ""), text=job.get("descriptionPlain") or "",
            published=parse_date(job.get("publishedAt")), company=company, ats="ashby", token=token,
            money=money, query=query,
            raw={"id": job.get("id"), "location": job.get("location"), "department": job.get("department"),
                 "employment": job.get("employmentType"), "remote": job.get("isRemote")})


# ---- finding boards for an industry's tools --------------------------------------------------------

@dataclass
class FoundBoard:
    company: str
    ats: str
    token: str
    jobs: int

    @property
    def url(self) -> str:
        return BOARD_URL[self.ats].format(token=self.token)


def slugs(name: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", name.lower())
    out = ["".join(words)] + (["-".join(words)] if len(words) > 1 else [])
    return [s for s in dict.fromkeys(out) if len(s) >= 3]


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


_SUFFIX = re.compile(r"^(inc|llc|ltd|co|corp|hq|app|labs?|software|technologies|technology|tech|io|ai|careers|jobs)*$")


def same_company(board_name: str, company: str) -> bool:
    """'Weave' or 'Weave Communications'? Only a corporate suffix may follow the company's name."""
    name, want = _norm(board_name), _norm(company)
    return name == want or (name.startswith(want) and bool(_SUFFIX.match(name[len(want):])))


async def find_boards(http: HttpClient, robots: RobotsChecker, company: str) -> list[FoundBoard]:
    """Try the company's name as a board on each ATS; keep boards that are clearly this company.

    Greenhouse returns the board's name, which must match. Lever and Ashby don't, so at least one
    posting must mention the company by name (a generic slug like "playground" could be anyone).
    """
    found = []
    for ats in API:
        for token in slugs(company):
            url = API[ats].format(token=token)
            try:
                if not await robots.allowed(url):
                    continue
                r = await http.get(url, attempts=2)
                if r.status_code != 200:
                    continue
                data = r.json()
            except Exception:
                continue
            jobs = data.get("jobs", []) if isinstance(data, dict) else data
            if not jobs:
                continue
            if ats == "greenhouse":
                info = await http.get(f"https://boards-api.greenhouse.io/v1/boards/{token}", attempts=2)
                name = info.json().get("name", "") if info.status_code == 200 else ""
                ok = same_company(name, company)
            else:
                texts = (str(j.get("descriptionPlain") or "") + str(j.get("additionalPlain") or "") for j in jobs[:20])
                ok = any(company.lower() in t.lower() for t in texts)
            if ok:
                found.append(FoundBoard(company, ats, token, len(jobs)))
                break
    return found
