"""Strip personal data from model-written fields before storage (section 3.2).

The prompt already forbids names and contact details; this is the backstop for the ones that slip
through: emails, phone numbers, @handles and profile-style URLs.
"""

from __future__ import annotations

import re

_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_PHONE = re.compile(r"(?<![\w$€£.,])\+?\d[\d ()\-.]{7,}\d(?![\w%])")
_HANDLE = re.compile(r"(?<![\w@])@[A-Za-z0-9_]{2,30}\b")
_URL = re.compile(r"https?://\S+")


def scrub(text: str | None) -> str | None:
    if text is None:
        return None
    text = _EMAIL.sub("[email]", text)
    text = _URL.sub("[link]", text)
    text = _HANDLE.sub("[user]", text)
    text = _PHONE.sub(lambda m: "[phone]" if sum(c.isdigit() for c in m.group()) >= 9 else m.group(), text)
    return text
