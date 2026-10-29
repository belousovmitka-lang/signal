import io
import zipfile
from datetime import datetime, timezone

import httpx

from signal_connectors.connectors.nhtsa import chunk_name
from signal_connectors.runner import run_source

SINCE = datetime(2026, 10, 1, tzinfo=timezone.utc)


def row(odi, component, received="20261020", text="Engine stalls at highway speed. Call me 555-123-4567.", over=None):
    f = [""] * 51
    values = {1: "1", 2: odi, 3: "Ford Motor Company", 4: "FORD", 5: "F-150", 6: "2024", 7: "N", 8: "20261015",
              9: "N", 10: "1", 11: "0", 12: component, 13: "Springfield", 14: "OH", 15: "1FTFW1E5XRF",
              17: received, 18: "41000", 20: text, 21: "IVOQ", 41: "Bob's Ford", 42: "555-000-1111",
              46: "V", 49: "Y", 51: "Jane Driver", **(over or {})}
    for n, v in values.items():
        f[n - 1] = v
    return "\t".join(f)


def zipped(lines):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("COMPLAINTS_RECEIVED_2025-2026.txt", "\r\n".join(lines) + "\r\n")
    return buf.getvalue()


def test_chunk_names():
    assert chunk_name(2026, 2026) == "COMPLAINTS_RECEIVED_2025-2026.zip"     # the live file name, 2026-10-28
    assert chunk_name(2025, 2027) == "COMPLAINTS_RECEIVED_2025-2027.zip"
    assert chunk_name(2024, 2026) == "COMPLAINTS_RECEIVED_2020-2024.zip"


async def test_merges_components_drops_personal_fields_and_old_rows(make_services):
    data = zipped([row("11700001", "ENGINE"), row("11700001", "FUEL SYSTEM, GASOLINE"),
                   row("11700002", "SERVICE BRAKES", received="20261021", text="Brakes grind.", over={7: "Y", 6: "9999"}),
                   row("11600000", "ENGINE", received="20260901")])
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, content=data)

    sources = {"sources": {"nhtsa": {"route": "own_api", "source_type": "government",
                                     "queries": [{"industry_hint": "auto_repair", "limit": 100}]}}}
    async with make_services(sources, handler) as svc:
        out = await run_source(svc, svc.sources.get("nhtsa"), since=SINCE, collect=True)
    assert out.result.status == "ok" and out.result.new == 2           # September row is before since
    assert any(p.startswith("/odi/ffdd/cmpl/COMPLAINTS_RECEIVED_2025-") for p in paths)
    brakes, engine = out.posts
    assert brakes.title == "Service Brakes: FORD F-150" and "crash" in brakes.text and brakes.raw["year"] == "9999"
    assert engine.raw["components"] == ["ENGINE", "FUEL SYSTEM, GASOLINE"] and engine.raw["injured"] == 1
    assert engine.url == "https://api.nhtsa.gov/complaints/odinumber?odinumber=11700001"
    assert engine.industry_hint == "auto_repair" and engine.published_at.date().isoformat() == "2026-10-20"
    dumped = engine.model_dump_json()
    for personal in ("555-123-4567", "1FTFW1E5XRF", "Springfield", "Bob's Ford", "555-000-1111", "Jane Driver"):
        assert personal not in dumped
    assert "[phone]" in engine.text and "towed" in engine.text and "Mileage 41000" in engine.text
