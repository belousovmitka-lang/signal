"""GitHub REST search API (token GITHUB_TOKEN): issues (bounties, feature requests) and new repos.

Terms (checked 2026-10-09): API use is allowed except for spam or selling users' personal data;
no sharing tokens to exceed rate limits. The trending page is not scraped (the AUP limits scraped
data to research/archival use); "trending" here is the search API's newest repos by stars.
Search limit: 30 requests/minute; usernames are stored only as hashes.

Queries:
  {params: {kind: issues}, keywords: ['label:bounty']}            -> /search/issues, newest first
  {params: {kind: repos, min_stars: 50}}                          -> /search/repositories created since cursor
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

from ..base import BaseConnector
from ..models import HealthStatus, RawPost, SourceQuery
from ..parsing import parse_date
from ..privacy import hash_author
from . import register

API = "https://api.github.com"
PAGE = 100
MAX_RESULTS = 1000     # the search API never returns more than 1,000 results per query


@register
class GitHubConnector(BaseConnector):
    connector = "github"
    route = "own_api"
    source_type = "community"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.token = os.environ.get("GITHUB_TOKEN") or None

    async def healthcheck(self) -> HealthStatus:
        status = await super().healthcheck()
        status.auth_ok = status.ok = bool(self.token)
        status.message = None if self.token else "GITHUB_TOKEN is not set"
        return status

    def search_query(self, query: SourceQuery, kind: str) -> str:
        parts = list(query.keywords)
        date_field = "created"
        if kind == "issues":
            parts += ["is:issue", "is:open"]
        else:
            parts.append(f"stars:>={int(query.params.get('min_stars', 50))}")
        if query.since:
            parts.append(f"{date_field}:>={query.since:%Y-%m-%dT%H:%M:%SZ}")
        return " ".join(parts)

    async def fetch(self, query: SourceQuery) -> AsyncIterator[RawPost]:
        if not self.token:
            raise RuntimeError("GITHUB_TOKEN is not set")
        kind = query.params.get("kind", "issues")
        path = "/search/issues" if kind == "issues" else "/search/repositories"
        params: dict[str, Any] = {"q": self.search_query(query, kind), "per_page": min(PAGE, query.limit), "page": 1,
                                  "sort": "created" if kind == "issues" else "stars", "order": "desc"}
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28"}
        fetched = 0
        while fetched < query.limit and (params["page"] - 1) * params["per_page"] < MAX_RESULTS:
            response = await self.http.get(f"{API}{path}", params=params, headers=headers)
            if response.status_code != 200:
                raise RuntimeError(f"GitHub HTTP {response.status_code}: {response.text[:160]}")
            items = response.json().get("items", [])
            for item in items:
                post = self._issue(item, query) if kind == "issues" else self._repo(item, query)
                if post:
                    fetched += 1
                    yield post
                    if fetched >= query.limit:
                        return
            if len(items) < params["per_page"]:
                return
            params["page"] += 1

    def _issue(self, i: dict[str, Any], query: SourceQuery) -> RawPost | None:
        if "pull_request" in i:      # PRs carry the key, sometimes with an empty object
            return None
        repo = (i.get("repository_url") or "").removeprefix(f"{API}/repos/")
        labels = [lab.get("name") for lab in i.get("labels") or [] if lab.get("name")]
        bounty = any("bounty" in (lab or "").lower() or (lab or "").startswith("$") for lab in labels)
        return self.make_post(
            source="github:issues",
            source_type="paid_demand" if bounty else "community",
            url=i["html_url"],
            title=i.get("title"),
            text=(i.get("body") or "")[:8000],
            is_snippet=False,
            author_hash=hash_author((i.get("user") or {}).get("login"), "github"),
            community=repo or None,
            published_at=parse_date(i.get("created_at")),
            language=None,
            engagement={"comments": i.get("comments"), "reactions": (i.get("reactions") or {}).get("total_count")},
            industry_hint=query.industry_hint,
            query=query.query_text,
            raw={"repo": repo, "number": i.get("number"), "labels": labels, "state": i.get("state"),
                 "bounty": bounty},
        )

    def _repo(self, r: dict[str, Any], query: SourceQuery) -> RawPost | None:
        topics = r.get("topics") or []
        return self.make_post(
            source="github:repos",
            source_type="startup",
            url=r["html_url"],
            title=r.get("full_name"),
            text="\n".join(x for x in (r.get("description") or "", f"Topics: {', '.join(topics)}" if topics else "",
                                        f"Language: {r['language']}" if r.get("language") else "") if x),
            is_snippet=False,
            community="GitHub repositories",
            published_at=parse_date(r.get("created_at")),
            language=None,
            engagement={"stars": r.get("stargazers_count"), "forks": r.get("forks_count"),
                        "open_issues": r.get("open_issues_count")},
            industry_hint=query.industry_hint,
            query=query.query_text,
            raw={"topics": topics, "language": r.get("language"), "license": (r.get("license") or {}).get("spdx_id")},
        )
