"""Pages Signal hosts for a builder's validation tests (Validate tab, web/app.js): a landing page with a
price and a sign-up (smoke test), and a short survey. Public at /p/{id}, no sign-in; the builder sees views,
sign-ups and answers in the workspace.

Visitors are told on the page that what they enter goes to the person running the test. Only the email and
answers they type are stored, plus a salted hash of their IP for rate limits (no IP, no cookies).
"""

from __future__ import annotations

import hashlib
import html
import os
import re
import secrets
from collections.abc import Callable
from datetime import timedelta
from typing import Any, Literal
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update

from signal_connectors.models import utcnow

from .api import Me
from .db import FeedStore, TestPageResponseRow, TestPageRow, WorkspaceRow

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,24}$")
MAX_PER_CLIENT_HOUR = 5
SALT = os.environ.get("SIGNAL_PAGE_SALT") or secrets.token_hex(16)


class LandingContent(BaseModel):
    headline: str = Field(min_length=1, max_length=120)
    subhead: str = Field(default="", max_length=300)
    bullets: list[str] = Field(default_factory=list, max_length=5)
    price: str = Field(default="", max_length=60)
    cta: str = Field(default="Join the pilot", max_length=40)


class SurveyContent(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    intro: str = Field(default="", max_length=400)
    questions: list[str] = Field(min_length=1, max_length=10)
    ask_email: bool = True


class PageIn(BaseModel):
    user_id: str
    kind: Literal["landing", "survey"]
    landing: LandingContent | None = None
    survey: SurveyContent | None = None
    active: bool = True


STYLE = """<style>:root{--ink:#121417;--muted:#585D64;--line:#E2E3DD;--accent:#4D8DFF}*{box-sizing:border-box}
body{margin:0;background:#F4F4F0;color:var(--ink);font:16px/1.55 "Public Sans",system-ui,sans-serif}
main{max-width:640px;margin:0 auto;padding:56px 20px 64px}h1{font:700 38px/1.12 "Schibsted Grotesk",system-ui,sans-serif;
letter-spacing:-.02em;margin:0 0 14px}p.sub{font-size:19px;color:#3C4046;margin:0 0 24px}ul{padding-left:20px;margin:0 0 28px}
li{margin:6px 0}.card{background:#fff;border:1px solid var(--line);border-radius:12px;padding:24px}
.price{font:700 22px/1.3 "Schibsted Grotesk",system-ui,sans-serif;margin:0 0 16px}label{display:block;font-weight:600;margin:0 0 6px}
input,textarea{width:100%;font:inherit;min-height:46px;padding:10px 14px;border:1px solid #D3D5CE;border-radius:10px;margin:0 0 16px}
textarea{min-height:84px}button{font:inherit;font-weight:600;min-height:48px;padding:0 22px;border:0;border-radius:10px;
background:var(--ink);color:#fff;cursor:pointer}small{display:block;color:var(--muted);font-size:13px;margin-top:14px}
@media(max-width:600px){h1{font-size:30px}main{padding-top:32px}}</style>"""


def _page(title: str, body: str) -> str:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">'
            f'<title>{html.escape(title)}</title><link href="https://fonts.googleapis.com/css2?family=Public+Sans:'
            f'wght@400;600&family=Schibsted+Grotesk:wght@700&display=swap" rel="stylesheet">{STYLE}</head>'
            f'<body><main>{body}</main></body></html>')


def render(row: TestPageRow, error: str = "") -> str:
    e, c = html.escape, row.content
    err = f'<p style="color:#B42318">{e(error)}</p>' if error else ""
    note = "<small>What you enter goes to the person running this test. It isn't shared with anyone else.</small>"
    if row.kind == "landing":
        bullets = "".join(f"<li>{e(b)}</li>" for b in c.get("bullets", []) if b.strip())
        price = f'<p class="price">{e(c["price"])}</p>' if c.get("price") else ""
        return _page(c["headline"], f'<h1>{e(c["headline"])}</h1>' + (f'<p class="sub">{e(c["subhead"])}</p>' if c.get("subhead") else "")
                     + (f"<ul>{bullets}</ul>" if bullets else "")
                     + f'<form class="card" method="post">{price}{err}<label for="email">Email</label>'
                     f'<input id="email" name="email" type="email" required autocomplete="email" placeholder="you@company.com">'
                     f'<button>{e(c.get("cta") or "Join the pilot")}</button>{note}</form>')
    qs = "".join(f'<label for="q{i}">{e(q)}</label><textarea id="q{i}" name="q{i}" maxlength="1000"></textarea>'
                 for i, q in enumerate(c["questions"]))
    email = ('<label for="email">Email (optional, if we may follow up)</label>'
             '<input id="email" name="email" type="email" autocomplete="email">') if c.get("ask_email", True) else ""
    return _page(c["title"], f'<h1>{e(c["title"])}</h1>' + (f'<p class="sub">{e(c["intro"])}</p>' if c.get("intro") else "")
                 + f'<form class="card" method="post">{err}{qs}{email}<button>Send</button>{note}</form>')


def _client(request: Request) -> str:
    ip = request.client.host if request.client else "?"
    return hashlib.sha256(f"{SALT}:{ip}".encode()).hexdigest()[:16]


def test_page_routes(app: FastAPI, store: FeedStore, _user: Callable) -> None:
    def _owned(s, user_id: str, problem_id: str) -> None:
        if s.get(WorkspaceRow, (user_id, problem_id)) is None:
            raise HTTPException(status_code=404, detail="not saved")

    @app.put("/workspaces/{problem_id}/pages")
    def put_page(problem_id: str, body: PageIn, request: Request, me: Me) -> dict:
        """Create or update this workspace's page of that kind (one each)."""
        _user(body.user_id, me)
        content = body.landing if body.kind == "landing" else body.survey
        if content is None:
            raise HTTPException(status_code=422, detail=f"{body.kind} content missing")
        now = utcnow()
        with store.session() as s, s.begin():
            _owned(s, body.user_id, problem_id)
            row = s.scalars(select(TestPageRow).where(TestPageRow.user_id == body.user_id, TestPageRow.problem_id == problem_id,
                                                      TestPageRow.kind == body.kind)).first()
            if row is None:
                row = TestPageRow(id=secrets.token_urlsafe(9), user_id=body.user_id, problem_id=problem_id,
                                  kind=body.kind, views=0, created_at=now)
                s.add(row)
            row.content, row.active, row.updated_at = content.model_dump(), body.active, now
        return {"id": row.id, "url": f"{_base(request)}/p/{row.id}"}

    @app.get("/workspaces/{problem_id}/pages")
    def get_pages(problem_id: str, request: Request, me: Me, user_id: str = Query(min_length=1, max_length=64)) -> dict:
        """The workspace's pages with their views, sign-ups and answers (newest first)."""
        _user(user_id, me)
        with store.session() as s:
            _owned(s, user_id, problem_id)
            rows = list(s.scalars(select(TestPageRow).where(TestPageRow.user_id == user_id,
                                                            TestPageRow.problem_id == problem_id)))
            out = {}
            for r in rows:
                resp = list(s.scalars(select(TestPageResponseRow).where(TestPageResponseRow.page_id == r.id)
                                      .order_by(TestPageResponseRow.at.desc()).limit(500)))
                out[r.kind] = {"id": r.id, "url": f"{_base(request)}/p/{r.id}", "content": r.content, "views": r.views,
                               "active": r.active, "responses": [{"email": x.email, "answers": x.answers,
                                                                  "at": x.at.isoformat()} for x in resp]}
        return out

    def _public(page_id: str) -> TestPageRow:
        with store.session() as s:
            row = s.get(TestPageRow, page_id)
        if row is None or not row.active:
            raise HTTPException(status_code=404, detail="no such page")
        return row

    @app.get("/p/{page_id}", response_class=HTMLResponse, include_in_schema=False)
    def public_page(page_id: str, request: Request) -> HTMLResponse:
        row = _public(page_id)
        ua = (request.headers.get("user-agent") or "").lower()
        if not any(b in ua for b in ("bot", "crawl", "spider", "preview", "curl")):
            with store.session() as s, s.begin():
                s.execute(update(TestPageRow).where(TestPageRow.id == page_id).values(views=TestPageRow.views + 1))
        return HTMLResponse(render(row), headers={"Cache-Control": "no-store"})

    @app.post("/p/{page_id}", response_class=HTMLResponse, include_in_schema=False)
    async def public_submit(page_id: str, request: Request):
        row = _public(page_id)
        raw = (await request.body())[:20_000].decode("utf-8", "replace")
        form = {k: v[0].strip() for k, v in parse_qs(raw).items()}
        email = form.get("email", "")[:254] or None
        if email and not EMAIL_RE.match(email):
            return HTMLResponse(render(row, "That doesn't look like an email address."), status_code=422)
        answers: dict[str, Any] = {}
        if row.kind == "landing":
            if not email:
                return HTMLResponse(render(row, "Add your email."), status_code=422)
        else:
            answers = {q: form.get(f"q{i}", "")[:1000] for i, q in enumerate(row.content["questions"])}
            if not any(answers.values()):
                return HTMLResponse(render(row, "Answer at least one question."), status_code=422)
        client, now = _client(request), utcnow()
        with store.session() as s, s.begin():
            recent = s.scalar(select(func.count()).select_from(TestPageResponseRow).where(
                TestPageResponseRow.page_id == page_id, TestPageResponseRow.client_hash == client,
                TestPageResponseRow.at >= now - timedelta(hours=1)))
            if recent >= MAX_PER_CLIENT_HOUR:
                return HTMLResponse(render(row, "Thanks, we already have your answer."), status_code=429)
            s.add(TestPageResponseRow(page_id=page_id, email=email, answers=answers, client_hash=client, at=now))
        return RedirectResponse(f"/p/{page_id}/thanks", status_code=303)

    @app.get("/p/{page_id}/thanks", response_class=HTMLResponse, include_in_schema=False)
    def public_thanks(page_id: str) -> HTMLResponse:
        row = _public(page_id)
        msg = "You're on the list. We'll be in touch." if row.kind == "landing" else "Thank you. That helps a lot."
        return HTMLResponse(_page("Thanks", f"<h1>Thanks</h1><p class=\"sub\">{msg}</p>"))


def _base(request: Request) -> str:
    return (os.environ.get("SIGNAL_APP_URL") or str(request.base_url)).rstrip("/")
