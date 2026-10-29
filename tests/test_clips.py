import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from signal_connectors.clips import ClipIn, list_clips, save_clip  # noqa: E402
from signal_connectors.store import Store  # noqa: E402

POST_URL = "https://www.reddit.com/r/FreightBrokers/comments/abc/rate_cons/?utm_source=share"


def client(store: Store) -> TestClient:
    from scripts.clip_server import make_app
    return TestClient(make_app(store))


def test_save_list_update_delete(store):
    c = client(store)
    r = c.post("/clips", json={"user_id": "u1", "url": POST_URL, "title": "Rate cons by hand",
                               "text": "I re-key every rate con. Call me at 555-123-4567 or bob@example.com",
                               "note": "dispatchers re-enter rate confirmations"})
    assert r.status_code == 200 and r.json()["new"] is True
    clip = r.json()
    assert clip["url"] == "https://www.reddit.com/r/FreightBrokers/comments/abc/rate_cons"     # tracking dropped
    assert clip["site"] == "reddit.com" and "[phone]" in clip["text"] and "[email]" in clip["text"]
    # Same page and selection again: the clip is updated, not duplicated.
    again = c.post("/clips", json={"user_id": "u1", "url": POST_URL, "text": clip["text"], "note": "new note"})
    assert again.json()["id"] == clip["id"] and again.json()["new"] is False
    listed = c.get("/clips", params={"user_id": "u1"}).json()
    assert len(listed) == 1 and listed[0]["note"] == "new note"
    assert c.get("/clips", params={"user_id": "u2"}).json() == []            # clips are per user
    assert c.delete(f"/clips/{clip['id']}", params={"user_id": "u2"}).status_code == 404
    assert c.delete(f"/clips/{clip['id']}", params={"user_id": "u1"}).json() == {"ok": True}
    assert list_clips(store, "u1") == []
    assert store.count_posts() == 0                                            # never in the shared feed


def test_rejects_bad_clips(store):
    c = client(store)
    assert c.post("/clips", json={"user_id": "u1", "url": "chrome://extensions", "text": "x"}).status_code == 422
    assert c.post("/clips", json={"user_id": "u1", "url": POST_URL, "text": "  "}).status_code == 422
    assert c.post("/clips", json={"url": POST_URL, "text": "x"}).status_code == 422
    with pytest.raises(ValueError):
        save_clip(store, ClipIn(user_id="u1", url=POST_URL, text="", note=None))


def test_page_lists_clips_escaped(store):
    save_clip(store, ClipIn(user_id="me", url=POST_URL, text="<script>alert(1)</script> by hand", title="T"))
    page = client(store).get("/", params={"user_id": "me"}).text
    assert "&lt;script&gt;" in page and "<script>" not in page


def test_clips_from_sites_we_cannot_collect(store):
    # Amazon Seller Forums and Meetup forbid our bot (2026-10-29); a user's own clip is how they get in.
    c = client(store)
    for url, site in [("https://sellercentral.amazon.com/seller-forums/discussions/t/df36ea80-3dbd-4358-852f-c5f108fdea5a",
                       "sellercentral.amazon.com"),
                      ("https://www.meetup.com/amazon-sellers-indianapolis-network/events/311234567/?recId=abc",
                       "meetup.com")]:
        r = c.post("/clips", json={"user_id": "u1", "url": url, "text": "Account suspended with zero sales, support loops"})
        assert r.status_code == 200 and r.json()["site"] == site
    assert len(list_clips(store, "u1")) == 2 and store.count_posts() == 0
