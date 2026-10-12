"""G8 podcasts: episodes from each show's own RSS feed, with publisher transcripts when the feed links one.

Shows come from the industry maps' `podcasts` (url = the show's feed), found with the Podcast Index
API (`find_podcasts`, scripts/find_podcasts.py). Reading episodes needs no key: podcast feeds exist
for syndication, and robots.txt is checked for every feed and transcript URL.

Transcripts: Podcasting 2.0 `<podcast:transcript>` links (VTT, SRT, JSON, HTML or plain text) are
fetched for up to `transcripts_per_feed` new episodes per run and appended to the show notes. No
speech-to-text: audio is never downloaded.

Access (checked 2026-10-11): Podcast Index: "The core, categorized index will always be available for
free, for any use"; api.podcastindex.org has no robots.txt rules. Free key + secret from
api.podcastindex.org (PODCASTINDEX_KEY, PODCASTINDEX_SECRET).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from collections.abc import AsyncIterator
from datetime import date, datetime, timedelta, timezone
from typing import Any

from ..base import HttpClient
from ..models import RawPost, SourceQuery
from ..parsing import html_to_text
from . import register
from .rss import RssConnector

log = logging.getLogger(__name__)

PODCAST_INDEX = "https://api.podcastindex.org/api/1.0"
TRANSCRIPT_MAX_CHARS = 60_000
_CUE = re.compile(r"^(WEBVTT.*|NOTE.*|\d+|[\d:.,]+\s*-->\s*[\d:.,]+.*)$")
_SPEAKER_TAG = re.compile(r"</?v[^>]*>")


def transcript_text(body: str, kind: str) -> str:
    """Plain text from a VTT, SRT, JSON (Podcasting 2.0 segments), HTML or text transcript."""
    kind = kind.lower()
    if "json" in kind:
        try:
            segments = json.loads(body).get("segments", [])
        except (ValueError, AttributeError):
            return ""
        return " ".join(str(s.get("body", "")).strip() for s in segments).strip()
    if "html" in kind:
        return html_to_text(body)
    if "vtt" in kind or "srt" in kind or "subrip" in kind:
        lines = [_SPEAKER_TAG.sub("", ln).strip() for ln in body.splitlines()]
        return " ".join(ln for ln in lines if ln and not _CUE.match(ln))
    return body.strip()


@register
class PodcastsConnector(RssConnector):
    connector = "podcasts"
    route = "rss"
    source_type = "media"
    default_cadence = "weekly"
    parallel_queries = 4

    def prepare(self, entry: Any) -> Any:
        link = entry.get("link") or ""
        if not link.startswith("http"):        # guid used as a link, or none: point at the episode audio
            enclosure = next((ln.get("href") for ln in entry.get("links", []) if ln.get("rel") == "enclosure"), None)
            if enclosure:
                entry["link"], entry["guidislink"] = enclosure, False
        return entry

    def adapt(self, fields: dict[str, Any], entry: Any, query: SourceQuery) -> dict[str, Any]:
        fields = dict(fields)
        fields["source"] = f"podcast:{query.params.get('name')}"
        fields["source_type"] = "media"
        fields["is_snippet"] = True                     # show notes until a transcript is added
        transcript = entry.get("podcast_transcript") or {}
        fields["raw"] = {**fields["raw"], **{k: v for k, v in {
            "enclosure": next((ln.get("href") for ln in entry.get("links", []) if ln.get("rel") == "enclosure"), None),
            "duration": entry.get("itunes_duration"), "episode": entry.get("itunes_episode"),
            "transcript_url": transcript.get("url"), "transcript_type": transcript.get("type"),
        }.items() if v}}
        return fields

    async def fetch(self, query: SourceQuery) -> AsyncIterator[RawPost]:
        budget = int(self.source.options.get("transcripts_per_feed", 3))
        async for post in super().fetch(query):
            url = post.raw.get("transcript_url")
            if url and budget > 0 and (not query.since or post.published_at >= query.since):
                budget -= 1
                text = await self._transcript(url, post.raw.get("transcript_type", ""))
                if text:
                    post = post.model_copy(update={
                        "text": f"{post.text}\n\nTranscript:\n{text[:TRANSCRIPT_MAX_CHARS]}".strip(),
                        "is_snippet": False, "raw": {**post.raw, "transcript_chars": len(text)}})
            yield post

    async def _transcript(self, url: str, kind: str) -> str:
        try:
            if not await self.services.robots.allowed(url):
                return ""
            response = await self.http.get(url, attempts=2)
            if response.status_code != 200:
                return ""
            return transcript_text(response.text, kind or response.headers.get("content-type", ""))
        except Exception as exc:          # a missing transcript never costs the episode
            log.info("%s: transcript %s failed (%s)", self.name, url, type(exc).__name__)
            return ""


# ---- finding shows (Podcast Index) ------------------------------------------------------------------

def podcast_index_headers(key: str, secret: str, now: float | None = None) -> dict[str, str]:
    stamp = str(int(now if now is not None else time.time()))
    return {"X-Auth-Key": key, "X-Auth-Date": stamp,
            "Authorization": hashlib.sha1((key + secret + stamp).encode()).hexdigest()}


async def search_podcasts(http: HttpClient, term: str, *, by_title: bool = False, limit: int = 20,
                          key: str | None = None, secret: str | None = None) -> list[dict[str, Any]]:
    key, secret = key or os.environ.get("PODCASTINDEX_KEY", ""), secret or os.environ.get("PODCASTINDEX_SECRET", "")
    if not key or not secret:
        raise RuntimeError("PODCASTINDEX_KEY and PODCASTINDEX_SECRET are needed to search Podcast Index")
    path = "search/bytitle" if by_title else "search/byterm"
    data = await http.get_json(f"{PODCAST_INDEX}/{path}", params={"q": term, "max": limit, "clean": "true"},
                               headers=podcast_index_headers(key, secret))
    return data.get("feeds", [])


_WORD = re.compile(r"[a-z][a-z0-9'-]{3,}")


def judge_show(feed: dict[str, Any], terms: set[str], today: date, max_age_days: int = 90,
               check_topic: bool = True) -> str | None:
    """None if the show is worth reading, else why not: dead, asleep, tiny, not English, off-topic.

    `check_topic=False` for shows a person already named in a map."""
    if feed.get("dead"):
        return "marked dead"
    newest = feed.get("newestItemPubdate") or feed.get("lastUpdateTime") or 0
    if datetime.fromtimestamp(newest, tz=timezone.utc).date() < today - timedelta(days=max_age_days):
        return "no recent episodes"
    if (feed.get("episodeCount") or 0) < 5:
        return "fewer than 5 episodes"
    if not str(feed.get("language") or "en").lower().startswith("en"):
        return "not English"
    if not check_topic:
        return None
    words = set(_WORD.findall(f"{feed.get('title', '')} {feed.get('description', '')}".lower()))
    words |= {w[:-1] for w in words if w.endswith("s")}
    return None if words & terms else "off-topic"


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


async def find_shows(http: HttpClient, name: str, roles: list[str], named: list[str], terms: set[str], today: date,
                     limit: int = 8) -> list[tuple[str | None, dict[str, Any]]]:
    """[(the named show it resolves, or None for a new one), Podcast Index feed] for one industry.

    Shows a person already named are matched by exact title and only need to be alive; keyword
    searches (the industry's name and top roles) add shows that also match the vocabulary."""
    picked: list[tuple[str | None, dict[str, Any]]] = []
    seen: set[int] = set()
    for show in named:
        for feed in await search_podcasts(http, show, by_title=True, limit=5):
            if _norm(feed.get("title", "")) == _norm(show) and not judge_show(feed, terms, today, check_topic=False):
                picked.append((show, feed))
                seen.add(feed["id"])
                break
    for term in [name, *roles[:2]]:
        for feed in await search_podcasts(http, term, limit=20):
            if len(picked) >= limit:
                return picked
            if feed["id"] not in seen and not judge_show(feed, terms, today):
                picked.append((None, feed))
                seen.add(feed["id"])
    return picked
