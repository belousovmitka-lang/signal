"""Bluesky through Jetstream: every new public post on the network, filtered here to work pain.

Jetstream (bsky.network/docs/jetstream, read 2026-10-17) streams the network's records as JSON over
one WebSocket; the live tail needs no key, isn't metered, and replays from a cursor (a unix-microsecond
time) within a 36-hour lookback. So each run resumes from the last run, reads until it reaches the
time the run started, and disconnects: about 160,000 posts and ~160 MB an hour (no compression offered),
replayed at ~18,000 events a second.

Nearly all of it is news, politics and fandom, so a post is kept only when it
- is in English (the record's own `langs`),
- states a workflow pain in the first person (WORKFLOW_PAIN: "by hand", "is there a tool", ...), and
- names an industry: a self-described role ("I'm a dispatcher") or a term from its vocabulary.
The best-matching industry becomes industry_hint; the feed's tagging decides whether it's a real problem.
Everything else is dropped in memory, unstored. Measured 2026-10-17: ~390,000 English posts gave ~73 with a
first-person workflow pain and ~4 that also named an industry, so expect a handful a day. Requiring a
self-described role or two industry terms kept none.

Bluesky Developer Guidelines (github.com/bluesky-social/bsky-docs, developer-guidelines.md): honor
deletions (guideline 3). Delete events in the stream purge stored posts at once; older posts are
re-checked with getPosts (deletion_recheck_days). Authors labelled !no-unauthenticated (hidden from
logged-out viewers) are skipped, checked with app.bsky.actor.getProfiles on the public AppView,
whose robots.txt allows crawling the public API. No engagement counts: Jetstream carries records only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import cached_property
from typing import Any
from urllib.parse import urlencode

from ..base import BaseConnector
from ..industries import Industry, load_industries
from ..models import RawPost, SourceQuery, make_post_id, utcnow
from ..parsing import parse_date
from ..privacy import extract_author_role, hash_author
from ..ws import WebSocket, WebSocketError
from . import register
from .search_api import BSKY_API, BSKY_BATCH, NO_UNAUTHENTICATED, BlueskyResolver

log = logging.getLogger(__name__)

ENDPOINT = "wss://jetstream.us-east.bsky.network/xrpc/network.bsky.jetstream.subscribeEvents"
POST = "app.bsky.feed.post"
LOOKBACK = timedelta(hours=35)            # Bluesky-hosted instances keep 36 hours
RECONNECTS = 3
RETRY_SECONDS = 10

# A workflow pain in the author's own words. Emotional words alone ("nightmare", "sick of", "paperwork")
# matched mostly politics and fandom in 12 hours of the stream (2026-10-17), so they don't count here.
WORKFLOW_PAIN = (
    r"by hand|manually|spreadsheets?|re-?enter(?:ing)?|copy(?:ing)? and past(?:e|ing)|copy-?past(?:e|ing)|double entry"
    r"|takes (?:forever|hours)|hours every (?:day|week)|i'?d pay for|i would pay for"
    r"|is there (?:a|an) (?:tool|app|software|way to automate)|is there software|any (?:software|app|tool) (?:for|that)"
    r"|looking for (?:a )?(?:tool|software|an app)|wish there (?:was|were) (?:a|an) (?:tool|app|way|software|program)"
    r"|there(?:'s| has| has got) (?:to be|got to be) a better way|i hate having to|still on paper|every time i have to"
)
# Vocabulary entries too common outside work to name an industry on their own.
GENERIC_WORDS = {"takes forever", "wish there was", "is there a tool", "is there software", "so tired of",
                 "nightmare", "burnout", "paperwork", "server", "close", "covers", "census", "partner",
                 "carrier", "contractor", "price increases", "no-shows", "how do you handle"}
FIRST_PERSON = re.compile(r"\b(?:i|i'm|im|i've|my|we|we're|our|us)\b", re.I)


def _term(t: str, *, brand: bool = False) -> re.Pattern[str]:
    t = t.strip()
    # Acronyms ("BOL", "HOS") and product names ("Toast", "Clio") are case-sensitive: "pod", "toast".
    exact = brand or (t.isupper() and len(t) <= 5)
    return re.compile(rf"(?<![\w-]){re.escape(t)}s?(?![\w-])", 0 if exact else re.I)


@dataclass
class _Vocab:
    key: str
    roles: list[str]
    terms: list[tuple[str, re.Pattern[str]]] = field(default_factory=list)


class PainMatcher:
    """Decides which posts are someone's work problem, and in which industry."""

    def __init__(self, industries: dict[str, Industry]) -> None:
        self.pain = re.compile(rf"(?<!\w)(?:{WORKFLOW_PAIN})(?!\w)", re.I)
        self.vocabs = []
        for key, m in sorted(industries.items()):
            v = m.vocabulary
            words = {w.strip() for w in v.roles + v.tasks + v.jargon + v.pain_phrases if len(w.strip()) >= 3}
            # The industry has to show in its own words, not in the pain marker ("manually", "spreadsheet").
            words = {w for w in words if not self.pain.search(w) and w.lower() not in GENERIC_WORDS}
            tools = {t.strip() for t in v.tools if len(t.strip()) >= 3}
            terms = [(w, _term(w)) for w in sorted(words)] + [(t, _term(t, brand=True)) for t in sorted(tools)]
            self.vocabs.append(_Vocab(key, v.roles, terms))

    def match(self, text: str) -> tuple[str, str | None] | None:
        """(industry, author role) when the post states a pain in an industry's words, else None."""
        if not text or not self.pain.search(text) or not FIRST_PERSON.search(text):
            return None
        best: tuple[int, str, str | None] | None = None
        for v in self.vocabs:
            role = extract_author_role(text, v.roles)        # "I'm a dispatcher", "paralegal here"
            found = {w.lower() for w, rx in v.terms if rx.search(text)}
            if not (role or found):
                continue
            score = len(found) + 2 * bool(role)
            if best is None or score > best[0]:
                best = (score, v.key, role)
        return (best[1], best[2]) if best else None


def web_url(did: str, rkey: str) -> str:
    return f"https://bsky.app/profile/{did}/post/{rkey}"


def time_us(when: datetime) -> int:
    return int(when.timestamp() * 1_000_000)


@register
class JetstreamConnector(BaseConnector):
    connector = "jetstream"
    route = "own_api"
    source_type = "social"
    default_cadence = "hourly"
    filter_since = False                 # the stream cursor decides; createdAt is set by the author's client

    @cached_property
    def matcher(self) -> PainMatcher:
        return PainMatcher(load_industries())

    async def fetch(self, query: SourceQuery) -> AsyncIterator[RawPost]:
        end = utcnow()
        since = max(query.since or end - LOOKBACK, end - LOOKBACK)
        cursor: int | str = time_us(since)
        endpoint = self.source.options.get("endpoint", ENDPOINT)
        # A safety valve: 35 hours are ~5.6M events. Hitting it skips the rest of the window (logged).
        max_events = int(self.source.options.get("max_events", 8_000_000))
        kept: list[dict[str, Any]] = []
        deletes: list[str] = []
        seen = english = 0
        failures = 0
        done = False
        while not done:
            before = cursor
            params = {"collections": POST, "kinds": "commit", "cursor": cursor}
            try:
                ws = await WebSocket.connect(f"{endpoint}?{urlencode(params)}", subprotocols=["xrpc.v1.json"],
                                             headers={"User-Agent": self.http.user_agent})
            except WebSocketError as exc:
                if exc.status == 400 and failures == 0:      # cursor below the lookback floor: start at the floor
                    log.warning("%s: cursor refused (%s); reading from the oldest available", self.name, exc.body[:200])
                    cursor, failures = time_us(utcnow() - LOOKBACK + timedelta(minutes=5)), 1
                    continue
                failures += 1                                 # connection reset or DNS: retry with backoff
                if exc.status is not None or failures > RECONNECTS:
                    raise
                log.warning("%s: %s; retrying in %ds", self.name, exc, RETRY_SECONDS * failures)
                await asyncio.sleep(RETRY_SECONDS * failures)
                continue
            try:
                async for message in ws.messages(idle_timeout=60):
                    event = json.loads(message).get("payload") or {}
                    at = parse_date(event.get("time"))
                    if (at and at >= end) or seen >= max_events:     # caught up with the run's start
                        if seen >= max_events:
                            log.warning("%s: stopped at max_events=%d, %s behind the run's start", self.name,
                                        max_events, end - at if at else "?")
                        done = True
                        break
                    seen += 1
                    cursor = event.get("seq", cursor)
                    if event.get("collection") == POST:
                        if event.get("operation") == "delete":
                            deletes.append(web_url(event["did"], event["rkey"]))
                        elif event.get("operation") == "create":
                            record = event.get("record") or {}
                            if "en" in [lang.split("-")[0].lower() for lang in record.get("langs") or []]:
                                english += 1
                                if hit := self.matcher.match(record.get("text") or ""):
                                    kept.append({"event": event, "industry": hit[0], "role": hit[1]})
                else:
                    raise WebSocketError("stream closed by the server")
            except WebSocketError as exc:
                # The server ends long replays now and then; only failures without progress count.
                failures = 1 if cursor != before else failures + 1
                if failures > RECONNECTS:
                    raise
                log.warning("%s: %s; reconnecting from seq %s", self.name, exc, cursor)
            finally:
                await ws.close()
        stored = self.services.store.post_ids(self.name)
        purged = self.services.store.delete_posts([i for u in deletes if (i := make_post_id(self.name, u)) in stored])
        log.info("%s: read %d events (%d English posts), kept %d, purged %d deleted", self.name, seen, english,
                 len(kept), purged)
        hidden = await self._hidden_authors({k["event"]["did"] for k in kept})
        for k in kept:
            if k["event"]["did"] not in hidden and (post := self._to_post(k)):
                yield post

    async def _hidden_authors(self, dids: set[str]) -> set[str]:
        """Authors who asked not to be shown to logged-out viewers (or whose profile is gone)."""
        ids, shown = sorted(dids), set()
        for i in range(0, len(ids), BSKY_BATCH):
            await self.services.robots.require(f"{BSKY_API}/app.bsky.actor.getProfiles")
            data = await self.http.get_json(f"{BSKY_API}/app.bsky.actor.getProfiles",
                                            params=[("actors", d) for d in ids[i:i + BSKY_BATCH]])
            for profile in data.get("profiles", []):
                if not any(label.get("val") == NO_UNAUTHENTICATED for label in profile.get("labels") or []):
                    shown.add(profile.get("did"))
        return dids - shown

    def _to_post(self, kept: dict[str, Any]) -> RawPost | None:
        event, record = kept["event"], kept["event"].get("record") or {}
        reply = record.get("reply") or {}
        return self.make_post(
            url=web_url(event["did"], event["rkey"]),
            title=None,
            text=record.get("text") or "",
            is_snippet=False,
            author_hash=hash_author(event["did"], "bluesky"),
            author_role=kept["role"],
            community="Bluesky",
            published_at=parse_date(record.get("createdAt")) or parse_date(event.get("time")),
            language="en",
            engagement={},
            industry_hint=kept["industry"],
            query=None,
            raw={"uri": f"at://{event['did']}/{POST}/{event['rkey']}", "cid": event.get("cid"),
                 "seq": event.get("seq"), "reply_to": (reply.get("parent") or {}).get("uri"),
                 "embed": (record.get("embed") or {}).get("$type")},
        )

    async def deleted(self, urls: list[str]) -> list[str]:
        """Stored posts deleted or hidden since (getPosts on the public AppView)."""
        return await BlueskyResolver(self.http, self.services.robots).deleted(urls)
