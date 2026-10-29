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


def test_signed_in_app_serves_the_web_app_saved_items_and_no_scores(env, tmp_path):
    app, mail, _ = env
    c, _ = sign_in(app, mail, "eve@example.com")
    uid = c.post("/onboarding", json={"industries": ["Legal"]}).json()["user_id"]
    page = c.get("/feed", params={"user_id": uid, "session_id": "s1"}).json()
    assert page["items"] and all(it["score"] is None and it["score_breakdown"] == {} for it in page["items"])
    first = page["items"][0]
    c.post("/events", json={"user_id": uid, "events": [{"client_event_id": "e1", "session_id": "s1", "type": "save",
                                                        "item_type": first["item_type"], "item_id": first["item_id"],
                                                        "at": NOW.isoformat()}]})
    saved = c.get("/saved", params={"user_id": uid}).json()["items"]
    assert [s["item_id"] for s in saved] == [first["item_id"]] and saved[0]["title"] == first["title"]
    assert TestClient(app).get("/saved", params={"user_id": uid}).status_code == 401


def test_the_web_app_is_served_from_the_same_origin(world, feed_store, feed_config):  # noqa: F811
    from signal_feed.settings import REPO_ROOT
    c = TestClient(create_app(feed_store, world[0], feed_config, web_dir=REPO_ROOT / "web"))
    index = c.get("/")
    assert index.status_code == 200 and '<div id="app">' in index.text
    assert c.get("/static/app.js").status_code == 200 and c.get("/static/vendor/preact-htm.js").status_code == 200
    assert TestClient(create_app(feed_store, world[0], feed_config)).get("/").status_code == 404


class FakeLLM:
    """Stands in for LLMTask: answers explanations and headlines without a model call."""

    def __init__(self):
        self.calls = []

    def ask(self, purpose, system, user, output_model):
        self.calls.append((purpose, user))
        if output_model.__name__ == "Headlines":
            n = user.count("<item id=")
            return output_model(items=[{"id": k, "headline": f"Headline {k}."} for k in range(n)])
        return output_model(paragraphs=[f"{purpose} explanation.", "Second paragraph."])


def test_the_item_page_explains_once_per_style_and_keeps_the_source_secondary(world, feed_store, feed_config):  # noqa: F811
    reader = world[0]
    mail, llm = Mailbox(), FakeLLM()
    app = create_app(feed_store, reader, feed_config,
                     FeedService(feed_store, feed_config, FeedDataCache(feed_store, reader), clock=lambda: NOW, seed=1),
                     auth=Auth(feed_store, feed_config, mail, "https://signal.example", clock=Clock()), llm=llm)
    c, _ = sign_in(app, mail, "fay@example.com")
    uid = c.post("/onboarding", json={"industries": ["Legal"]}).json()["user_id"]
    it = next(i for i in c.get("/feed", params={"user_id": uid, "session_id": "s1"}).json()["items"]
              if i["item_type"] == "post")
    assert it["headline"]
    page = c.get(f"/item/post/{it['item_id']}", params={"user_id": uid}).json()
    assert page["headline"] == it["headline"] and page["url"] and page["explanations"] == {} and page["can_explain"]
    assert "text" not in page                                  # the post's own text never reaches the browser
    for _ in range(2):
        r = c.post(f"/item/post/{it['item_id']}/explain", json={"user_id": uid, "style": "plain"})
        assert r.status_code == 200 and r.json()["text"].startswith("explain_plain")
    assert [p for p, _ in llm.calls] == ["explain_plain"]          # written once, then kept
    assert c.get(f"/item/post/{it['item_id']}", params={"user_id": uid}).json()["explanations"] == \
        {"plain": "explain_plain explanation.\n\nSecond paragraph."}
    assert c.post(f"/item/post/{it['item_id']}/explain", json={"user_id": uid, "style": "poetic"}).status_code == 422
    assert c.get("/item/post/nope", params={"user_id": uid}).status_code == 404
    assert c.get(f"/item/post/{it['item_id']}", params={"user_id": "other"}).status_code == 403


def test_every_item_gets_a_written_headline_and_falls_back_until_then(world, feed_store, feed_config):  # noqa: F811
    from signal_feed.db import EnrichedPostRow
    from signal_feed.feed.data import load
    from signal_feed.feed.explain import headline, headlines_for, needs_headline, write_headlines
    reader = world[0]
    with feed_store.session() as s, s.begin():          # one post with no summary, one with a long one
        rows = list(s.query(EnrichedPostRow).filter_by(status="ok").limit(2))
        rows[0].pain_statement, rows[1].pain_statement = None, "x " * 120
    data = load(feed_store, reader, feed_config)
    by_id = {it.post_id: it for it in data.items}
    no_summary, long_summary = by_id[rows[0].post_id], by_id[rows[1].post_id]
    short = next(it for it in data.items if it.summary and len(it.summary) <= feed_config.explain.headline_max_chars)
    # Before any is written: a short summary stands in, else the original title.
    assert headline(short, {}, feed_config) == short.summary.strip().rstrip(".")
    assert headline(no_summary, {}, feed_config) == (no_summary.title or "")
    assert all(needs_headline(it, feed_config) for it in data.items)
    llm = FakeLLM()
    assert write_headlines(feed_store, reader, feed_config, data, llm, workers=1) == len(data.items)
    written = headlines_for(feed_store, [it.post_id for it in data.items])
    assert set(written) == set(by_id) and all(not h.endswith(".") for h in written.values())
    assert headline(long_summary, written, feed_config) == written[long_summary.post_id]
    assert write_headlines(feed_store, reader, feed_config, data, llm, workers=1) == 0      # nothing left to write
