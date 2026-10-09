"""SEC EDGAR Form D (private fundraising notices), from the daily form index. No key.

robots.txt disallows /cgi-bin/ (the current-filings feed), so this reads
/Archives/edgar/daily-index/<year>/QTR<n>/form.<YYYYMMDD>.idx for each day since the cursor,
takes Form D and D/A lines, then each filing's primary_doc.xml. SEC fair access: a User-Agent
with a contact email (SIGNAL_CONTACT_EMAIL) and at most 10 requests/second (sources.yaml uses 5).

Stored: issuer name (a company), entity type, state, industry group, offering amounts, security
types. Not stored: relatedPersonsList (executives' names) or any addresses.
Pooled investment funds are skipped by default (params.skip_industries).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator
from datetime import date, datetime, timedelta, timezone
from typing import Any

from ..base import BaseConnector
from ..models import RawPost, SourceQuery, utcnow
from . import register

ARCHIVES = "https://www.sec.gov/Archives/"
INDEX = ARCHIVES + "edgar/daily-index/{y}/QTR{q}/form.{d:%Y%m%d}.idx"
FORMS = {"D", "D/A"}
DEFAULT_SKIP = ["Pooled Investment Fund"]
_LINE = re.compile(r"^(?P<form>\S+(?: \S+)?)\s{2,}(?P<company>.+?)\s{2,}(?P<cik>\d+)\s+(?P<date>\d{8})\s+(?P<file>edgar/\S+\.txt)\s*$")


def parse_index(text: str) -> list[dict[str, str]]:
    rows = []
    for line in text.splitlines():
        m = _LINE.match(line)
        if m and m.group("form") in FORMS:
            rows.append(m.groupdict())
    return rows


def filing_folder(file: str, cik: str) -> str:
    accession = file.rsplit("/", 1)[1].removesuffix(".txt").replace("-", "")
    return f"{ARCHIVES}edgar/data/{cik}/{accession}/"


def _text(root: ET.Element, path: str) -> str | None:
    e = root.find(path)
    return e.text.strip() if e is not None and e.text else None


def _amount(value: str | None) -> float | None:
    try:
        return float(value) if value else None
    except ValueError:
        return None     # "Indefinite"


@register
class EdgarConnector(BaseConnector):
    connector = "edgar"
    route = "own_api"
    source_type = "startup"
    day_granular = True
    text_dedupe = False

    async def fetch(self, query: SourceQuery) -> AsyncIterator[RawPost]:
        start = (query.since or utcnow() - timedelta(days=3)).date()
        skip = set(query.params.get("skip_industries", DEFAULT_SKIP))
        day, today, fetched = start, utcnow().date(), 0
        while day <= today and fetched < query.limit:
            url = INDEX.format(y=day.year, q=(day.month - 1) // 3 + 1, d=day)
            await self.services.robots.require(url)
            response = await self.http.get(url)
            if response.status_code == 200:
                for row in parse_index(response.text):
                    post = await self._filing(row, skip, query)
                    if post:
                        fetched += 1
                        yield post
                        if fetched >= query.limit:
                            return
            elif response.status_code not in (403, 404):     # weekends/holidays have no index
                response.raise_for_status()
            day += timedelta(days=1)

    async def _filing(self, row: dict[str, str], skip: set[str], query: SourceQuery) -> RawPost | None:
        folder = filing_folder(row["file"], row["cik"])
        response = await self.http.get(folder + "primary_doc.xml")
        if response.status_code != 200:
            return None
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError:
            self.invalid += 1
            return None
        industry = _text(root, "offeringData/industryGroup/industryGroupType") or "Unknown"
        if industry in skip:
            return None
        name = _text(root, "primaryIssuer/entityName") or row["company"].strip()
        offered = _amount(_text(root, "offeringData/offeringSalesAmounts/totalOfferingAmount"))
        sold = _amount(_text(root, "offeringData/offeringSalesAmounts/totalAmountSold"))
        securities = [c.tag.removeprefix("is").removesuffix("Type") for c in root.findall("offeringData/typesOfSecuritiesOffered/*")
                      if (c.text or "").strip().lower() == "true"]
        amendment = row["form"] == "D/A"
        sold_txt = f"${sold:,.0f} sold" if sold is not None else "amount sold not stated"
        offered_txt = f"of ${offered:,.0f} offered" if offered is not None else "(indefinite offering)"
        filed = datetime.strptime(row["date"], "%Y%m%d").replace(tzinfo=timezone.utc)
        return self.make_post(
            url=folder,
            title=f"{name}: Form D{'/A' if amendment else ''}, {industry}",
            text=f"{name} ({_text(root, 'primaryIssuer/entityType') or 'entity'}, "
                 f"{_text(root, 'primaryIssuer/issuerAddress/stateOrCountry') or '?'}) reported {sold_txt} "
                 f"{offered_txt}. Securities: {', '.join(securities) or 'n/a'}. Industry: {industry}.",
            is_snippet=False,
            community=industry,
            published_at=filed,
            language="en",
            money={"offered_usd": offered, "sold_usd": sold},
            industry_hint=query.industry_hint,
            raw={"cik": row["cik"], "file": row["file"], "amendment": amendment, "industry_group": industry,
                 "entity_type": _text(root, "primaryIssuer/entityType"),
                 "state": _text(root, "primaryIssuer/issuerAddress/stateOrCountry"),
                 "securities": securities,
                 "investors": _text(root, "offeringData/investors/totalNumberAlreadyInvested")},
        )
