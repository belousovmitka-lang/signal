from datetime import datetime, timezone

import httpx

from signal_connectors.config import SourcesFile
from signal_connectors.connectors.edgar import filing_folder, parse_index
from signal_connectors.runner import run_source
from signal_connectors.services import Services

from .conftest import signal_vcr

INDEX = """Form Type   Company Name                                                  CIK         Date Filed  File Name
---------------------------------------------------------------------------------------------------------------------------------------------
1-A              Cahero Nytra Holding LLC                                      2158856     20261008    edgar/data/2158856/0002158856-26-000001.txt
D                Docketly Inc                                                  1993097     20261008    edgar/data/1993097/0001993097-26-000001.txt
D/A              Some Fund LP                                                  2158743     20261008    edgar/data/2158743/0000919574-26-006748.txt
"""

FORM_D = """<?xml version="1.0"?><edgarSubmission><primaryIssuer><cik>1993097</cik><entityName>Docketly Inc</entityName>
<issuerAddress><street1>1 Main St</street1><stateOrCountry>DE</stateOrCountry></issuerAddress><entityType>Corporation</entityType></primaryIssuer>
<relatedPersonsList><relatedPersonInfo><relatedPersonName><firstName>Jane</firstName><lastName>Founder</lastName></relatedPersonName></relatedPersonInfo></relatedPersonsList>
<offeringData><industryGroup><industryGroupType>Other Technology</industryGroupType></industryGroup>
<typesOfSecuritiesOffered><isEquityType>true</isEquityType><isDebtType>false</isDebtType></typesOfSecuritiesOffered>
<offeringSalesAmounts><totalOfferingAmount>2500000</totalOfferingAmount><totalAmountSold>1200000</totalAmountSold></offeringSalesAmounts>
<investors><totalNumberAlreadyInvested>7</totalNumberAlreadyInvested></investors></offeringData></edgarSubmission>"""

FUND = FORM_D.replace("Other Technology", "Pooled Investment Fund").replace("Docketly Inc", "Some Fund LP")


def test_parse_index_and_folder():
    rows = parse_index(INDEX)
    assert [(r["form"], r["cik"]) for r in rows] == [("D", "1993097"), ("D/A", "2158743")]
    assert filing_folder(rows[0]["file"], "1993097") == \
        "https://www.sec.gov/Archives/edgar/data/1993097/000199309726000001/"


async def test_days_filings_and_privacy(make_services):
    seen = []

    def handler(request):
        path = request.url.path
        seen.append(path)
        if path == "/robots.txt":
            return httpx.Response(404)
        if path.endswith("form.20261008.idx"):
            return httpx.Response(200, text=INDEX)
        if path.endswith(".idx"):
            return httpx.Response(404)          # weekend / not yet published
        if "1993097" in path:
            return httpx.Response(200, text=FORM_D)
        return httpx.Response(200, text=FUND)

    cfg = {"sources": {"edgar": {"route": "own_api", "source_type": "startup", "queries": [{"limit": 50}]}}}
    since = datetime(2026, 10, 7, tzinfo=timezone.utc)
    async with make_services(cfg, handler) as svc:
        out = await run_source(svc, svc.sources.get("edgar"), since=since, collect=True)
    (p,) = out.posts                              # the pooled fund is skipped
    assert p.title == "Docketly Inc: Form D, Other Technology"
    assert p.money == {"offered_usd": 2500000.0, "sold_usd": 1200000.0} and p.raw["securities"] == ["Equity"]
    assert "Jane" not in p.model_dump_json() and "Main St" not in p.model_dump_json()
    assert any(s.endswith("form.20261007.idx") for s in seen) and any(s.endswith("form.20261008.idx") for s in seen)


@signal_vcr.use_cassette("edgar.yaml")
async def test_cassette_edgar(settings):
    sources = SourcesFile.model_validate({"sources": {"edgar": {
        "route": "own_api", "source_type": "startup", "queries": [{"limit": 20}],
        "rate_limit": {"requests_per_second": 5, "domains": ["www.sec.gov"]}}}})
    async with Services.open(settings, sources) as svc:
        out = await run_source(svc, svc.sources.get("edgar"), since=datetime(2026, 10, 8, tzinfo=timezone.utc),
                               collect=True)
    assert out.result.status == "ok" and out.result.fetched == 20 and out.result.invalid == 0
    assert all(p.url.startswith("https://www.sec.gov/Archives/edgar/data/") and "Form D" in p.title for p in out.posts)
    assert all(p.raw["industry_group"] != "Pooled Investment Fund" for p in out.posts)
