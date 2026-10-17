import {DEFAULTS, settings} from "./common.js";

const $ = id => document.getElementById(id);
const s = await settings();
$("userId").value = s.userId;
$("server").value = s.server;

$("save").addEventListener("click", async () => {
  const server = ($("server").value.trim() || DEFAULTS.server).replace(/\/$/, "");
  const userId = $("userId").value.trim();
  const status = (text, cls) => { $("status").textContent = text; $("status").className = cls; };
  let url;
  try { url = new URL(server); } catch { return status("The server must be a full URL.", "bad"); }
  const local = ["127.0.0.1", "localhost"].includes(url.hostname);
  if (!local) {
    if (url.protocol !== "https:") return status("A remote server must use https.", "bad");
    // Ask for access to that one server only (requires the click that triggered this handler).
    const granted = await chrome.permissions.request({origins: [`${url.origin}/*`]});
    if (!granted) return status("Chrome needs permission to reach that server.", "bad");
  }
  await chrome.storage.sync.set({server, userId});
  status(userId ? "Saved." : "Saved. Add your user id to start clipping.", userId ? "ok" : "bad");
});
