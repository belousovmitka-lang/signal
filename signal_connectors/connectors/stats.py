"""Government statistics. Census Business Formation Statistics (key CENSUS_API_KEY): new business
applications per month by NAICS sector, US level, seasonally adjusted.

One call per run (/data/timeseries/eits/bfs, last 14 months) gives every sector's business
applications (BA_BA) and high-propensity applications (BA_HBA, likely employers). Each sector's
newest `months` months become one record each, with month-over-month and year-over-year change:
a rising count of new restaurants or construction firms is a market of new buyers. In some sectors
(e.g. NAICS 72) Census counts every application as high-propensity, so the two numbers match there.

Census API terms (read 2026-10-15): commercial use allowed; the app must show "This product uses the
Census Bureau Data API but is not endorsed or certified by the Census Bureau." (raw.notice). The data
are national aggregates, so nothing identifies a person or business. robots.txt answers our bot with a
firewall page with no rules (allow).
"""

from __future__ import annotations

import os
from collections import defaultdict
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from ..base import BaseConnector
from ..models import RawPost, SourceQuery, utcnow
from . import register

API = "https://api.census.gov/data/timeseries/eits/bfs"
PAGE = "https://www.census.gov/econ/bfs/index.html"
NOTICE = "This product uses the Census Bureau Data API but is not endorsed or certified by the Census Bureau."
SECTORS = {
    "TOTAL": ("All industries", None), "NAICS11": ("Agriculture, forestry, fishing and hunting", "agriculture"),
    "NAICS21": ("Mining, quarrying, and oil and gas extraction", None), "NAICS22": ("Utilities", None),
    "NAICS23": ("Construction", "construction"), "NAICSMNF": ("Manufacturing", "manufacturing"),
    "NAICS42": ("Wholesale trade", None), "NAICSRET": ("Retail trade", "ecommerce"),
    "NAICSTW": ("Transportation and warehousing", "logistics"), "NAICS51": ("Information", "it_devtools"),
    "NAICS52": ("Finance and insurance", "insurance"), "NAICS53": ("Real estate and rental and leasing", "real_estate"),
    "NAICS54": ("Professional, scientific, and technical services", None),
    "NAICS55": ("Management of companies and enterprises", None),
    "NAICS56": ("Administrative and support and waste management services", None),
    "NAICS61": ("Educational services", "education"), "NAICS62": ("Health care and social assistance", "healthcare"),
    "NAICS71": ("Arts, entertainment, and recreation", "fitness"),
    "NAICS72": ("Accommodation and food services", "restaurants"),
    "NAICS81": ("Other services (repair, personal care, nonprofits)", None),
}


def _month(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m").replace(tzinfo=timezone.utc)


def shift(month: str, by: int) -> str:
    """'2026-01' shifted by -1 -> '2025-12'."""
    y, m = map(int, month.split("-"))
    total = y * 12 + (m - 1) + by
    return f"{total // 12}-{total % 12 + 1:02d}"


def _change(now: int, before: int | None) -> str:
    if not before:
        return "n/a"
    pct = (now - before) / before * 100
    return f"{pct:+.1f}%"


@register
class CensusBfsConnector(BaseConnector):
    connector = "stats"
    route = "own_api"
    source_type = "research"
    default_cadence = "monthly"
    text_dedupe = False      # sector records share their wording
    filter_since = False     # months are published weeks after they end; urls dedupe

    async def fetch(self, query: SourceQuery) -> AsyncIterator[RawPost]:
        key = os.environ.get("CENSUS_API_KEY")
        if not key:
            raise RuntimeError("CENSUS_API_KEY is not set")
        now = utcnow()
        start = shift(f"{now:%Y-%m}", -14)
        await self.services.robots.require(API)
        with self.services.costs.metered(self.name, units=1, note="bfs"):
            response = await self.http.get(API, params={
                "get": "cell_value,data_type_code,category_code,seasonally_adj,time_slot_id",
                "for": "us:*", "time": f"from {start}", "key": key}, timeout=60)
        if response.status_code != 200:
            raise RuntimeError(f"Census BFS HTTP {response.status_code}: {response.text[:160]}")
        rows = response.json()
        head, rows = rows[0], rows[1:]
        col = {name: i for i, name in enumerate(head)}
        series: dict[tuple[str, str], dict[str, int]] = defaultdict(dict)   # (sector, type) -> {month: value}
        for r in rows:
            if r[col["seasonally_adj"]] != "yes" or r[col["data_type_code"]] not in ("BA_BA", "BA_HBA"):
                continue
            try:
                series[(r[col["category_code"]], r[col["data_type_code"]])][r[col["time"]]] = int(r[col["cell_value"]])
            except ValueError:
                continue                     # suppressed or missing cell
        months = int(query.params.get("months", 2))
        for code, (label, industry) in SECTORS.items():
            ba = series.get((code, "BA_BA"), {})
            hba = series.get((code, "BA_HBA"), {})
            for month in sorted(ba)[-months:]:
                yield self._to_post(code, label, industry, month, ba, hba, query)

    def _to_post(self, code: str, label: str, industry: str | None, month: str, ba: dict[str, int],
                 hba: dict[str, int], query: SourceQuery) -> RawPost:
        when = _month(month)
        prev, last_year = shift(month, -1), shift(month, -12)
        name = when.strftime("%B %Y")
        text = (f"Business applications in {label} ({code.removeprefix('NAICS') if code.startswith('NAICS') else code}), "
                f"United States, {name}: {ba[month]:,} seasonally adjusted ({_change(ba[month], ba.get(prev))} vs the "
                f"previous month, {_change(ba[month], ba.get(last_year))} vs a year earlier).")
        if month in hba:
            text += (f" High-propensity applications (likely employers): {hba[month]:,} "
                     f"({_change(hba[month], hba.get(prev))} m/m, {_change(hba[month], hba.get(last_year))} y/y).")
        return self.make_post(
            url=f"{PAGE}?sector={code}&month={month}",
            title=f"New business applications, {label}: {name}",
            text=text, is_snippet=False, community="Census Business Formation Statistics",
            published_at=when, language="en", engagement={},
            industry_hint=industry or query.industry_hint, query=None,
            raw={"sector": code, "month": month, "applications": ba[month], "high_propensity": hba.get(month),
                 "previous_month": ba.get(prev), "year_earlier": ba.get(last_year), "notice": NOTICE},
        )
