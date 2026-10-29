"""Magic-link sign-in (signal_feed/auth.py) and the API acting only as the signed-in user."""

import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from signal_feed.api import create_app
from signal_feed.auth import Auth
from signal_feed.db import AccountRow, LoginLinkRow, SessionRow
from signal_feed.feed.data import FeedDataCache
from signal_feed.feed.service import FeedService

from .test_feed import NOW, world  # noqa: F401  (fixture)


class Mailbox:
    def __init__(self):
        self.sent = []

    def send(self, to, subject, body):
        self.sent.append((to, subject, body))

    def token(self, to=None):
        body = next(b for t, _, b in reversed(self.sent) if to is None or t == to)
        return re.search(r"/auth/callback\?token=([\w-]+)", body).group(1)


class Clock:
    def __init__(self):
        self.t = NOW

    def __call__(self):
        return self.t


@pytest.fixture
def env(world, feed_store, feed_config, tmp_path):  # noqa: F811
    reader = world[0]
    mail, clock = Mailbox(), Clock()
    auth = Auth(feed_store, feed_config, mail, "https://signal.example", clock=clock)
    app = create_app(feed_store, reader, feed_config,
                     FeedService(feed_store, feed_config, FeedDataCache(feed_store, reader), clock=lambda: NOW, seed=1),
                     clips_db_url=f"sqlite:///{tmp_path / 'clips.db'}", auth=auth)
    return app, mail, clock


def sign_in(app, mail, email):
    c = TestClient(app, base_url="https://signal.example")
    assert c.post("/auth/start", json={"email": email}).json() == {"ok": True}
    r = c.post("/auth/verify", json={"token": mail.token(email.lower())})
    assert r.status_code == 200
    return c, r.json()


def test_a_link_signs_in_once_and_onboarding_binds_the_user(env, feed_store):
    app, mail, _ = env
    c = TestClient(app, base_url="https://signal.example")
    assert c.post("/auth/start", json={"email": " Ana@Example.com "}).status_code == 200
    (to, subject, body), = mail.sent
    assert to == "ana@example.com" and "https://signal.example/auth/callback?token=" in body
    token = mail.token()

    # Opening the link (or a mail scanner fetching it) doesn't use it: only the button's POST does.
    page = c.get("/auth/callback", params={"token": token})
    assert page.status_code == 200 and "Sign in" in page.text and token in page.text
    out = c.post("/auth/verify", json={"token": token})
    assert out.status_code == 200 and out.json()["user_id"] is None
    assert "httponly" in out.headers["set-cookie"].lower() and "secure" in out.headers["set-cookie"].lower()
    assert c.post("/auth/verify", json={"token": token}).status_code == 401           # one use

    # The cookie is the session; onboarding gives the account its feed user, once.
    assert c.get("/auth/me").json() == {"email": "ana@example.com", "user_id": None}
    uid = c.post("/onboarding", json={"industries": ["Legal"]}).json()["user_id"]
    assert c.get("/auth/me").json()["user_id"] == uid
    assert c.post("/onboarding", json={"industries": ["Legal"]}).status_code == 409
    assert c.get("/feed", params={"user_id": uid, "session_id": "s1"}).status_code == 200

    # Signing in again finds the same account and user.
    _, again = sign_in(app, mail, "ana@example.com")
    assert again["user_id"] == uid

    with feed_store.session() as s:      # tokens are stored only as hashes
        assert token not in {r.token_hash for r in s.scalars(select(LoginLinkRow))}
        assert out.json()["session_token"] not in {r.token_hash for r in s.scalars(select(SessionRow))}
        assert s.scalar(select(AccountRow.email)) == "ana@example.com"


def test_routes_need_a_session_and_act_only_as_its_user(env, feed_store):
    app, mail, _ = env
    anon = TestClient(app)
    assert anon.get("/onboarding/options").status_code == 200                           # public
    assert anon.post("/onboarding", json={"industries": ["Legal"]}).status_code == 401
    victim = feed_store.create_user()
    assert anon.get("/feed", params={"user_id": victim, "session_id": "s"}).status_code == 401
    assert anon.get("/map", params={"user_id": victim}).status_code == 401

    c, signed = sign_in(app, mail, "bo@example.com")
    uid = c.post("/onboarding", json={"industries": ["Legal"]}).json()["user_id"]
    bearer = TestClient(app)                                                             # no cookie, header only
    h = {"Authorization": f"Bearer {signed['session_token']}"}
    assert bearer.get("/map", params={"user_id": uid}, headers=h).status_code == 200
    # Someone else's user: refused before anything about it is revealed (a made-up id gets 403 too, not 404).
    for user in (victim, "made-up"):
        assert bearer.get("/map", params={"user_id": user}, headers=h).status_code == 403
        assert bearer.post("/map/reset", json={"user_id": user}, headers=h).status_code == 403
        assert bearer.post("/events", headers=h, json={"user_id": user, "events": [{
            "client_event_id": "e", "session_id": "s", "type": "save", "item_id": "x",
            "at": NOW.isoformat()}]}).status_code == 403
    assert bearer.get("/map", params={"user_id": uid}, headers={"Authorization": "Bearer nope"}).status_code == 401

    # Clips: own only.
    clip = {"user_id": uid, "url": "https://forum.example/t/1", "text": "We re-key invoices by hand"}
    assert bearer.post("/clips", json=clip, headers=h).status_code == 200
    assert bearer.post("/clips", json={**clip, "user_id": victim}, headers=h).status_code == 403
    assert bearer.get("/clips", params={"user_id": victim}, headers=h).status_code == 403
    assert anon.get("/clips", params={"user_id": uid}).status_code == 401

    assert c.post("/auth/sign-out").status_code == 200
    assert bearer.get("/map", params={"user_id": uid}, headers=h).status_code == 401    # session revoked


def test_links_and_sessions_expire(env):
    app, mail, clock = env
    c = TestClient(app, base_url="https://signal.example")
    c.post("/auth/start", json={"email": "cy@example.com"})
    clock.t += timedelta(minutes=16)
    assert c.post("/auth/verify", json={"token": mail.token()}).status_code == 401

    c, signed = sign_in(app, mail, "cy@example.com")
    clock.t += timedelta(days=20)
    assert c.get("/auth/me").status_code == 200                       # in use: extended to 30 days from now
    clock.t += timedelta(days=29)
    assert c.get("/auth/me").status_code == 200
    clock.t += timedelta(days=31)
    assert c.get("/auth/me").status_code == 401


def test_link_requests_are_limited_and_bad_input_refused(env, feed_config):
    app, mail, _ = env
    c = TestClient(app)
    for _ in range(feed_config.auth.links_per_email_per_hour + 2):
        assert c.post("/auth/start", json={"email": "dee@example.com"}).json() == {"ok": True}   # same answer
    assert len(mail.sent) == feed_config.auth.links_per_email_per_hour
    assert c.post("/auth/start", json={"email": "not an email"}).status_code == 422
    assert c.get("/auth/callback", params={"token": "</script><script>alert(1)</script>"}).status_code == 400
    assert c.post("/auth/verify", json={"token": "x" * 43}).status_code == 401


def test_without_auth_routes_trust_the_given_user(world, feed_store, feed_config):  # noqa: F811
    c = TestClient(create_app(feed_store, world[0], feed_config))
    uid = c.post("/onboarding", json={"industries": ["Legal"]}).json()["user_id"]
    assert c.get("/map", params={"user_id": uid}).status_code == 200
    assert c.post("/auth/start", json={"email": "a@b.co"}).status_code == 404       # no sign-in routes
