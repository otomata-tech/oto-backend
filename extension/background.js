// MV3 service worker. Routes messages from popup, watches LinkedIn cookie
// changes for auto-resync, refreshes badge on tab activity.

import { isAuthenticated, login, logout, getUserInfo } from "./lib/auth.js";
import { syncSession } from "./lib/linkedin.js";
import { clearLinkedinSession, getMe } from "./lib/api.js";
import { refreshBadge } from "./lib/badge.js";

const HANDLERS = {
  async login() {
    const user = await login();
    await refreshBadgeForActiveTab();
    return { ok: true, user };
  },
  async logout() {
    await logout();
    await refreshBadgeForActiveTab();
    return { ok: true };
  },
  async authStatus() {
    const authed = await isAuthenticated();
    const user = authed ? await getUserInfo() : null;
    return { authed, user };
  },
  async me() {
    return await getMe();
  },
  async sync() {
    const result = await syncSession();
    await refreshBadgeForActiveTab();
    return result;
  },
  async clear() {
    await clearLinkedinSession();
    await refreshBadgeForActiveTab();
    return { ok: true };
  },
};

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  const handler = HANDLERS[msg?.type];
  if (!handler) {
    sendResponse({ error: `unknown message type: ${msg?.type}` });
    return false;
  }
  handler(msg)
    .then(result => sendResponse({ result }))
    .catch(err => sendResponse({ error: err?.message || String(err) }));
  return true;  // async
});

async function refreshBadgeForActiveTab() {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  await refreshBadge(tab?.id);
}

chrome.tabs.onActivated.addListener(({ tabId }) => refreshBadge(tabId));
chrome.tabs.onUpdated.addListener((tabId, info) => {
  if (info.status === "complete" || info.url) refreshBadge(tabId);
});

// Auto-resync when LinkedIn rotates the li_at cookie. Only fires when user is
// already logged in to the extension, otherwise silent.
chrome.cookies.onChanged.addListener(async ({ cookie, removed }) => {
  if (cookie.name !== "li_at") return;
  if (!cookie.domain.endsWith(".linkedin.com")) return;
  if (removed) return;
  if (!(await isAuthenticated())) return;
  try {
    await syncSession({ silent: true });
  } catch {
    // swallow — popup will surface on next manual action
  }
  await refreshBadgeForActiveTab();
});

chrome.runtime.onInstalled.addListener(() => refreshBadgeForActiveTab());
chrome.runtime.onStartup.addListener(() => refreshBadgeForActiveTab());
