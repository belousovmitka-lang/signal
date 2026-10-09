"""Search demand from Semrush (paid; flag off: ENABLE_KEYWORDS_PAID).

GET https://api.semrush.com/?type=phrase_questions|phrase_related&key=...&phrase=...&database=us
&export_columns=Ph,Nq,Cp,Co,Kd&display_limit=N -> semicolon-separated CSV. 40 API units per
line (checked 2026-10-09). Every call is metered in units, plus dollars at
options.usd_per_1000_units. Each keyword is one record linking to Semrush's keyword overview;
the API license decides what may be stored: confirm before enabling.

sources.yaml query: {keywords: ["legal billing software"], params: {report: phrase_questions, database: us}}
"""

from __future__ import annotations

import csv
import io
import os
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import quote

from ..base import BaseConnector
from ..models import HealthStatus, RawPost, SourceQuery, utcnow
from . import register

API = "https://api.semrush.com/"
OVERVIEW = "https://www.semrush.com/analytics/keywordoverview/?q={q}&db={db}"
UNITS_PER_LINE = 40
COLUMNS = "Ph,Nq,Cp,Co,Kd"


def to_number(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


@register
class KeywordsPaidConnector(BaseConnector):
    connector = "keywords_paid"
    route = "own_api"
    source_type = "research"
    filter_since = False     # keyword data is a snapshot, not dated posts
    text_dedupe = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.key = os.environ.get("SEMRUSH_KEY") or None
        self.usd_per_unit = float(self.source.options.get("usd_per_1000_units", 0)) / 1000

    async def healthcheck(self) -> HealthStatus:
        status = await super().healthcheck()
        status.auth_ok = status.ok = bool(self.key)
        status.message = None if self.key else "SEMRUSH_KEY is not set"
        return status

    async def fetch(self, query: SourceQuery) -> AsyncIterator[RawPost]:
        if not self.key:
            raise RuntimeError("SEMRUSH_KEY is not set")
        report = query.params.get("report", "phrase_questions")
        db = query.params.get("database", "us")
        for phrase in query.keywords:
            units = UNITS_PER_LINE * query.limit
            with self.services.costs.metered(self.name, usd=units * self.usd_per_unit, units=units, note=report):
                response = await self.http.get(API, params={"type": report, "key": self.key, "phrase": phrase,
                                                            "database": db, "export_columns": COLUMNS,
                                                            "display_limit": query.limit})
            body = response.text
            if response.status_code != 200 or body.startswith("ERROR"):
                if "NOTHING FOUND" in body:
                    continue
                raise RuntimeError(f"Semrush: {body[:120]}")
            for row in csv.DictReader(io.StringIO(body), delimiter=";"):
                post = self._to_post(row, phrase, report, db, query)
                if post:
                    yield post

    def _to_post(self, row: dict[str, str], phrase: str, report: str, db: str, query: SourceQuery) -> RawPost | None:
        keyword = (row.get("Keyword") or "").strip()
        if not keyword:
            return None
        volume, cpc = to_number(row.get("Search Volume")), to_number(row.get("CPC"))
        competition, difficulty = to_number(row.get("Competition")), to_number(row.get("Keyword Difficulty Index"))
        return self.make_post(
            source=f"keywords_paid:{report}",
            url=OVERVIEW.format(q=quote(keyword), db=db),
            title=keyword,
            text=f"{int(volume or 0)} searches/month; CPC ${cpc or 0:.2f}; competition {competition or 0:.2f}; "
                 f"difficulty {difficulty or 0:.0f}. Related to: {phrase}",
            is_snippet=False,
            community=f"Semrush {db}",
            published_at=utcnow(),
            language="en" if db in {"us", "uk", "ca", "au"} else None,
            engagement={"search_volume": int(volume or 0)},
            money={"cpc_usd": cpc} if cpc is not None else None,
            industry_hint=query.industry_hint,
            query=phrase,
            raw={"report": report, "database": db, "competition": competition, "difficulty": difficulty},
        )
