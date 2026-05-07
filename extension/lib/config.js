// Endpoints + Logto app config. Override via chrome.storage.local["config"]
// for dev (e.g. point to localhost MCP).

export const DEFAULT_CONFIG = {
  logtoEndpoint: "https://auth.oto.zone",
  logtoAppId: "",  // filled by user via popup or build-time
  logtoResource: "https://mcp.oto.ninja/mcp",
  logtoScopes: ["openid", "profile", "email", "offline_access"],
  mcpBase: "https://mcp.oto.ninja",
};

export async function getConfig() {
  const stored = await chrome.storage.local.get("config");
  return { ...DEFAULT_CONFIG, ...(stored.config || {}) };
}

export async function setConfig(patch) {
  const current = (await chrome.storage.local.get("config")).config || {};
  await chrome.storage.local.set({ config: { ...current, ...patch } });
}

export function redirectUri() {
  // Chrome derives this from extension ID; deterministic if manifest "key" is set.
  return chrome.identity.getRedirectURL();
}
