from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from signal_connectors.models import RAW_MAX_STRING, RawPost, SourceQuery, make_post_id
from signal_connectors.privacy import hash_author


def post(**overrides):
    fields = dict(
        source="hn", source_type="community", route="own_api",
        url="https://news.ycombinator.com/item?id=1&utm_campaign=z",
        title="Ask HN: tool for X?", text="body", is_snippet=False,
        published_at=datetime(2026, 10, 1, 12, 0),
    )
    fields.update(overrides)
    return RawPost(**fields)


def test_id_and_url_are_canonical():
    p = post()
    assert p.url == "https://news.ycombinator.com/item?id=1"
    assert p.id == make_post_id("hn", "https://news.ycombinator.com/item?id=1")
    assert post(url="https://NEWS.ycombinator.com/item?id=1#c").id == p.id
    assert post(source="brave:ycombinator.com").id != p.id


def test_datetimes_are_utc_aware():
    p = post(published_at=datetime(2026, 10, 1, 14, 0, tzinfo=timezone(timedelta(hours=2))))
    assert p.published_at == datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    assert post().published_at.tzinfo is not None
    assert p.fetched_at.tzinfo is not None


def test_rejects_raw_author_handles():
    with pytest.raises(ValidationError, match="never a raw handle"):
        post(author_hash="some_user_123")
    assert post(author_hash=hash_author("some_user_123", "hn")).author_hash


def test_needs_title_or_text():
    with pytest.raises(ValidationError):
        post(title=None, text="   ")
    assert post(title=None, text="only text").text == "only text"


def test_rejects_unknown_fields_and_bad_enums():
    with pytest.raises(ValidationError):
        post(author_name="leak")
    with pytest.raises(ValidationError):
        post(route="scrape")


def test_engagement_and_raw_are_normalized():
    p = post(engagement={"upvotes": "12", "comments": None}, raw={"body": "x" * 10_000, "items": list(range(500))})
    assert p.engagement == {"upvotes": 12}
    assert len(p.raw["body"]) == RAW_MAX_STRING + 1
    assert len(p.raw["items"]) == 50


def test_source_query_defaults():
    q = SourceQuery(keywords=["is there a tool", "paralegal"], since=datetime(2026, 1, 1))
    assert q.limit == 50 and q.since.tzinfo is timezone.utc
    assert q.query_text == "is there a tool paralegal"
