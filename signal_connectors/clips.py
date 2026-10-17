"""Clip to Signal: posts a user saves by hand from any site, with the browser button (clipper/).

Why this route: platforms whose posts we can't collect (Reddit, LinkedIn, Facebook groups) still let a
person read a post and keep a note of it, like a bookmark or a web clipper. The user picks what to keep:
the button sends only the page's link and title, the text they selected, and their own note. It reads
nothing else from the page (no author names, profiles, comments or other posts) and runs only when clicked.

Clips belong to the user who made them: stored per user in the `clips` table, apart from the shared
`posts` feed, listed and deleted by that user. Emails and phone numbers in the selected text are
redacted (rule 4).

    from signal_connectors.clips import save_clip, list_clips, delete_clip, router
    app.include_router(router(store))          # POST /clips, GET /clips, DELETE /clips/{id}

No authentication yet (as in the feed API): the user id comes with the request. Bind the local server
to 127.0.0.1 until the app's auth exists.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator

from .dedupe import canonical_url, sha256
from .models import utcnow
from .privacy import redact_contacts
from .store import ClipRow, Store

MAX_TEXT = 10_000


class ClipIn(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    url: str = Field(max_length=4000)
    title: str | None = Field(default=None, max_length=500)
    text: str = Field(default="", max_length=MAX_TEXT)
    note: str | None = Field(default=None, max_length=4000)

    @field_validator("url")
    @classmethod
    def _http(cls, v: str) -> str:
        parts = urlsplit(v.strip())
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("clips need an http(s) page link")
        return v.strip()

    @field_validator("title", "note")
    @classmethod
    def _blank(cls, v: str | None) -> str | None:
        return (v or "").strip() or None


class Clip(BaseModel):
    id: str
    url: str
    site: str
    title: str | None
    text: str
    note: str | None
    created_at: str
    new: bool | None = None


def _out(row: ClipRow, new: bool | None = None) -> Clip:
    return Clip(id=row.id, url=row.url, site=row.site, title=row.title, text=row.text, note=row.note,
                created_at=row.created_at.isoformat(), new=new)


def save_clip(store: Store, clip: ClipIn) -> Clip:
    url = canonical_url(clip.url)
    text = redact_contacts(clip.text.strip())
    if not text and not clip.note:
        raise ValueError("select some text on the page or write a note")
    # The same user clipping the same text on the same page updates that clip (e.g. a new note).
    clip_id = sha256(f"{clip.user_id}|{url}|{text}")
    now = utcnow()
    row = ClipRow(id=clip_id, user_id=clip.user_id, url=url,
                  site=(urlsplit(url).hostname or "").removeprefix("www."), title=clip.title, text=text,
                  note=clip.note, created_at=now, updated_at=now)
    new = store.upsert_clip(row)
    return _out(row if new else next(r for r in store.list_clips(clip.user_id, 10_000) if r.id == clip_id), new)


def list_clips(store: Store, user_id: str, limit: int = 200) -> list[Clip]:
    return [_out(r) for r in store.list_clips(user_id, limit)]


def delete_clip(store: Store, user_id: str, clip_id: str) -> bool:
    return store.delete_clip(user_id, clip_id)


def router(store: Store) -> Any:
    """FastAPI routes for the browser button and the app (fastapi comes with the `feed` extra)."""
    from fastapi import APIRouter, HTTPException, Query

    api = APIRouter()

    @api.post("/clips", response_model=Clip)
    def post_clip(clip: ClipIn) -> Clip:
        try:
            return save_clip(store, clip)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @api.get("/clips", response_model=list[Clip])
    def get_clips(user_id: str = Query(min_length=1, max_length=64), limit: int = Query(200, ge=1, le=1000)):
        return list_clips(store, user_id, limit)

    @api.delete("/clips/{clip_id}")
    def remove_clip(clip_id: str, user_id: str = Query(min_length=1, max_length=64)) -> dict:
        if not delete_clip(store, user_id, clip_id):
            raise HTTPException(status_code=404, detail="no such clip")
        return {"ok": True}

    return api
