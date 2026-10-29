"""Magic-link sign-in: no passwords, no outside identity service.

1. `POST /auth/start {email}` emails a link to `<app_url>/auth/callback?token=...`. The answer is the same
   whether or not the address has an account, and at most `auth.links_per_email_per_hour` links go out.
2. The link opens a page with a "Sign in" button. Mail scanners fetch links before people do, so opening the
   link alone never uses it; the button posts the token to `POST /auth/verify`.
3. `/auth/verify` checks the token (one use, `auth.link_minutes`), creates the account on first sign-in and
   returns a session token, also set as an HttpOnly cookie. Sessions last `auth.session_days` from last use.
4. Every user-scoped route then needs that session (`Authorization: Bearer <token>` or the cookie) and acts
   only as the session's own user. A new account gets its feed user from `POST /onboarding`.

Only SHA-256 hashes of link and session tokens are stored. The email address is kept to send links, nothing else.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import smtplib
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Protocol

from sqlalchemy import delete, func, select

from signal_connectors.models import utcnow

from .config import FeedConfig
from .db import AccountRow, FeedStore, LoginLinkRow, SessionRow

log = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,189}\.[^@\s.]{2,}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,64}$")
COOKIE = "signal_session"


class AuthError(ValueError):
    pass


class Mailer(Protocol):
    def send(self, to: str, subject: str, body: str) -> None: ...


class SmtpMailer:
    """Any SMTP provider (Postmark, Resend, SES, a Gmail app password...): STARTTLS on 587, TLS on 465."""

    def __init__(self, host: str, port: int, user: str | None, password: str | None, sender: str) -> None:
        self.host, self.port, self.user, self.password, self.sender = host, port, user, password, sender

    def send(self, to: str, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.sender, to, subject
        msg.set_content(body)
        smtp = smtplib.SMTP_SSL(self.host, self.port, timeout=20) if self.port == 465 \
            else smtplib.SMTP(self.host, self.port, timeout=20)
        with smtp:
            if self.port != 465:
                smtp.starttls()
            if self.user:
                smtp.login(self.user, self.password or "")
            smtp.send_message(msg)


class LogMailer:
    """Development only: writes the email to the server log instead of sending it."""

    def send(self, to: str, subject: str, body: str) -> None:
        log.warning("SMTP not configured; email to %s not sent:\n%s", to, body)


def mailer_from_env() -> Mailer:
    host = os.environ.get("SMTP_HOST")
    if not host:
        log.warning("SMTP_HOST is not set: sign-in links go to the server log (development only)")
        return LogMailer()
    return SmtpMailer(host, int(os.environ.get("SMTP_PORT") or 587), os.environ.get("SMTP_USER"),
                      os.environ.get("SMTP_PASSWORD"), os.environ.get("MAIL_FROM") or os.environ.get("SMTP_USER") or "")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def normalize_email(email: str) -> str:
    email = (email or "").strip().lower()
    if len(email) > 254 or not EMAIL_RE.match(email):
        raise AuthError("not an email address")
    return email


@dataclass(frozen=True)
class Account:
    id: str
    email: str
    user_id: str | None


@dataclass(frozen=True)
class SignedIn:
    session_token: str
    expires_at: datetime
    account: Account


def _account(row: AccountRow) -> Account:
    return Account(row.id, row.email, row.user_id)


class Auth:
    def __init__(self, store: FeedStore, config: FeedConfig, mailer: Mailer, app_url: str,
                 clock: Callable[[], datetime] = utcnow) -> None:
        self.store, self.cfg, self.mailer, self.clock = store, config.auth, mailer, clock
        self.app_url = app_url.rstrip("/")

    @property
    def secure_cookie(self) -> bool:
        return self.app_url.startswith("https://")

    def start(self, email: str) -> None:
        """Email a sign-in link, unless this address already had its hourly share."""
        email, now = normalize_email(email), self.clock()
        with self.store.session() as s, s.begin():
            s.execute(delete(LoginLinkRow).where(LoginLinkRow.created_at < now - timedelta(days=1)))
            recent = s.scalar(select(func.count()).select_from(LoginLinkRow).where(
                LoginLinkRow.email == email, LoginLinkRow.created_at >= now - timedelta(hours=1))) or 0
            if recent >= self.cfg.links_per_email_per_hour:
                log.info("sign-in link limit reached for an address; not sent")
                return
            token = secrets.token_urlsafe(32)
            s.add(LoginLinkRow(token_hash=_hash(token), email=email, created_at=now,
                               expires_at=now + timedelta(minutes=self.cfg.link_minutes)))
        self.mailer.send(email, "Your Signal sign-in link",
                         f"Sign in to Signal:\n\n{self.app_url}/auth/callback?token={token}\n\n"
                         f"The link works once and expires in {self.cfg.link_minutes} minutes. "
                         "If you didn't ask for it, ignore this email.\n")

    def verify(self, token: str) -> SignedIn:
        """Use a sign-in link: one time, before it expires. Creates the account on first sign-in."""
        now = self.clock()
        if not TOKEN_RE.match(token or ""):
            raise AuthError("invalid or expired link")
        with self.store.session() as s, s.begin():
            link = s.get(LoginLinkRow, _hash(token))
            if link is None or link.used_at is not None or link.expires_at <= now:
                raise AuthError("invalid or expired link")
            link.used_at = now
            acct = s.scalars(select(AccountRow).where(AccountRow.email == link.email)).one_or_none()
            if acct is None:
                acct = AccountRow(id=uuid.uuid4().hex, email=link.email, created_at=now)
                s.add(acct)
            acct.last_login_at = now
            session_token = secrets.token_urlsafe(32)
            expires = now + timedelta(days=self.cfg.session_days)
            s.add(SessionRow(token_hash=_hash(session_token), account_id=acct.id, created_at=now, expires_at=expires))
            s.flush()
            return SignedIn(session_token, expires, _account(acct))

    def session(self, token: str | None) -> Account | None:
        """The account behind a live session token, extending the session (at most one write a day)."""
        if not token or not TOKEN_RE.match(token):
            return None
        now = self.clock()
        with self.store.session() as s, s.begin():
            row = s.get(SessionRow, _hash(token))
            if row is None or row.revoked_at is not None or row.expires_at <= now:
                return None
            renewed = now + timedelta(days=self.cfg.session_days)
            if renewed - row.expires_at >= timedelta(days=1):
                row.expires_at = renewed
            return _account(s.get(AccountRow, row.account_id))

    def sign_out(self, token: str | None) -> None:
        if not token or not TOKEN_RE.match(token):
            return
        with self.store.session() as s, s.begin():
            row = s.get(SessionRow, _hash(token))
            if row is not None and row.revoked_at is None:
                row.revoked_at = self.clock()

    def bind_user(self, account_id: str, user_id: str) -> None:
        with self.store.session() as s, s.begin():
            acct = s.get(AccountRow, account_id)
            if acct.user_id is not None:
                raise AuthError("this account already has a feed user")
            acct.user_id = user_id


def auth_from_env(store: FeedStore, config: FeedConfig) -> Auth | None:
    """On unless SIGNAL_AUTH=off (local experiments only: every route then trusts the user_id it is given)."""
    if (os.environ.get("SIGNAL_AUTH") or "on").lower() == "off":
        log.warning("SIGNAL_AUTH=off: no sign-in; any caller can act as any user")
        return None
    return Auth(store, config, mailer_from_env(), os.environ.get("SIGNAL_APP_URL") or "http://127.0.0.1:8000")
