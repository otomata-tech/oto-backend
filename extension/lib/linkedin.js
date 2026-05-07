// LinkedIn cookie capture + push to oto-mcp.

import { pushLinkedinSession } from "./api.js";

const LINKEDIN_DOMAIN = ".linkedin.com";
const COOKIE_NAME = "li_at";

export async function readLiAt() {
  // chrome.cookies returns the most permissive matching cookie. Filter manually
  // to .linkedin.com (host_permissions don't allow url filter to mismatch).
  const cookies = await chrome.cookies.getAll({ name: COOKIE_NAME });
  const li = cookies.find(c => c.domain.endsWith(LINKEDIN_DOMAIN));
  return li ? li.value : null;
}

// LinkedIn flags sessions where the cookie's UA differs from the request UA.
// chrome.userAgentData isn't precise enough — we ask the active linkedin.com tab
// for navigator.userAgent instead. Fallback: any tab.
async function captureUserAgent() {
  const tabs = await chrome.tabs.query({ url: "*://*.linkedin.com/*" });
  const tab = tabs.find(t => t.active) || tabs[0];
  if (!tab?.id) return null;
  try {
    const [{ result }] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: () => navigator.userAgent,
    });
    return result || null;
  } catch {
    return null;
  }
}

export async function syncSession({ silent = false } = {}) {
  const cookie = await readLiAt();
  if (!cookie) {
    const err = new Error("Pas connecté à LinkedIn (cookie li_at introuvable).");
    err.code = "no_cookie";
    throw err;
  }
  const ua = await captureUserAgent();
  const result = await pushLinkedinSession({ cookie, user_agent: ua || undefined });
  return { ok: true, hasUa: Boolean(ua), silent, ...result };
}
