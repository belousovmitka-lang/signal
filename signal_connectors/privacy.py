"""Author handling under rule 4: store a keyed hash and a self-described role, nothing else."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from collections.abc import Iterable


class MissingSaltError(RuntimeError):
    pass


def hash_author(handle: str | None, namespace: str, salt: str | None = None) -> str | None:
    """HMAC-SHA256 of a platform handle.

    `namespace` is the platform ("reddit", "hn"). Including it means the same handle on
    two platforms hashes differently, so stored hashes cannot link one person across sites.
    """
    if not handle or not handle.strip():
        return None
    salt = salt or os.environ.get("AUTHOR_HASH_SALT")
    if not salt:
        raise MissingSaltError("AUTHOR_HASH_SALT is not set; refusing to store author identifiers")
    message = f"{namespace}:{handle.strip().lower()}".encode()
    return hmac.new(salt.encode(), message, hashlib.sha256).hexdigest()


_ROLE_LEADS = r"(?:i\s*(?:'|’)?\s*m|i\s+am|im|i\s+work\s+as|as)\s+(?:a|an|the)?\s*(?:\w+\s+){0,2}?"


def extract_author_role(text: str | None, roles: Iterable[str]) -> str | None:
    """Return the first role from `roles` the author states about themselves.

    Matches "I'm a paralegal", "as a practice manager", "I work as an estimator",
    and "<role> here". `roles` comes from an industry map's vocabulary.
    """
    if not text:
        return None
    lowered = text.lower()
    for role in sorted({r.lower().strip() for r in roles if r.strip()}, key=len, reverse=True):
        r = re.escape(role)
        if re.search(rf"\b{_ROLE_LEADS}{r}s?\b", lowered) or re.search(rf"\b{r}\s+here\b", lowered):
            return role
    return None
