import {saveClip} from "./common.js";

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({id: "clip", title: "Clip to Signal", contexts: ["selection"]});
});

async function flash(tabId, text, color) {
  await chrome.action.setBadgeBackgroundColor({tabId, color});
  await chrome.action.setBadgeText({tabId, text});
  setTimeout(() => chrome.action.setBadgeText({tabId, text: ""}), 4000);
}

// Right-click on selected text: save it straight away, with the page's link and title.
chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId !== "clip" || !tab) return;
  try {
    await saveClip({url: info.pageUrl || tab.url, title: tab.title || null, text: info.selectionText || ""});
    await flash(tab.id, "✓", "#2e7d32");
  } catch (err) {
    console.warn("Clip to Signal:", err);
    await flash(tab.id, "!", "#c62828");
  }
});
