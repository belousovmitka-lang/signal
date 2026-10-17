from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from signal_feed.api import create_app
from signal_feed.events import utcnow


@pytest.fixture
def client(feed_store, posts, feed_config):
    return TestClient(create_app(feed_store, posts, feed_config))


def body(user, post_id, **extra):
    return {"user_id": user, "events": [{
        "client_event_id": "e1", "session_id": "s1", "type": "save", "item_id": post_id,
        "at": (utcnow() - timedelta(seconds=5)).isoformat(), **extra}]}


def test_post_events_stores_and_reports(client, feed_store, user_id, post_ids):
    r = client.post("/events", json=body(user_id, post_ids[0]))
    assert r.status_code == 200
    assert r.json() == {"accepted": 1, "duplicates": 0, "rejected": []}
    assert client.post("/events", json=body(user_id, post_ids[0])).json()["duplicates"] == 1
    assert feed_store.count_events(user_id) == 1


def test_unknown_user_is_404(client, post_ids):
    assert client.post("/events", json=body("ghost", post_ids[0])).status_code == 404


def test_malformed_batch_is_422(client, user_id, post_ids):
    assert client.post("/events", json=body(user_id, post_ids[0], type="like")).status_code == 422
    assert client.post("/events", json={"user_id": user_id, "events": []}).status_code == 422


def test_oversized_batch_is_413(client, feed_config, user_id, post_ids):
    one = body(user_id, post_ids[0])["events"][0]
    events = [{**one, "client_event_id": f"e{i}"} for i in range(feed_config.events.max_batch_size + 1)]
    assert client.post("/events", json={"user_id": user_id, "events": events}).status_code == 413


def test_clip_button_routes_are_mounted(feed_store, posts, feed_config, tmp_path):
    app = TestClient(create_app(feed_store, posts, feed_config, clips_db_url=f"sqlite:///{tmp_path / 'clips.db'}"))
    saved = app.post("/clips", json={"user_id": "u1", "url": "https://forum.example/t/1", "title": "Invoices",
                                     "text": "We still re-key every invoice by hand", "note": "my problem too"})
    assert saved.status_code == 200
    assert [c["url"] for c in app.get("/clips", params={"user_id": "u1"}).json()] == ["https://forum.example/t/1"]
    assert app.get("/clips", params={"user_id": "u2"}).json() == []          # clips are per user
    assert app.delete(f"/clips/{saved.json()['id']}", params={"user_id": "u1"}).status_code == 200
    assert app.post("/clips", json={"user_id": "u1", "url": "javascript:alert(1)"}).status_code == 422
    assert TestClient(create_app(feed_store, posts, feed_config)).get("/clips", params={"user_id": "u1"}).status_code == 404
