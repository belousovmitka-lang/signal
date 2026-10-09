from datetime import datetime, timezone

import httpx

from signal_connectors.config import SourcesFile
from signal_connectors.runner import run_source
from signal_connectors.services import Services

from .conftest import signal_vcr

SINCE = datetime(2026, 10, 2, tzinfo=timezone.utc)


def report(key, **kw):
    r = {"mdr_report_key": str(key), "report_number": f"R{key}", "event_type": "Malfunction",
         "date_received": "20260831", "product_problems": ["Battery Problem"],
         "manufacturer_contact_f_name": "Jane", "manufacturer_contact_phone_number": "5551234",
         "device": [{"generic_name": "INFUSION PUMP", "brand_name": "FlowMax", "manufacturer_d_name": "Acme Medical"}],
         "mdr_text": [{"text_type_code": "Description of Event or Problem", "text": "Pump alarmed and stopped."}]}
    r.update(kw)
    return r


def src(**q):
    return {"sources": {"openfda": {"route": "own_api", "source_type": "government", "queries": [q]}}}


async def test_lag_window_paging_and_no_contacts(make_services):
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        if request.url.params["skip"] == "100":
            return httpx.Response(404, json={"error": {"code": "NOT_FOUND", "message": "No matches found!"}})
        return httpx.Response(200, json={"results": [report(i) for i in range(100)]})

    async with make_services(src(limit=500), handler) as svc:
        out = await run_source(svc, svc.sources.get("openfda"), since=SINCE, collect=True)
    assert out.result.status == "ok" and out.result.new == 100      # all dated before since, kept on purpose
    assert seen[0]["search"].startswith("date_received:[20260803 TO 20991231]") and "_exists_:mdr_text" in seen[0]["search"]
    assert seen[0]["sort"] == "date_received:desc" and seen[1]["skip"] == "100"
    p = out.posts[0]
    assert p.title == "Malfunction: Infusion Pump (FlowMax)" and p.community == "Acme Medical"
    assert p.text == "Pump alarmed and stopped.\n\nProduct problems: Battery Problem"
    assert "Jane" not in p.model_dump_json() and "5551234" not in p.model_dump_json()
    assert p.industry_hint == "healthcare"


@signal_vcr.use_cassette("openfda.yaml")
async def test_cassette_openfda(settings):
    sources = SourcesFile.model_validate(src(limit=40))
    async with Services.open(settings, sources) as svc:
        out = await run_source(svc, svc.sources.get("openfda"), since=SINCE, collect=True)
    assert out.result.status == "ok" and out.result.invalid == 0 and out.result.fetched == 40
    assert all(p.text and p.title and p.url.startswith("https://www.accessdata.fda.gov/") for p in out.posts)
