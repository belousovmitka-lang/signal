"""URL canonicalization and content hashing used for deduplication."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query parameters that only track the click and never change the content.
TRACKING_PARAMS = frozenset({
    "fbclid", "gclid", "dclid", "gbraid", "wbraid", "msclkid", "yclid", "twclid", "ttclid",
    "igshid", "igsh", "mkt_tok", "ref", "ref_src", "ref_url", "referrer", "share_id",
    "spm", "si", "_ga", "_gl", "cmpid", "s_cid", "trk", "trackingid", "rss", "feature",
})
TRACKING_PREFIXES = ("utm_", "mc_", "pk_", "hsa_", "_hs", "vero_", "oly_")
DEFAULT_PORTS = {"http": 80, "https": 443}

# Texts shorter than this are too generic to treat as cross-posts ("[deleted]", "Thanks!").
MIN_TEXT_HASH_CHARS = 40


# Parameters that are tracking only for some values (Medium: source=rss-..., source=post_page-...).
_TRACKING_VALUES = {"source": ("rss", "post_page", "collection_home", "user_profile")}


def _is_tracking(key: str, value: str = "") -> bool:
    k = key.lower()
    if k in _TRACKING_VALUES and value.lower().startswith(_TRACKING_VALUES[k]):
        return True
    return k in TRACKING_PARAMS or k.startswith(TRACKING_PREFIXES)


def canonical_url(url: str) -> str:
    """Normalize a URL so that the same page always produces the same string.

    Lowercases scheme and host, drops credentials, default ports, fragments and
    tracking parameters, sorts the remaining query parameters and removes a
    trailing slash from non-root paths.
    """
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower().rstrip(".")
    if scheme not in {"http", "https"} or not host:
        raise ValueError(f"not an absolute http(s) URL: {url!r}")
    netloc = host
    if parts.port and parts.port != DEFAULT_PORTS[scheme]:
        netloc = f"{host}:{parts.port}"
    path = re.sub(r"/{2,}", "/", parts.path) or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"
    query = sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _is_tracking(k, v))
    return urlunsplit((scheme, netloc, path, urlencode(query), ""))


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def url_hash(url: str) -> str:
    return sha256(canonical_url(url))


_URL_RE = re.compile(r"https?://\S+")
_NON_WORD_RE = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """Fold case, width and punctuation and drop links so cross-posts compare equal."""
    text = unicodedata.normalize("NFKC", text).casefold()
    text = _URL_RE.sub(" ", text)
    text = _NON_WORD_RE.sub(" ", text)
    return _SPACE_RE.sub(" ", text).strip()


def text_hash(text: str | None) -> str | None:
    """Hash of the normalized text, or None when the text is too short to be distinctive."""
    if not text:
        return None
    normalized = normalize_text(text)
    if len(normalized) < MIN_TEXT_HASH_CHARS:
        return None
    return sha256(normalized)
