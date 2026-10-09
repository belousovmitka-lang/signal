import pytest

from signal_connectors.dedupe import canonical_url, normalize_text, text_hash, url_hash


@pytest.mark.parametrize("a, b", [
    ("https://Example.com/a/?utm_source=x&b=2&a=1#frag", "https://example.com/a?a=1&b=2"),
    ("HTTPS://www.reddit.com:443/r/paralegal/comments/abc/title/?share_id=1&ref=share",
     "https://www.reddit.com/r/paralegal/comments/abc/title"),
    ("http://example.com:80//x//y", "http://example.com/x/y"),
    ("https://user:pw@example.com/p?fbclid=1&gclid=2", "https://example.com/p"),
    ("//example.com/", "https://example.com/"),
    ("https://medium.com/@a/post-50e04eb1e905?source=rss-f40835382ca7------2", "https://medium.com/@a/post-50e04eb1e905"),
    ("https://medium.com/@a/post-50e04eb1e905?source=post_page-----x", "https://medium.com/@a/post-50e04eb1e905"),
])
def test_canonical_url(a, b):
    assert canonical_url(a) == b
    assert url_hash(a) == url_hash(b)


def test_canonical_url_keeps_meaningful_params_and_ports():
    assert canonical_url("https://example.com:8443/search?q=tool&page=2") == "https://example.com:8443/search?page=2&q=tool"
    assert canonical_url("https://data.example/api?source=census") == "https://data.example/api?source=census"


@pytest.mark.parametrize("bad", ["", "ftp://example.com/x", "/relative/path", "not a url"])
def test_canonical_url_rejects_non_http(bad):
    with pytest.raises(ValueError):
        canonical_url(bad)


def test_text_hash_catches_cross_posts():
    a = "Is there a TOOL for tracking   client intake?! I do it all manually: https://x.com/1"
    b = "is there a tool for tracking client intake I do it all manually https://y.org/other"
    assert normalize_text(a) == normalize_text(b)
    assert text_hash(a) == text_hash(b) is not None


def test_text_hash_skips_short_generic_text():
    assert text_hash("[deleted]") is None
    assert text_hash("Thanks, this helped!") is None
    assert text_hash(None) is None
