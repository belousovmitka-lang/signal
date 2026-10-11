from datetime import datetime, timezone
from pathlib import Path

import pytest

from signal_connectors.models import RawPost
from signal_connectors.store import Store
from signal_feed.config import FeedConfig, load_feed_config
from signal_feed.db import FeedStore
from signal_feed.posts import PostsReader
from signal_feed.settings import REPO_ROOT

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
LONG_TEXT = " ".join(["word"] * 460)   # 2 minutes at 230 wpm


def make_post(url: str, text: str = "Every Friday I re-key invoices by hand.") -> RawPost:
    return RawPost(source="hn", source_type="community", route="own_api", url=url, text=text,
                   is_snippet=False, published_at=datetime(2026, 10, 1, tzinfo=timezone.utc))


@pytest.fixture
def feed_config() -> FeedConfig:
    return load_feed_config(REPO_ROOT / "config" / "feed.yaml")


@pytest.fixture
def posts_db(tmp_path: Path) -> tuple[str, list[RawPost]]:
    """A connectors database with two posts: a short one and a two-minute read."""
    url = f"sqlite:///{tmp_path / 'signal.db'}"
    posts = [make_post("https://a.example/short"), make_post("https://a.example/long", LONG_TEXT)]
    Store(url).save_posts(posts)
    return url, posts


@pytest.fixture
def posts(posts_db) -> PostsReader:
    return PostsReader(posts_db[0])


@pytest.fixture
def post_ids(posts_db) -> tuple[str, str]:
    short, long = posts_db[1]
    return short.id, long.id


@pytest.fixture
def feed_store(tmp_path: Path) -> FeedStore:
    return FeedStore(f"sqlite:///{tmp_path / 'feed.db'}")


@pytest.fixture
def user_id(feed_store: FeedStore) -> str:
    return feed_store.create_user("u1")
