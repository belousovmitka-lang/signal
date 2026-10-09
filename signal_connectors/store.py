"""Database writes. SQLite now; Postgres later by changing DATABASE_URL."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    TypeDecorator,
    create_engine,
    delete,
    event,
    func,
    select,
)
from sqlalchemy.engine import Dialect, Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from .dedupe import text_hash
from .models import FetchResult, RawPost, to_utc


class UTCDateTime(TypeDecorator[datetime]):
    """Stores UTC and always returns timezone-aware datetimes (SQLite drops tzinfo)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return to_utc(value) if value else None

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return to_utc(value) if value else None


class Base(DeclarativeBase):
    pass


class PostRow(Base):
    __tablename__ = "posts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source: Mapped[str] = mapped_column(String(200), index=True)
    source_type: Mapped[str] = mapped_column(String(32))
    route: Mapped[str] = mapped_column(String(32))
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    text_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    is_snippet: Mapped[bool] = mapped_column(Boolean)
    author_hash: Mapped[str | None] = mapped_column(String(64))
    author_role: Mapped[str | None] = mapped_column(String(200))
    community: Mapped[str | None] = mapped_column(String(300))
    published_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime)
    language: Mapped[str | None] = mapped_column(String(16))
    engagement: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)
    money: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    industry_hint: Mapped[str | None] = mapped_column(String(100))
    query: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class RunRow(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(200), index=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    status: Mapped[str] = mapped_column(String(16))
    fetched: Mapped[int] = mapped_column(Integer, default=0)
    new: Mapped[int] = mapped_column(Integer, default=0)
    duplicates: Mapped[int] = mapped_column(Integer, default=0)
    invalid: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    errors: Mapped[list[str]] = mapped_column(JSON, default=list)


class CostEventRow(Base):
    __tablename__ = "cost_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(200), index=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    usd: Mapped[float] = mapped_column(Float, default=0.0)
    units: Mapped[float] = mapped_column(Float, default=0.0)   # API quota units (YouTube units, Threads queries)
    calls: Mapped[int] = mapped_column(Integer, default=1)
    note: Mapped[str | None] = mapped_column(Text)


class CursorRow(Base):
    """Per-query incremental cursor: the start time of that query's last successful fetch."""

    __tablename__ = "query_cursors"

    source: Mapped[str] = mapped_column(String(200), primary_key=True)
    query_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_success: Mapped[datetime] = mapped_column(UTCDateTime)


@dataclass
class SaveResult:
    new: int = 0
    duplicates: int = 0


def _sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


class Store:
    def __init__(self, url: str) -> None:
        if url.startswith("sqlite:///") and not url.endswith(":memory:"):
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        self.engine: Engine = create_engine(url)
        if self.engine.dialect.name == "sqlite":
            event.listen(self.engine, "connect", _sqlite_pragmas)
        Base.metadata.create_all(self.engine)

    def session(self) -> Session:
        return Session(self.engine, expire_on_commit=False)

    # --- posts ---------------------------------------------------------------

    def save_posts(self, posts: Iterable[RawPost]) -> SaveResult:
        """Insert posts that are new by id and, failing that, by normalized text hash."""
        posts = list(posts)
        result = SaveResult()
        if not posts:
            return result
        with self.session() as s, s.begin():
            ids = {p.id for p in posts}
            hashes = {h for p in posts if (h := text_hash(p.text))}
            seen_ids = set(s.scalars(select(PostRow.id).where(PostRow.id.in_(ids))))
            seen_hashes = set(s.scalars(select(PostRow.text_hash).where(PostRow.text_hash.in_(hashes)))) if hashes else set()
            for post in posts:
                h = text_hash(post.text)
                if post.id in seen_ids or (h and h in seen_hashes):
                    result.duplicates += 1
                    continue
                s.add(PostRow(text_hash=h, **post.model_dump()))
                seen_ids.add(post.id)
                if h:
                    seen_hashes.add(h)
                result.new += 1
        return result

    def get_post(self, post_id: str) -> RawPost | None:
        with self.session() as s:
            row = s.get(PostRow, post_id)
            if row is None:
                return None
            data = {c: getattr(row, c) for c in RawPost.model_fields}
            return RawPost.model_validate(data)

    def count_posts(self, source: str | None = None) -> int:
        stmt = select(func.count()).select_from(PostRow)
        if source:
            stmt = stmt.where(PostRow.source == source)
        with self.session() as s:
            return s.scalar(stmt) or 0

    def ids_for_recheck(self, source: str, published_after: datetime) -> list[tuple[str, str]]:
        """(id, url) pairs for posts a platform's terms require us to re-check for deletion."""
        stmt = select(PostRow.id, PostRow.url).where(
            PostRow.source == source, PostRow.published_at >= published_after)
        with self.session() as s:
            return [(r.id, r.url) for r in s.execute(stmt)]

    def delete_posts(self, ids: Sequence[str]) -> int:
        """Purge posts deleted at the origin (rule 5)."""
        if not ids:
            return 0
        with self.session() as s, s.begin():
            return s.execute(delete(PostRow).where(PostRow.id.in_(ids))).rowcount or 0

    # --- runs ----------------------------------------------------------------

    def record_run(self, result: FetchResult) -> None:
        with self.session() as s, s.begin():
            s.add(RunRow(**result.model_dump()))

    def last_success(self, source: str) -> datetime | None:
        """Start time of the last fully successful run (health reporting; fetching uses query cursors)."""
        stmt = select(func.max(RunRow.started_at)).where(RunRow.source == source, RunRow.status == "ok")
        with self.session() as s:
            value = s.scalar(stmt)
            return to_utc(value) if value else None

    def get_cursor(self, source: str, query_key: str) -> datetime | None:
        with self.session() as s:
            row = s.get(CursorRow, (source, query_key))
            return row.last_success if row else None

    def set_cursor(self, source: str, query_key: str, when: datetime) -> None:
        with self.session() as s, s.begin():
            row = s.get(CursorRow, (source, query_key))
            if row is None:
                s.add(CursorRow(source=source, query_key=query_key, last_success=when))
            else:
                row.last_success = when

    # --- costs ---------------------------------------------------------------

    def add_cost_event(self, source: str, at: datetime, usd: float, units: float, calls: int,
                       note: str | None) -> None:
        with self.session() as s, s.begin():
            s.add(CostEventRow(source=source, at=at, usd=usd, units=units, calls=calls, note=note))

    def cost_totals(self, start: datetime, end: datetime, source: str | None = None) -> dict[str, dict[str, float]]:
        stmt = (
            select(CostEventRow.source, func.sum(CostEventRow.usd), func.sum(CostEventRow.units),
                   func.sum(CostEventRow.calls))
            .where(CostEventRow.at >= start, CostEventRow.at < end)
            .group_by(CostEventRow.source)
        )
        if source:
            stmt = stmt.where(CostEventRow.source == source)
        with self.session() as s:
            return {
                name: {"usd": float(usd or 0), "units": float(units or 0), "calls": float(calls or 0)}
                for name, usd, units, calls in s.execute(stmt)
            }
