"""OSHA enforcement data (DOL data API v4, key DOL_API_KEY): what inspectors cite, per industry.

Monthly: violations issued in the 30 days ending at the newest issuance date in the data (the data
lag about a month) are joined to their inspections for the NAICS code, then summed per industry
map (NAICS prefixes in `INDUSTRY_NAICS`): inspections, violations by type, penalties, and the most
cited standards. Repeated citations of the same standard are a compliance burden somebody pays for.

Only aggregates are stored: no establishment names, addresses or inspection numbers (rule 4 spirit:
small businesses are often named after their owners).

Access (2026-10-15): apiprod.dol.gov robots.txt answers 403 (allow); dataportal.dol.gov allows all but
/static/. DOL data are public; the key is free (dataportal.dol.gov). filter_object is JSON
{field, operator, value}; `in` takes a list; up to 10,000 rows per request.
"""

from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any

from ..base import BaseConnector
from ..models import RawPost, SourceQuery
from ..parsing import parse_date
from . import register

API = "https://apiprod.dol.gov/v4/get/OSHA/{table}/json"
PAGE = 10_000
JOIN_CHUNK = 80          # the API gateway rejects URLs much past ~2,000 characters (403)
INDUSTRY_NAICS = {
    "construction": ["23"], "manufacturing": ["31", "32", "33"], "restaurants": ["722"],
    "healthcare": ["621", "622", "623"], "dental": ["6212"], "veterinary": ["54194"], "auto_repair": ["8111"],
    "logistics": ["484", "492", "493"], "agriculture": ["111", "112", "115"], "ecommerce": ["44", "45"],
    "home_services": ["5617"], "salons": ["8121"], "fitness": ["71394"], "childcare": ["6244"],
    "property_management": ["5313"], "education": ["611"], "dog_grooming": ["81291"], "events_weddings": ["7223"],
}
TYPES = {"S": "serious", "W": "willful", "R": "repeat", "O": "other-than-serious", "U": "unclassified"}
STANDARDS = {
    "1926.501": "fall protection", "1926.503": "fall protection training", "1926.451": "scaffolds",
    "1926.1053": "ladders", "1926.1052": "stairways", "1926.102": "eye and face protection",
    "1926.100": "head protection", "1926.20": "general safety and health program", "1926.21": "safety training",
    "1926.1153": "respirable crystalline silica", "1926.404": "electrical wiring design",
    "1910.1200": "hazard communication", "1910.134": "respiratory protection", "1910.147": "lockout/tagout",
    "1910.178": "powered industrial trucks (forklifts)", "1910.212": "machine guarding", "1910.303": "electrical equipment",
    "1910.305": "electrical wiring methods", "1910.22": "walking-working surfaces", "1910.23": "ladders",
    "1910.37": "exit routes", "1910.157": "fire extinguishers", "1910.1030": "bloodborne pathogens",
    "1910.132": "personal protective equipment", "1910.151": "first aid", "1910.95": "occupational noise",
    "1910.215": "abrasive wheel machinery", "1910.219": "mechanical power transmission", "1910.1048": "formaldehyde",
    "1910.1052": "methylene chloride", "1910.333": "electrical safe work practices", "1910.106": "flammable liquids",
    "1904.4": "injury and illness recording", "1904.29": "injury and illness forms", "1904.32": "annual injury summary",
    "1904.39": "reporting fatalities and severe injuries", "1903.19": "abatement verification",
    "5A.1": "General Duty Clause",
}


def standard_code(raw: str) -> str:
    """'19260501 B10' -> '1926.501'; '5A0001' -> '5A.1'."""
    code = (raw or "").split()[0]
    if len(code) >= 5 and code[:4].isdigit():
        return f"{code[:4]}.{int(code[4:]) if code[4:].isdigit() else code[4:]}"
    if code.startswith("5A"):
        return f"5A.{int(code[2:] or 0)}"
    return code


@register
class OshaConnector(BaseConnector):
    connector = "osha"
    route = "own_api"
    source_type = "government"
    default_cadence = "monthly"
    text_dedupe = False
    filter_since = False     # the window ends at the data's newest date, weeks back; urls dedupe

    async def _get(self, table: str, **params: Any) -> list[dict[str, Any]]:
        if "filter_object" in params:
            params["filter_object"] = json.dumps(params["filter_object"])
        with self.services.costs.metered(self.name, units=1, note=table):
            response = await self.http.get(API.format(table=table), params={**params, "X-API-KEY": self.key},
                                           timeout=120)
        if response.status_code == 204:
            return []
        if response.status_code != 200:
            raise RuntimeError(f"DOL API {table} HTTP {response.status_code}: {response.text[:160]}")
        return response.json().get("data", [])

    async def fetch(self, query: SourceQuery) -> AsyncIterator[RawPost]:
        self.key = os.environ.get("DOL_API_KEY")
        if not self.key:
            raise RuntimeError("DOL_API_KEY is not set")
        await self.services.robots.require(API.format(table="violation"))
        newest = await self._get("violation", limit=1, sort_by="issuance_date", sort="desc", fields="issuance_date")
        end = parse_date(newest[0]["issuance_date"]) if newest else None
        if end is None:
            return
        days = int(query.params.get("days", 30))
        start = end - timedelta(days=days)
        violations: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = await self._get("violation", limit=PAGE, offset=offset,
                                   fields="activity_nr,standard,viol_type,initial_penalty,current_penalty,delete_flag",
                                   filter_object={"field": "issuance_date", "operator": "gt", "value": f"{start:%Y-%m-%d}"})
            violations += [v for v in page if v.get("delete_flag") != "X"]
            if len(page) < PAGE:
                break
            offset += PAGE
        ids = sorted({v["activity_nr"] for v in violations})
        naics: dict[int, str] = {}
        for i in range(0, len(ids), JOIN_CHUNK):
            for row in await self._get("inspection", fields="activity_nr,naics_code",
                                       filter_object={"field": "activity_nr", "operator": "in", "value": ids[i:i + JOIN_CHUNK]}):
                naics[row["activity_nr"]] = str(row.get("naics_code") or "")
        for key, prefixes in INDUSTRY_NAICS.items():
            mine = [v for v in violations if naics.get(v["activity_nr"], "").startswith(tuple(prefixes))]
            if mine:
                yield self._to_post(key, prefixes, mine, start, end, query)

    def _to_post(self, key: str, prefixes: list[str], viols: list[dict[str, Any]], start: datetime, end: datetime,
                 query: SourceQuery) -> RawPost:
        inspections = len({v["activity_nr"] for v in viols})
        types = Counter(TYPES.get(v.get("viol_type"), "unclassified") for v in viols)
        initial = sum(float(v.get("initial_penalty") or 0) for v in viols)
        current = sum(float(v.get("current_penalty") or 0) for v in viols)
        per_standard: dict[str, int] = defaultdict(int)
        for v in viols:
            per_standard[standard_code(v.get("standard"))] += 1
        top = sorted(per_standard.items(), key=lambda kv: -kv[1])[:5]
        cited = "; ".join(f"{code}{f' {STANDARDS[code]}' if code in STANDARDS else ''} ({n})" for code, n in top)
        kinds = ", ".join(f"{n} {t}" for t, n in types.most_common())
        naics = "/".join(prefixes)
        text = (f"OSHA citations issued {start:%b %d} to {end:%b %d, %Y} in NAICS {naics}: {len(viols)} violations "
                f"({kinds}) from {inspections} inspections; initial penalties ${initial:,.0f}, current ${current:,.0f}. "
                f"Most cited standards: {cited}.")
        return self.make_post(
            url=f"https://www.osha.gov/data?industry={key}&through={end:%Y-%m-%d}",
            title=f"OSHA citations, {key.replace('_', ' ')} (NAICS {naics}), 30 days to {end:%b %d, %Y}",
            text=text, is_snippet=False, community="OSHA enforcement data", published_at=end, language="en",
            engagement={}, money={"initial_penalties_usd": round(initial), "current_penalties_usd": round(current)},
            industry_hint=key, query=None,
            raw={"naics": prefixes, "window_start": f"{start:%Y-%m-%d}", "window_end": f"{end:%Y-%m-%d}",
                 "violations": len(viols), "inspections": inspections, "by_type": dict(types),
                 "top_standards": [{"standard": c, "name": STANDARDS.get(c), "count": n} for c, n in top]},
        )
