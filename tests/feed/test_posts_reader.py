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
