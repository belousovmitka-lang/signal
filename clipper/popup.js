import {readSelection, saveClip, settings} from "./common.js";

const $ = id => document.getElementById(id);
let page = null;

function status(text, cls) { $("status").textContent = text; $("status").className = cls || ""; }

$("opts").addEventListener("click", e => { e.preventDefault(); chrome.runtime.openOptionsPage(); });

async function load() {
  const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
  try {
    const [{result}] = await chrome.scripting.executeScript({target: {tabId: tab.id}, func: readSelection});
    page = result;
  } catch {
    page = {url: tab.url, title: tab.title || null, text: ""};   // pages scripts can't run on (chrome://, the store)
  }
  if (!/^https?:/.test(page.url || "")) {
    $("save").disabled = true;
    status("This page can't be clipped.", "bad");
  }
  $("page").textContent = page.title || page.url;
  $("page").title = page.url;
  $("text").value = page.text || "";
  if (!(await settings()).userId) status("Set your Signal user id in Settings first.", "bad");
}

$("save").addEventListener("click", async () => {
  $("save").disabled = true;
  status("Saving…");
  try {
    const clip = await saveClip({url: page.url, title: page.title, text: $("text").value, note: $("note").value});
    status(clip.new ? "Saved to Signal." : "Updated your earlier clip.", "ok");
    setTimeout(() => window.close(), 900);
  } catch (err) {
    status(err.message === "Failed to fetch" ? "Signal isn't reachable. Is the clip server running?" : err.message, "bad");
    $("save").disabled = false;
  }
});

load();
