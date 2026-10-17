# Clip to Signal

A Chrome extension that saves a post you're reading to your Signal workspace. It sends only the page's
link and title, the text you select, and your own note. It reads nothing else from the page, and runs
only when you click it.

## Try it locally

1. Start the local clip server: `python scripts/clip_server.py` (stores clips in `data/signal.db`).
2. Chrome → `chrome://extensions` → turn on Developer mode → **Load unpacked** → pick this `clipper/` folder.
3. Right-click the Signal button → **Options** → enter a user id (any name for now) → Save.
4. On any post, select the part that matters, then click the button (or right-click → **Clip to Signal**).
5. See your clips at `http://127.0.0.1:8765/?user_id=<your id>`.

The app can serve the same routes itself: `app.include_router(signal_connectors.clips.router(store))`.
