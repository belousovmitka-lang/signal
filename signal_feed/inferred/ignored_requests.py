"""Detector 5: ignored requests (section 17.3). Requests with many votes, open for a long time, with no
status change: GitHub issues and discussions today (public feature boards aren't collected yet).

Votes are the issue's reactions or the discussion's upvotes; "no status change" means the issue is still
open (a discussion: no accepted answer). Whether the vendor ever replied isn't in the collected data, so it
is left to the critic. Supporting evidence: complaints that name the same tool. Starts as a `trial` detector.

Note: the GitHub connector collects issues created since its last run, so year-old requests only appear
once a connector fetches older popular issues."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from ..config import FeedConfig
from ..db import EnrichedPostRow, FeedStore, PostNicheRow
from ..posts import PostsReader
from .common import Candidate, Evidence, meets_minimum

NAME = "ignored_requests"
TEMPLATE = "{tool} users have asked for {feature} for {time}; nobody serves them."


def _votes(post) -> int:
    e = post.engagement or {}
    return int(e.get("reactions") or e.get("upvotes") or 0)


def _open(post) -> bool:
    raw = post.raw or {}
    if "state" in raw:
        return raw["state"] == "open"
    return not raw.get("answer_chosen_at") and not raw.get("answerChosenAt")


def _age_words(days: int) -> str:
    years = days / 365
    return f"over {int(years)} year{'s' if years >= 2 else ''}" if years >= 1 else f"{days // 30} months"


def find_candidates(store: FeedStore, posts: PostsReader, config: FeedConfig, now: datetime) -> tuple[list[Candidate], int]:
    cfg, icfg = config.inferred.ignored_requests, config.inferred
    with store.session() as s:
        rows = {r.post_id: r for r in s.scalars(select(EnrichedPostRow).where(
            EnrichedPostRow.status == "ok", EnrichedPostRow.post_type == "tool_gap"))}
        leaf = dict(s.execute(select(PostNicheRow.post_id, PostNicheRow.niche_id)
                              .where(PostNicheRow.tree == "industry")).all())
    raw = {p.id: p for p in posts.get_posts(list(rows))}
    requests = [p for p in raw.values() if p.source.startswith("github") and _votes(p) >= cfg.min_votes
                and (now - p.published_at) >= timedelta(days=cfg.min_age_days) and _open(p)]
    out = []
    for req in requests:
        row = rows[req.id]
        tool = (row.tools_mentioned or [None])[0] or (req.community or "the tool").split("/")[-1]
        age = (now - req.published_at).days
        evidence = [Evidence(post_id=req.id, source=req.source, source_type=req.source_type, url=req.url,
                             what_it_shows=f"Open {age} days with {_votes(req)} votes: {req.title}")]
        for r in rows.values():
            p = raw.get(r.post_id)
            if p is None or p.id == req.id or tool.lower() not in {t.lower() for t in (r.tools_mentioned or [])}:
                continue
            evidence.append(Evidence(post_id=p.id, source=p.source, source_type=p.source_type, url=p.url,
                                     what_it_shows=f"A complaint about {tool}: {r.pain_statement}"))
            if len(evidence) >= 8:
                break
        near_miss = not meets_minimum(evidence, icfg)   # corroboration may complete it
        feature = (row.task or (req.title or "this feature")).strip().rstrip(".")
        out.append(Candidate(near_miss=near_miss, detector=NAME, task=feature, who=f"{tool} users", niche_id=leaf.get(req.id),
                             evidence=evidence, hypothesis=TEMPLATE.format(tool=tool, feature=feature,
                                                                            time=_age_words(age)),
                             rows=[rows[e.post_id] for e in evidence if e.post_id in rows],
                             posts=[raw[e.post_id] for e in evidence if e.post_id in raw]))
    return out, len(requests)
