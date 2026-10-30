"""Send a sample sign-in email with the SMTP settings in .env, to check them before people sign in.

    python scripts/send_test_email.py you@example.com

The link in it is a dummy that signs no one in. Without SMTP_HOST the email goes to the log instead.
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from signal_feed.auth import LogMailer, link_email, mailer_from_env, normalize_email  # noqa: E402
from signal_feed.settings import REPO_ROOT  # noqa: E402


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    load_dotenv(REPO_ROOT / ".env")
    to = normalize_email(sys.argv[1])
    mailer = mailer_from_env()
    if isinstance(mailer, LogMailer):
        print("SMTP_HOST isn't set in .env, so nothing is sent. See .env.example (SMTP_*).")
        return 1
    text, html = link_email("https://example.invalid/auth/callback?token=test-only", 15)
    try:
        mailer.send(to, "Signal test email", text, html=html)
    except Exception as exc:          # show the SMTP server's own answer: it says what's wrong
        print(f"Not sent: {type(exc).__name__}: {exc}")
        return 1
    print(f"Sent to {to} via {mailer.host}:{mailer.port}. Check the inbox (and spam).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
