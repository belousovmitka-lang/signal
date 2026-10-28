import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from signal_feed.posts import PostsReader


def test_reads_posts_and_word_counts(posts, post_ids):
    short, long = post_ids
    assert posts.existing_ids({short, "nope"}) == {short}
    assert posts.word_counts({long})[long] == 460
    assert [p.id for p in posts.get_posts({short})] == [short]
    assert {p.id for p in posts.iter_posts()} == {short, long}


def test_connection_is_read_only(posts_db):
    reader = PostsReader(posts_db[0])
    with reader.engine.connect() as conn, pytest.raises(OperationalError, match="readonly"):
        conn.execute(text("DELETE FROM posts"))


def test_ids_from_one_source_or_a_family(tmp_path):
    from signal_connectors.models import RawPost
    from signal_connectors.store import Store
    from signal_feed.posts import PostsReader
    from tests.feed.conftest import make_post
    url = f"sqlite:///{tmp_path / 'sig.db'}"
    posts = [make_post("https://a.example/1"),
             RawPost(source="x", source_type="social", route="own_api", url="https://x.com/i/status/1",
                     text="by hand again", is_snippet=False, published_at=make_post("https://b").published_at),
             RawPost(source="rss:law360", source_type="news", route="rss", url="https://law.example/1",
                     text="news", is_snippet=False, published_at=make_post("https://b").published_at)]
    Store(url).save_posts(posts, text_dedupe=False)
    r = PostsReader(url)
    assert r.ids_from_source("x") == {posts[1].id}
    assert r.ids_from_source("rss:") == {posts[2].id} and r.ids_from_source("rss") == set()


def test_lookups_of_more_ids_than_sqlite_variables(posts, post_ids):
    many = [f"missing{k}" for k in range(40_000)] + list(post_ids)
    assert posts.existing_ids(many) == set(post_ids)
    assert {p.id for p in posts.get_posts(many)} == set(post_ids)
    assert set(posts.word_counts(many)) == set(post_ids)
