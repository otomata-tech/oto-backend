// Authenticated calls to the oto-mcp REST API.

import { getConfig } from "./config.js";
import { getAccessToken } from "./auth.js";

async function authedFetch(path, init = {}) {
  const cfg = await getConfig();
  const token = await getAccessToken();
  const resp = await fetch(`${cfg.mcpBase}${path}`, {
    ...init,
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
      ...(init.headers || {}),
    },
  });
  return resp;
}

export async function getMe() {
  const resp = await authedFetch("/api/me");
  if (!resp.ok) throw new Error(`GET /api/me ${resp.status}`);
  return await resp.json();
}

export async function pushLinkedinSession({ cookie, user_agent }) {
  const resp = await authedFetch("/api/settings/linkedin", {
    method: "POST",
    body: JSON.stringify({ cookie, user_agent }),
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`POST /api/settings/linkedin ${resp.status}: ${text}`);
  }
  return await resp.json();
}

export async function clearLinkedinSession() {
  const resp = await authedFetch("/api/settings/linkedin", { method: "DELETE" });
  if (!resp.ok) throw new Error(`DELETE /api/settings/linkedin ${resp.status}`);
  return await resp.json();
}
