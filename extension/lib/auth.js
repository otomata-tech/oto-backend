// Logto OIDC PKCE flow for Chrome extensions.
//
// Login = launchWebAuthFlow → /oidc/auth (PKCE S256, with audience param so the
// access token is targeted at the MCP resource). Tokens stored in
// chrome.storage.local. Refresh on demand using offline_access refresh token.
//
// Logto self-hosted signs ES384 — we don't verify locally, the backend does.
//
// Robustness :
// - **Single-flight refresh** (`_refreshInflight`) coalesce les appels
//   concurrents. Sans ça, un popup qui ouvre `me` + `whatsappStatus` en
//   parallèle pendant que le cookie watcher en background tente aussi un
//   `getAccessToken()` se retrouve avec 3 POST /oidc/token simultanés ; Logto
//   rotate les refresh tokens donc le 2ème et 3ème échouent en 400 et
//   l'utilisateur perd ses tokens pour rien.
// - **Silent re-auth** (`prompt=none`) en cas d'échec du refresh : si la
//   session SSO de auth.oto.zone est encore vivante, on récupère
//   transparemment un nouveau triplet (access, refresh, id) sans UI.
//   Si Logto répond `login_required`, on lève alors une erreur
//   `SESSION_EXPIRED` propre que le popup transforme en "reconnexion".

import { getConfig, redirectUri } from "./config.js";
import { makePkcePair, randomState } from "./pkce.js";

const TOKENS_KEY = "tokens";
const PKCE_KEY = "pkce_state";

// Sentinel utilisé par la UI pour distinguer "vraie" expiration d'une erreur transitoire.
export const SESSION_EXPIRED = "SESSION_EXPIRED";

let _refreshInflight = null;

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
  return await runAuthFlow({ interactive: true });
}

// Flow OIDC PKCE générique. `interactive=false` + `prompt=none` = silent SSO :
// si Logto a une session active il renvoie un code direct, sinon le launch
// échoue/expire sans afficher d'UI. Renvoie l'identité (sub/email/name).
async function runAuthFlow({ interactive, prompt }) {
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
  if (prompt) params.set("prompt", prompt);

  const authUrl = `${auth}?${params.toString()}`;
  let responseUrl;
  try {
    responseUrl = await chrome.identity.launchWebAuthFlow({
      url: authUrl,
      interactive: !!interactive,
    });
  } catch (e) {
    // En non-interactif, l'API jette si Logto a besoin d'UI — c'est attendu.
    throw new Error(interactive ? (e?.message || String(e)) : SESSION_EXPIRED);
  }
  if (!responseUrl) {
    throw new Error(interactive ? "Login annulé." : SESSION_EXPIRED);
  }

  const url = new URL(responseUrl);
  const code = url.searchParams.get("code");
  const returnedState = url.searchParams.get("state");
  const error = url.searchParams.get("error");
  if (error) {
    if (!interactive && (error === "login_required" || error === "interaction_required")) {
      throw new Error(SESSION_EXPIRED);
    }
    throw new Error(`OIDC error: ${error}`);
  }
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
  // Single-flight : si un refresh est déjà en cours, on attend son résultat
  // au lieu de poster un 2ème POST /oidc/token avec le même refresh_token
  // (que Logto vient de marquer comme consommé via la rotation).
  if (_refreshInflight) return _refreshInflight;
  _refreshInflight = _doRefresh().finally(() => {
    _refreshInflight = null;
  });
  return _refreshInflight;
}

async function _doRefresh() {
  const tokens = await getTokens();
  if (!tokens?.refresh_token) {
    // Pas de refresh_token mais la session SSO peut encore exister côté Logto.
    return await _silentReauthAccessToken();
  }
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
  if (resp.ok) {
    const data = await resp.json();
    if (!data.refresh_token) data.refresh_token = tokens.refresh_token;
    await setTokens(data);
    return data.access_token;
  }
  // Refresh KO — rotation race, refresh expiré, ou scopes/resource désalignés.
  // On tente le silent re-auth avant de capituler.
  return await _silentReauthAccessToken();
}

async function _silentReauthAccessToken() {
  try {
    await runAuthFlow({ interactive: false, prompt: "none" });
    const tokens = await getTokens();
    if (tokens?.access_token) return tokens.access_token;
    throw new Error(SESSION_EXPIRED);
  } catch (e) {
    await clearTokens();
    throw new Error(SESSION_EXPIRED);
  }
}

export async function getAccessToken() {
  const tokens = await getTokens();
  if (!tokens?.access_token) throw new Error(SESSION_EXPIRED);
  if (tokens.expires_at && Date.now() >= tokens.expires_at) {
    return await refreshTokens();
  }
  return tokens.access_token;
}

export async function logout() {
  // Best-effort revoke (Logto supports /oidc/session/end via GET redirect).
  await clearTokens();
}
