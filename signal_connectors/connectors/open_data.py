"""G9: open data portals. Socrata (SODA API) rows and CKAN new-dataset listings, no key.

Socrata query (one dataset per query; only the fields named here are ever stored, so personal
columns such as applicant names or street addresses never reach the database):

    params:
      kind: socrata
      domain: data.cityofnewyork.us
      dataset: 43nn-pn8j
      date_field: inspection_date
      id_fields: [camis, inspection_date, violation_code]   # together unique per row
      title: "{dba}: {violation_description}"
      text: "{inspection_type}. {action}"
      community: "{boro}"
      keep: [cuisine_description, critical_flag, grade]
      where: "critical_flag = 'Critical'"                  # optional SoQL

CKAN query: {params: {kind: ckan, base: https://open.canada.ca/data}, keywords: [permits]}
(catalog.data.gov's CKAN API returns 404 since its 2026 move; checked 2026-10-09).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlencode

from ..base import BaseConnector
from ..models import RawPost, SourceQuery
from ..parsing import html_to_text, parse_date
from . import register

PAGE = 500


def render(template: str | None, row: dict[str, Any]) -> str:
    if not template:
        return ""
    values: defaultdict[str, str] = defaultdict(str, {k: str(v).strip() for k, v in row.items() if v is not None})
    return " ".join(template.format_map(values).split()).strip(" :.-") or ""


@register
class OpenDataConnector(BaseConnector):
    connector = "open_data"
    route = "own_api"
    source_type = "government"
    day_granular = True       # many datasets carry dates without times
    text_dedupe = False       # rows repeat text (same violation, same permit type)

    async def fetch(self, query: SourceQuery) -> AsyncIterator[RawPost]:
        kind = query.params.get("kind", "socrata")
        gen = self._socrata(query) if kind == "socrata" else self._ckan(query) if kind == "ckan" else None
        if gen is None:
            raise ValueError(f"unknown open_data kind {kind!r}")
        async for post in gen:
            yield post

    # --- Socrata -------------------------------------------------------------------

    async def _socrata(self, query: SourceQuery) -> AsyncIterator[RawPost]:
        p = query.params
        for key in ("domain", "dataset", "date_field", "id_fields", "title"):
            if not p.get(key):
                raise ValueError(f"socrata query needs params.{key}")
        url = f"https://{p['domain']}/resource/{p['dataset']}.json"
        where = [f"{p['date_field']} >= '{query.since:%Y-%m-%dT00:00:00}'"] if query.since else []
        if p.get("where"):
            where.append(f"({p['where']})")
        offset, fetched = 0, 0
        while fetched < query.limit:
            await self.services.robots.require(url)
            params: dict[str, Any] = {"$limit": min(PAGE, query.limit - fetched), "$offset": offset,
                                      "$order": f"{p['date_field']} DESC"}
            if where:
                params["$where"] = " AND ".join(where)
            rows = await self.http.get_json(url, params=params)
            for row in rows:
                post = self._row_to_post(row, url, query)
                if post:
                    fetched += 1
                    yield post
            if len(rows) < params["$limit"]:
                return
            offset += len(rows)

    def _row_to_post(self, row: dict[str, Any], url: str, query: SourceQuery) -> RawPost | None:
        p = query.params
        ids = {f: row.get(f) for f in p["id_fields"]}
        if any(v in (None, "") for v in ids.values()):
            return None
        return self.make_post(
            source=f"open_data:{p['domain']}:{p['dataset']}",
            url=f"{url}?{urlencode(ids)}",          # the API URL that returns exactly this row
            title=render(p["title"], row) or None,
            text=render(p.get("text"), row),
            is_snippet=False,
            community=render(p.get("community"), row) or p.get("name"),
            published_at=parse_date(row.get(p["date_field"])),
            language="en",
            industry_hint=query.industry_hint,
            raw={"dataset": p["dataset"], "domain": p["domain"],
                 **{k: row.get(k) for k in p.get("keep", []) if row.get(k) is not None}},
        )

    # --- CKAN ----------------------------------------------------------------------

    async def _ckan(self, query: SourceQuery) -> AsyncIterator[RawPost]:
        base = query.params.get("base", "").rstrip("/")
        if not base:
            raise ValueError("ckan query needs params.base")
        api = f"{base}/api/3/action/package_search"
        fq = f"metadata_created:[{query.since:%Y-%m-%dT%H:%M:%SZ} TO NOW]" if query.since else ""
        start, fetched = 0, 0
        while fetched < query.limit:
            await self.services.robots.require(api)
            data = await self.http.get_json(api, params={"q": query.query_text or "*:*", "fq": fq,
                                                         "sort": "metadata_created desc",
                                                         "rows": min(100, query.limit - fetched), "start": start})
            results = (data.get("result") or {}).get("results", [])
            for ds in results:
                post = self.make_post(
                    source=f"open_data:{base.split('//', 1)[-1]}",
                    url=f"{base}/dataset/{ds['name']}",
                    title=ds.get("title"),
                    text=html_to_text(ds.get("notes"))[:4000],
                    is_snippet=False,
                    community=(ds.get("organization") or {}).get("title"),
                    published_at=parse_date(ds.get("metadata_created")),
                    language=None,
                    industry_hint=query.industry_hint,
                    query=query.query_text,
                    raw={"ckan_id": ds.get("id"), "tags": [t.get("name") for t in ds.get("tags", [])][:20],
                         "num_resources": ds.get("num_resources")},
                )
                if post:
                    fetched += 1
                    yield post
            if len(results) < 1 or start + len(results) >= (data.get("result") or {}).get("count", 0):
                return
            start += len(results)
