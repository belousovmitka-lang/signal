// Shared by the popup, the right-click menu and the options page.
export const DEFAULTS = {server: "http://127.0.0.1:8765", userId: ""};

export async function settings() {
  return {...DEFAULTS, ...(await chrome.storage.sync.get(["server", "userId"]))};
}

// Runs inside the page, only when the user clicks: the selection, the title and the page's own link.
// Nothing else is read from the page (no author names, profiles or other posts).
export function readSelection() {
  const canonical = document.querySelector('link[rel="canonical"]')?.href;
  return {
    text: String(window.getSelection() || "").trim(),
    title: document.title || null,
    url: canonical && /^https?:/.test(canonical) ? canonical : location.href,
  };
}

export async function saveClip(clip) {
  const {server, userId} = await settings();
  if (!userId) throw new Error("Set your Signal user id first (right-click the button → Options).");
  const res = await fetch(`${server.replace(/\/$/, "")}/clips`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({...clip, user_id: userId}),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = Array.isArray(body.detail) ? body.detail.map(d => d.msg).join("; ") : body.detail;
    throw new Error(detail || `Signal answered HTTP ${res.status}`);
  }
  return body;
}
