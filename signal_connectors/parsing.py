"""Small parsing helpers shared by connectors: HTML to text, lenient dates."""

from __future__ import annotations

import calendar
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from .models import to_utc

_SPACE_RE = re.compile(r"[ \t\r\f\v]+")
_BLANKS_RE = re.compile(r"\n\s*\n+")

# Formats seen in the wild that RFC 822 / ISO parsers reject, e.g. "Oct 9, 2026 11:15am".
_EXTRA_DATE_FORMATS = (
    "%b %d, %Y %I:%M%p",
    "%B %d, %Y %I:%M%p",
    "%b %d, %Y",
    "%B %d, %Y",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
)


def html_to_text(html: str | None) -> str:
    """Visible text of an HTML fragment, paragraphs kept as blank-line breaks."""
    if not html:
        return ""
    if "<" not in html and "&" not in html:
        return html.strip()
    tree = HTMLParser(html)
    for node in tree.css("script, style, noscript"):
        node.decompose()
    for node in tree.css("br"):
        node.replace_with("\n")
    for node in tree.css("p, div, li, h1, h2, h3, h4, h5, h6, blockquote, tr, pre"):
        node.insert_before("\n\n")  # HN-style "<p>" used as a separator has no closing tag
        node.insert_after("\n\n")
    root = tree.body or tree.root
    text = root.text(separator="") if root else ""
    text = _SPACE_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANKS_RE.sub("\n\n", text).strip()


def struct_to_datetime(value: time.struct_time | None) -> datetime | None:
    """feedparser's *_parsed fields are UTC struct_times."""
    if not value:
        return None
    return datetime.fromtimestamp(calendar.timegm(value), tz=timezone.utc)


def parse_date(value: str | None) -> datetime | None:
    """Parse RFC 822, ISO 8601 or a few common loose formats. Naive results are taken as UTC."""
    if not value:
        return None
    value = value.strip()
    try:
        return to_utc(parsedate_to_datetime(value))
    except (TypeError, ValueError, IndexError):
        pass
    try:
        return to_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        pass
    normalized = re.sub(r"\s*(am|pm)\b", lambda m: m.group(1).upper(), value, flags=re.I)
    for fmt in _EXTRA_DATE_FORMATS:
        try:
            return datetime.strptime(normalized, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None
