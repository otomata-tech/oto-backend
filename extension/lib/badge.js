// Action icon badge — green when on linkedin.com and signed in to extension.

import { isAuthenticated } from "./auth.js";
import { readLiAt } from "./linkedin.js";

const COLOR_OK = "#16a34a";       // green-600
const COLOR_WARN = "#f59e0b";     // amber-500
const COLOR_OFF = "#9ca3af";      // gray-400

async function isOnLinkedin(tabId) {
  if (!tabId) return false;
  try {
    const tab = await chrome.tabs.get(tabId);
    return Boolean(tab?.url && /^https?:\/\/[^/]*linkedin\.com\//.test(tab.url));
  } catch {
    return false;
  }
}

export async function refreshBadge(tabId) {
  const onLi = await isOnLinkedin(tabId);
  if (!onLi) {
    await chrome.action.setBadgeText({ text: "" });
    return;
  }
  const authed = await isAuthenticated();
  if (!authed) {
    await chrome.action.setBadgeBackgroundColor({ color: COLOR_OFF });
    await chrome.action.setBadgeText({ text: "off" });
    return;
  }
  const cookie = await readLiAt();
  if (!cookie) {
    await chrome.action.setBadgeBackgroundColor({ color: COLOR_WARN });
    await chrome.action.setBadgeText({ text: "?" });
    return;
  }
  await chrome.action.setBadgeBackgroundColor({ color: COLOR_OK });
  await chrome.action.setBadgeText({ text: "OK" });
}
