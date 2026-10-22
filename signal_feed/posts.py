"""Read-only access to the connectors' raw posts.

Never goes through signal_connectors.store.Store: its constructor creates and alters tables.
SQLite databases are opened with mode=ro, so a stray write fails instead of landing.
"""

from __future__ import annotations

from collections.abc import Collection, Iterator
from datetime import datetime

from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from signal_connectors.models import RawPost
from signal_connectors.store import ClipRow, PostRow


def _read_only_url(url: str) -> str:
    if url.startswith("sqlite:///") and not url.endswith(":memory:") and "mode=ro" not in url:
        return f"sqlite:///file:{url.removeprefix('sqlite:///')}?mode=ro&uri=true"
    return url


def _to_raw_post(row: PostRow) -> RawPost:
    return RawPost.model_validate({c: getattr(row, c) for c in RawPost.model_fields})


class PostsReader:
    def __init__(self, url: str) -> None:
        self.engine: Engine = create_engine(_read_only_url(url))

    def session(self) -> Session:
        return Session(self.engine)

    def existing_ids(self, ids: Collection[str]) -> set[str]:
        if not ids:
            return set()
        with self.session() as s:
            return set(s.scalars(select(PostRow.id).where(PostRow.id.in_(set(ids)))))

    def all_ids(self) -> list[str]:
        """Every post id, oldest fetch first."""
        with self.session() as s:
            return list(s.scalars(select(PostRow.id).order_by(PostRow.fetched_at, PostRow.id)))

    def word_counts(self, ids: Collection[str]) -> dict[str, int]:
        """Words in title + text per post, for expected reading time."""
        if not ids:
            return {}
        stmt = select(PostRow.id, PostRow.title, PostRow.text).where(PostRow.id.in_(set(ids)))
        with self.session() as s:
            return {pid: len(f"{title or ''} {text}".split()) for pid, title, text in s.execute(stmt)}

    def clips(self, user_id: str | None = None) -> list[ClipRow]:
        """"Clip to Signal" clips (signal.db `clips`, written by signal_connectors.clips), read-only. Empty when
        the table doesn't exist yet."""
        stmt = select(ClipRow) if user_id is None else select(ClipRow).where(ClipRow.user_id == user_id)
        try:
            with self.session() as s:
                rows = list(s.scalars(stmt))
                for r in rows:
                    s.expunge(r)
                return rows
        except OperationalError:
            return []

    def get_posts(self, ids: Collection[str]) -> list[RawPost]:
        if not ids:
            return []
        with self.session() as s:
            return [_to_raw_post(r) for r in s.scalars(select(PostRow).where(PostRow.id.in_(set(ids))))]

    def posts_from_source(self, prefix: str) -> list[RawPost]:
        """Posts whose source is `prefix` or starts with `prefix:` (e.g. "regulations_gov")."""
        with self.session() as s:
            return [_to_raw_post(r) for r in s.scalars(select(PostRow).where(
                (PostRow.source == prefix) | PostRow.source.startswith(f"{prefix}:")))]

    def iter_posts(self, since: datetime | None = None, limit: int | None = None) -> Iterator[RawPost]:
        """Posts by fetch time, oldest first (Stage A enriches in this order)."""
        stmt = select(PostRow).order_by(PostRow.fetched_at, PostRow.id)
        if since is not None:
            stmt = stmt.where(PostRow.fetched_at > since)
        if limit is not None:
            stmt = stmt.limit(limit)
        with self.session() as s:
            for row in s.scalars(stmt):
                yield _to_raw_post(row)
