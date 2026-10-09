"""Redact personal data from recorded responses before a cassette is written (CLAUDE.md section 10).

Connectors never store these fields, but cassettes keep whole response bodies. JSON bodies have
personal leaf fields replaced with "redacted", and person objects (account, author, creator...)
have their identifying fields replaced. XML/RSS bodies lose author tags and EDGAR's related
persons and street addresses. Fields connectors need to work (ids, DIDs, consent flags,
timestamps, counts) are kept.
"""

from __future__ import annotations

import json
import re
from typing import Any

REDACTED = "redacted"

# Redacted wherever they appear.
PERSONAL_KEYS = {
    "author", "by", "username", "acct", "handle", "display_name", "displayname", "fullname", "full_name",
    "email", "phone", "phone_number", "firstname", "lastname", "first_name", "last_name", "middle_name",
    "avatar", "avatar_static", "profile_image", "bio", "zip_code", "zipcode", "incident_zip", "incident_address",
    "street", "street1", "street2", "street_name", "street_number", "building", "author_flair_text",
    "zip", "city", "address1", "address2", "fax", "submitterrep", "submitterrepaddress", "submitterrepcitystate",
}
# Dropped whole: these hold nothing connectors need.
DROPPED_CONTAINERS = {"pointofcontact", "relatedpersonslist", "patient"}
PERSONAL_PREFIXES = ("contact_", "manufacturer_contact_", "distributor_")
# Inside these objects, also redact their name/link-style fields.
PERSON_CONTAINERS = {"account", "author", "creator", "owner", "user", "person", "mentions", "submitter"}
PERSON_FIELDS = {"name", "url", "link", "note", "fields", "header", "header_static", "actor_id", "uri"}

_XML_TAGS = re.compile(
    r"<(dc:creator|author|managingEditor|relatedPersonsList|street1|street2|zipCode|phoneNumber|issuerPhoneNumber)"
    r"(\s[^>]*)?>.*?</\1>", re.S | re.I)
_AUTHOR_TAG = re.compile(r'"author_[^"]+"')
# Emails inside free text (tender descriptions, dataset notes) are redacted in every body.
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")


def _is_personal(key: str) -> bool:
    k = key.lower()
    return k in PERSONAL_KEYS or k.startswith(PERSONAL_PREFIXES)


def scrub_json(value: Any, in_person: bool = False) -> Any:
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k.lower() in DROPPED_CONTAINERS:
                out[k] = REDACTED
            elif isinstance(v, (dict, list)) and (_is_personal(k) or k.lower() in PERSON_CONTAINERS):
                out[k] = scrub_json(v, in_person=True)       # e.g. Bluesky "author": {did, handle, labels}
            elif _is_personal(k) or (in_person and k.lower() in PERSON_FIELDS):
                out[k] = v if isinstance(v, bool) or v is None else REDACTED
            else:
                out[k] = scrub_json(v, in_person=k.lower() in PERSON_CONTAINERS)
        return out
    if isinstance(value, list):
        return [scrub_json(v, in_person) for v in value]
    return value


def scrub_text(body: str, content_type: str) -> str:
    stripped = body.lstrip()
    if "json" in content_type or stripped[:1] in "[{":
        try:
            data = json.loads(body)
        except ValueError:
            pass
        else:
            return _AUTHOR_TAG.sub('"author_redacted"', json.dumps(scrub_json(data), ensure_ascii=False))
    if "xml" in content_type or "rss" in content_type or stripped.startswith("<"):
        return _XML_TAGS.sub(lambda m: f"<{m.group(1)}{m.group(2) or ''}>{REDACTED}</{m.group(1)}>", body)
    return body


def scrub_body(response: dict[str, Any]) -> dict[str, Any]:
    body = response.get("body") or {}
    raw = body.get("string")
    if not raw:
        return response
    headers = {k.lower(): v for k, v in (response.get("headers") or {}).items()}
    ctype = headers.get("content-type", [""])
    ctype = (ctype[0] if isinstance(ctype, list) else ctype or "").lower()
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    cleaned = _EMAIL.sub("redacted@example.org", scrub_text(text, ctype))
    body["string"] = cleaned.encode("utf-8") if isinstance(raw, bytes) else cleaned
    if "content-length" in headers:
        for k in list(response["headers"]):
            if k.lower() == "content-length":
                response["headers"][k] = [str(len(body["string"] if isinstance(body["string"], bytes)
                                                  else body["string"].encode("utf-8")))]
    return response
