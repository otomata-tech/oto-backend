// Logto OIDC PKCE flow for Chrome extensions.
//
// Login = launchWebAuthFlow → /oidc/auth (PKCE S256, with audience param so the
// access token is targeted at the MCP resource). Tokens stored in
// chrome.storage.local. Refresh on demand using offline_access refresh token.
//
// Logto self-hosted signs ES384 — we don't verify locally, the backend does.

import { getConfig, redirectUri } from "./config.js";
import { makePkcePair, randomState } from "./pkce.js";

const TOKENS_KEY = "tokens";
const PKCE_KEY = "pkce_state";

async function discovery() {
  const cfg = await getConfig();
  return {
    auth: `${cfg.logtoEndpoint}/oidc/auth`,
    token: `${cfg.logtoEndpoint}/oidc/token`,
    end_session: `${cfg.logtoEndpoint}/oidc/session/end`,
    cfg,
  };
}

async function setTokens(tokens) {
  const expires_at = tokens.expires_in
    ? Date.now() + (tokens.expires_in - 30) * 1000
    : null;
  await chrome.storage.local.set({
    [TOKENS_KEY]: { ...tokens, expires_at },
  });
}

async function getTokens() {
  return (await chrome.storage.local.get(TOKENS_KEY))[TOKENS_KEY] || null;
}

export async function clearTokens() {
  await chrome.storage.local.remove([TOKENS_KEY, PKCE_KEY]);
}

export async function getUserInfo() {
  const tokens = await getTokens();
  if (!tokens?.id_token) return null;
  // Decode JWT payload — no verification, just for popup display.
  try {
    const payload = tokens.id_token.split(".")[1];
    const json = atob(payload.replace(/-/g, "+").replace(/_/g, "/"));
    const claims = JSON.parse(decodeURIComponent(escape(json)));
    return { sub: claims.sub, email: claims.email, name: claims.name };
  } catch {
    return null;
  }
}

export async function isAuthenticated() {
  const tokens = await getTokens();
  return Boolean(tokens?.access_token);
}

export async function login() {
  const { auth, cfg } = await discovery();
  if (!cfg.logtoAppId) {
    throw new Error("Logto appId non configuré. Ouvre l'option de l'extension pour le saisir.");
  }
  const { verifier, challenge } = await makePkcePair();
  const state = randomState(16);
  const ru = redirectUri();
  await chrome.storage.local.set({ [PKCE_KEY]: { verifier, state, redirect_uri: ru } });

  const params = new URLSearchParams({
    client_id: cfg.logtoAppId,
    redirect_uri: ru,
    response_type: "code",
    scope: cfg.logtoScopes.join(" "),
    resource: cfg.logtoResource,
    code_challenge: challenge,
    code_challenge_method: "S256",
    state,
  });

  const authUrl = `${auth}?${params.toString()}`;
  const responseUrl = await chrome.identity.launchWebAuthFlow({
    url: authUrl,
    interactive: true,
  });
  if (!responseUrl) throw new Error("Login annulé.");

  const url = new URL(responseUrl);
  const code = url.searchParams.get("code");
  const returnedState = url.searchParams.get("state");
  const error = url.searchParams.get("error");
  if (error) throw new Error(`OIDC error: ${error}`);
  if (!code) throw new Error("Pas de code OIDC dans la réponse.");
  if (returnedState !== state) throw new Error("State mismatch (CSRF).");

  await exchangeCode(code, verifier, ru);
  await chrome.storage.local.remove(PKCE_KEY);
  return await getUserInfo();
}

async function exchangeCode(code, verifier, ru) {
  const { token, cfg } = await discovery();
  const body = new URLSearchParams({
    grant_type: "authorization_code",
    code,
    redirect_uri: ru,
    client_id: cfg.logtoAppId,
    code_verifier: verifier,
    resource: cfg.logtoResource,
  });
  const resp = await fetch(token, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: body.toString(),
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`Token exchange failed (${resp.status}): ${text}`);
  }
  const data = await resp.json();
  await setTokens(data);
}

async function refreshTokens() {
  const tokens = await getTokens();
  if (!tokens?.refresh_token) throw new Error("Pas de refresh_token, relance le login.");
  const { token, cfg } = await discovery();
  const body = new URLSearchParams({
    grant_type: "refresh_token",
    refresh_token: tokens.refresh_token,
    client_id: cfg.logtoAppId,
    resource: cfg.logtoResource,
    scope: cfg.logtoScopes.join(" "),
  });
  const resp = await fetch(token, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: body.toString(),
  });
  if (!resp.ok) {
    await clearTokens();
    throw new Error(`Refresh failed (${resp.status}). Relance le login.`);
  }
  const data = await resp.json();
  // Logto rotates refresh tokens — keep new one, fall back to old if absent.
  if (!data.refresh_token) data.refresh_token = tokens.refresh_token;
  await setTokens(data);
  return data.access_token;
}

export async function getAccessToken() {
  const tokens = await getTokens();
  if (!tokens?.access_token) throw new Error("Non connecté.");
  if (tokens.expires_at && Date.now() >= tokens.expires_at) {
    return await refreshTokens();
  }
  return tokens.access_token;
}

export async function logout() {
  // Best-effort revoke (Logto supports /oidc/session/end via GET redirect).
  await clearTokens();
}
