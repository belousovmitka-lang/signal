"""Wikipedia trending articles from the Wikimedia pageviews API (wikimedia.org, no key).

/api/rest_v1/metrics/pageviews/top/<project>/all-access/<yyyy>/<mm>/<dd> -> the day's top 1,000
articles. Each article is stored once, the first day it shows up in the top N (an emerging-
topic signal). Wikimedia's User-Agent policy needs real contact info (SIGNAL_CONTACT_EMAIL).
en.wikipedia.org's own /api/ is robots-disallowed and isn't used.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

from ..base import BaseConnector
from ..models import RawPost, SourceQuery, utcnow
from . import register

API = "https://wikimedia.org/api/rest_v1/metrics/pageviews/top/{project}/all-access/{d:%Y}/{d:%m}/{d:%d}"
SKIP_PREFIXES = ("Special:", "Wikipedia:", "Portal:", "File:", "Help:", "Talk:", "User:", "Template:", "Category:")
SKIP = {"Main_Page", "-"}


@register
class WikipediaConnector(BaseConnector):
    connector = "wikipedia"
    route = "own_api"
    source_type = "research"
    day_granular = True
    text_dedupe = False

    async def fetch(self, query: SourceQuery) -> AsyncIterator[RawPost]:
        project = query.params.get("project", "en.wikipedia")
        top = int(query.params.get("top", 100))
        # Pageviews for a day are published the next day; read every complete day since the cursor.
        day = (query.since or utcnow() - timedelta(days=2)).date()
        last = (utcnow() - timedelta(days=1)).date()
        fetched = 0
        while day <= last and fetched < query.limit:
            url = API.format(project=project, d=day)
            await self.services.robots.require(url)
            response = await self.http.get(url)
            if response.status_code == 200:
                articles = ((response.json().get("items") or [{}])[0]).get("articles", [])
                for a in articles:
                    if a["rank"] > top or fetched >= query.limit:
                        break
                    post = self._to_post(a, project, day, query)
                    if post:
                        fetched += 1
                        yield post
            elif response.status_code != 404:       # not yet published
                response.raise_for_status()
            day += timedelta(days=1)

    def _to_post(self, a: dict[str, Any], project: str, day: Any, query: SourceQuery) -> RawPost | None:
        name = a.get("article") or ""
        if name in SKIP or name.startswith(SKIP_PREFIXES):
            return None
        lang = project.split(".")[0]
        return self.make_post(
            url=f"https://{project}.org/wiki/{quote(name)}",
            title=name.replace("_", " "),
            text=f"Rank {a['rank']} on {project} with {a['views']:,} views on {day:%Y-%m-%d}.",
            is_snippet=True,
            community=project,
            published_at=datetime(day.year, day.month, day.day, tzinfo=timezone.utc),
            language=lang,
            engagement={"views": a.get("views"), "rank": a.get("rank")},
            industry_hint=query.industry_hint,
            raw={"article": name, "date": f"{day:%Y-%m-%d}"},
        )
