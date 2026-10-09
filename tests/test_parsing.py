from datetime import datetime, timezone

import pytest

from signal_connectors.parsing import html_to_text, parse_date


@pytest.mark.parametrize("html, text", [
    ("<p>Hello <b>world</b></p><p>Two&amp;three<br>four</p><script>x()</script>", "Hello world\n\nTwo&three\nfour"),
    ("First para.<p>Second para, HN style.<p>Third", "First para.\n\nSecond para, HN style.\n\nThird"),
    ('<a href="/x" hreflang="en">Abridge &amp; UT</a>', "Abridge & UT"),
    ("plain text", "plain text"),
    ("", ""),
    (None, ""),
])
def test_html_to_text(html, text):
    assert html_to_text(html) == text


@pytest.mark.parametrize("value, expected", [
    ("Thu, 09 Oct 2026 10:00:00 -0400", datetime(2026, 10, 9, 14, 0, tzinfo=timezone.utc)),
    ("2026-10-09T10:00:00Z", datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc)),
    ("Oct 9, 2026 11:15am", datetime(2026, 10, 9, 11, 15, tzinfo=timezone.utc)),
    ("October 9, 2026", datetime(2026, 10, 9, tzinfo=timezone.utc)),
    ("not a date", None),
    (None, None),
])
def test_parse_date(value, expected):
    assert parse_date(value) == expected
