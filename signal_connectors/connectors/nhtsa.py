"""NHTSA vehicle safety complaints (ODI flat files, no key, US government work).

The complaints API only looks up by make + model + year or by ODI number, so new complaints come from
the bulk file NHTSA rebuilds daily: COMPLAINTS_RECEIVED_<y>-<y>.zip, 5-year chunks by received date, the newest ending at the current year
(~43 MB zipped, ~210,000 rows for 2025-2026; ~10,000 complaints a month). Layout: CMPL.txt next to it
(51 tab-separated fields, read 2026-10-28). One complaint (ODINO) has one row per component; rows are
merged into one post with every component listed.

Stored: make, model, year, components, the owner's narrative (contacts redacted), crash/fire/injury
counts, mileage, received date, state. Not stored: VIN, city, dealer name/phone/address, the vehicle
operator's name (field 51). Each post links to the API record for its ODI number.

Access (2026-10-28): static.nhtsa.gov serves no robots.txt (404: allowed); api.nhtsa.gov likewise.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import AsyncIterator, Iterator
from datetime import date, datetime, timezone
from typing import Any

from ..base import BaseConnector
from ..models import RawPost, SourceQuery
from ..privacy import redact_contacts
from . import register

BASE = "https://static.nhtsa.gov/odi/ffdd/cmpl/"
RECORD = "https://api.nhtsa.gov/complaints/odinumber?odinumber={}"
FIELDS = 51
# 1-based field numbers from CMPL.txt
ODINO, MFR, MAKE, MODEL, YEAR, CRASH, FAILDATE, FIRE, INJURED, DEATHS, COMPDESC = 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12
STATE, LDATE, MILES, CDESCR, CMPL_TYPE, TOWED, PROD_TYPE = 14, 17, 18, 20, 21, 49, 46
PRODUCTS = {"V": "vehicle", "T": "tires", "E": "equipment", "C": "child restraint"}


def chunk_name(year: int, current_year: int) -> str:
    """The 5-year file holding complaints received in `year` (1995-1999, ..., 2020-2024, then 2025 to the
    current year: COMPLAINTS_RECEIVED_2025-2026.zip in 2026)."""
    start = year - (year - 1995) % 5
    return f"COMPLAINTS_RECEIVED_{start}-{min(start + 4, current_year)}.zip"


def _ymd(value: str) -> date | None:
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        return None


def _field_reader(row: list[str]):
    return lambda n: row[n - 1].strip() if n <= len(row) else ""


def _int(value: str) -> int:
    return int(value) if value.isdigit() else 0


def parse_rows(data: bytes) -> Iterator[list[str]]:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():
            with z.open(name) as f:
                for line in f:
                    row = line.decode("utf-8", errors="replace").rstrip("\r\n").split("\t")
                    if len(row) >= FIELDS - 2:          # fields 50-51 were added in 2026
                        yield row


@register
class NhtsaConnector(BaseConnector):
    connector = "nhtsa"
    route = "own_api"
    source_type = "government"
    default_cadence = "weekly"
    day_granular = True
    text_dedupe = False

    async def fetch(self, query: SourceQuery) -> AsyncIterator[RawPost]:
        since = query.since.date() if query.since else None
        today = datetime.now(timezone.utc).date()
        names = {chunk_name(today.year, today.year), chunk_name((since or today).year, today.year)}
        complaints: dict[str, dict[str, Any]] = {}
        for name in sorted(names):
            url = BASE + name
            await self.services.robots.require(url)
            response = await self.http.get(url, timeout=300)
            response.raise_for_status()
            for row in parse_rows(response.content):
                f = _field_reader(row)
                received = _ymd(f(LDATE))
                if not received or (since and received < since) or not f(ODINO):
                    continue
                c = complaints.setdefault(f(ODINO), {"row": f, "received": received, "components": []})
                if f(COMPDESC) and f(COMPDESC) not in c["components"]:
                    c["components"].append(f(COMPDESC))
        newest = sorted(complaints.values(), key=lambda c: (c["received"], c["row"](ODINO)), reverse=True)
        for c in newest[:query.limit]:
            if post := self._to_post(c, query):
                yield post

    def _to_post(self, c: dict[str, Any], query: SourceQuery) -> RawPost | None:
        f = c["row"]
        vehicle = " ".join(x for x in (f(YEAR) if f(YEAR) != "9999" else "", f(MAKE), f(MODEL)) if x)
        components = ", ".join(c["components"]) or "unspecified component"
        flags = [label for flag, label in ((CRASH, "crash"), (FIRE, "fire"), (TOWED, "towed")) if f(flag) == "Y"]
        injured, deaths = _int(f(INJURED)), _int(f(DEATHS))
        facts = ", ".join(flags + ([f"{injured} injured"] if injured else []) + ([f"{deaths} killed"] if deaths else []))
        text = "\n".join(x for x in (
            redact_contacts(f(CDESCR)),
            f"Vehicle: {vehicle}. Components: {components}." + (f" Mileage {f(MILES)}." if f(MILES) not in ("", "0") else ""),
            f"Outcome: {facts}." if facts else "") if x)
        received = c["received"]
        return self.make_post(
            url=RECORD.format(f(ODINO)),
            title=f"{components.split(',')[0].title()}: {vehicle or f(MFR) or 'vehicle'}",
            text=text,
            is_snippet=False,
            community=f(MAKE) or f(MFR) or None,
            published_at=datetime(received.year, received.month, received.day, tzinfo=timezone.utc),
            language="en",
            industry_hint=query.industry_hint,
            raw={"odi_number": f(ODINO), "manufacturer": f(MFR), "make": f(MAKE), "model": f(MODEL),
                 "year": f(YEAR), "components": c["components"], "crash": f(CRASH) == "Y", "fire": f(FIRE) == "Y",
                 "injured": injured, "deaths": deaths, "miles": _int(f(MILES)), "state": f(STATE) or None,
                 "failed_on": f(FAILDATE) or None, "complaint_type": f(CMPL_TYPE) or None,
                 "product": PRODUCTS.get(f(PROD_TYPE), f(PROD_TYPE) or None)},
        )
