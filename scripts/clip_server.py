"""Local server for the "Clip to Signal" browser button, until the app mounts clips.router itself.

    python scripts/clip_server.py              # http://127.0.0.1:8765, stores clips in signal.db
    open http://127.0.0.1:8765/?user_id=<id>   # your clips, newest first

Listens on 127.0.0.1 only: there's no login yet, so it must not be reachable from other machines.
"""

from __future__ import annotations

import argparse
import html
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from signal_connectors.clips import list_clips, router  # noqa: E402
from signal_connectors.settings import Settings  # noqa: E402
from signal_connectors.store import Store  # noqa: E402


def make_app(store: Store):
    from fastapi import FastAPI, Query
    from fastapi.responses import HTMLResponse

    app = FastAPI(title="Clip to Signal (local)")
    app.include_router(router(store))

    @app.get("/", response_class=HTMLResponse)
    def page(user_id: str = Query("me", min_length=1, max_length=64)) -> str:
        items = "".join(
            f"<li><a href='{html.escape(c.url)}'>{html.escape(c.title or c.url)}</a> <small>{html.escape(c.site)} · "
            f"{c.created_at[:16].replace('T', ' ')}</small><blockquote>{html.escape(c.text[:600])}</blockquote>"
            + (f"<p><b>Note:</b> {html.escape(c.note)}</p>" if c.note else "") + "</li>"
            for c in list_clips(store, user_id))
        return (f"<!doctype html><meta charset=utf-8><title>Your clips</title>"
                f"<style>body{{font:15px system-ui;max-width:760px;margin:24px auto;padding:0 16px}}"
                f"blockquote{{color:#444;border-left:3px solid #ccc;margin:6px 0;padding-left:10px}}"
                f"li{{margin-bottom:18px}}</style><h1>Clips for {html.escape(user_id)}</h1>"
                f"<ol>{items or '<p>No clips yet.</p>'}</ol>")

    return app


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=8765)
    args = p.parse_args()
    import uvicorn

    uvicorn.run(make_app(Store(Settings.from_env().database_url)), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
