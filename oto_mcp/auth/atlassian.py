"""Atlassian OAuth — flow web per-user pour fédérer le Rovo Remote MCP (#40).

Atlassian héberge un MCP distant (mcp.atlassian.com/v1/mcp, Jira + Confluence)
avec SON PROPRE serveur d'autorisation OAuth 2.1 (RFC 8414), DCR + PKCE.
Découverte live (2026-06-22, `.well-known/oauth-authorization-server`) :
  authorization_endpoint : https://mcp.atlassian.com/v1/authorize
  token_endpoint         : https://cf.mcp.atlassian.com/v1/token
  registration_endpoint  : https://cf.mcp.atlassian.com/v1/register  (DCR)
  PKCE S256, grants authorization_code + refresh_token.

Client **public** : la DCR rend `token_endpoint_auth_method=none` (pas de
client_secret) → échange/refresh en `client_id` + `code_verifier`, SANS Basic
auth (client public, pas de secret). Le `client_id` est enregistré une fois par
DCR et fourni via `ATLASSIAN_OAUTH_CLIENT_ID`. La sélection du site Atlassian
Cloud (cloudid) est gérée par l'AS Atlassian — rien à porter côté oto.

Comme google : le refresh_token (long-lived) est le `secret` chiffré du
coffre ; l'access_token (bearer ~1h, dérivé) vit dans `meta` et est rafraîchi de
façon transparente. Le proxy de tools/mount.py l'injecte par requête
(access.resolve_mount_token → access_token_for).
"""
from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Optional

from .. import credentials_store
from . import pkce as oauth2_pkce, flow as oauth_flow
from ..connectors import flow as connector_flow
from ..connectors import health as connector_health
from ..connectors import link as connector_link

_AUTH_URL = "https://mcp.atlassian.com/v1/authorize"
_TOKEN_URL = "https://cf.mcp.atlassian.com/v1/token"
_REGISTER_URL = "https://cf.mcp.atlassian.com/v1/register"
_MCP_RESOURCE = "https://mcp.atlassian.com/v1/mcp"
_CONNECTOR = "atlassian"
# Entité « plateforme » du coffre où l'on cache le client_id DCR (public, pas un
# secret) — auto-enregistré une fois, partagé par tous les users.
_CLIENT_ENTITY = ("platform", "")
_STATE_TTL = 600  # 10 min
# offline_access = condition du refresh_token. Les scopes d'outils (jira/confluence)
# sont consentis via l'AS Atlassian ; surchargeable une fois le flow validé en live.
_DEFAULT_SCOPE = "offline_access"


def _scope() -> str:
    return os.environ.get("ATLASSIAN_OAUTH_SCOPE", _DEFAULT_SCOPE)


def _register_client() -> str:
    """DCR d'un client PUBLIC sur l'AS Atlassian (token_endpoint_auth_method=none,
    pas de secret) → renvoie le client_id. Le redirect_uri DOIT matcher au callback."""
    import requests
    r = requests.post(_REGISTER_URL, json={
        "client_name": "Oto",
        "redirect_uris": [_redirect_uri()],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "scope": _scope(),
    }, timeout=15)
    r.raise_for_status()
    cid = r.json().get("client_id")
    if not cid:
        raise RuntimeError("DCR Atlassian sans client_id")
    return cid


def _client_id() -> str:
    """client_id OAuth — ZÉRO env requise. Override `ATLASSIAN_OAUTH_CLIENT_ID` si
    posée ; sinon cache coffre (entité plateforme) ; sinon **auto-DCR** (client
    public) puis cache. La DB gouverne — pas de provisioning manuel."""
    env = os.environ.get("ATLASSIAN_OAUTH_CLIENT_ID")
    if env:
        return env
    cached = credentials_store.get_credential(*_CLIENT_ENTITY, _CONNECTOR)
    if cached:
        return cached
    cid = _register_client()
    credentials_store.set_credential(*_CLIENT_ENTITY, _CONNECTOR, secret=cid, set_by="system")
    return cid


def reset_client_id() -> None:
    """Purge le client_id caché → re-DCR au prochain `_client_id()`. À appeler si
    l'AS rejette le client (`invalid_client` : registration purgée côté Atlassian)."""
    credentials_store.clear_credential(*_CLIENT_ENTITY, _CONNECTOR)


def _state_secret() -> bytes:
    v = os.environ.get("OTO_MCP_OAUTH_STATE_SECRET")
    if not v:
        raise RuntimeError("OTO_MCP_OAUTH_STATE_SECRET env var manquante")
    return v.encode()


def _redirect_uri() -> str:
    base = os.environ.get("OTO_MCP_PUBLIC_URL", "https://mcp.oto.ninja").rstrip("/")
    return f"{base}/api/atlassian/oauth/callback"


def build_auth_url(sub: str) -> str:
    from urllib.parse import urlencode
    verifier, challenge = oauth2_pkce.pkce_pair()
    params = {
        "client_id": _client_id(),
        "redirect_uri": _redirect_uri(),
        "response_type": "code",
        "scope": _scope(),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": oauth2_pkce.make_state(_state_secret(), sub, verifier),
    }
    return f"{_AUTH_URL}?{urlencode(params)}"


def verify_state(state: str) -> Optional[tuple[str, str]]:
    """(sub, code_verifier) si le state est valide et non expiré, sinon None."""
    return oauth2_pkce.verify_state(_state_secret(), state, _STATE_TTL)


# --- échange + persistance --------------------------------------------------

def exchange_code(code: str, verifier: str) -> dict:
    import requests
    # Client public → pas de Basic auth ; `client_id` + `code_verifier` dans le corps.
    r = requests.post(
        _TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _redirect_uri(),
            "code_verifier": verifier,
            "client_id": _client_id(),
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def persist_token(sub: str, token_response: dict) -> None:
    refresh_token = token_response.get("refresh_token")
    if not refresh_token:
        raise RuntimeError(
            "Atlassian n'a pas émis de refresh_token (vérifie le scope offline_access).")
    credentials_store.set_credential(
        "user", sub, _CONNECTOR, secret=refresh_token, set_by=sub,
        meta={
            "access_token": token_response.get("access_token"),
            "expires_at": oauth2_pkce.expires_at(token_response.get("expires_in")),
        },
    )


class AtlassianReauthRequired(Exception):
    """Refresh token Atlassian mort (invalid_grant) → l'user doit reconnecter."""


def _refresh(refresh_token: str) -> dict:
    import requests
    r = requests.post(
        _TOKEN_URL,
        data={"grant_type": "refresh_token", "refresh_token": refresh_token,
              "client_id": _client_id()},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    # `invalid_grant` SEUL vaut « réauth » : l'appelant PURGE le credential sur cette
    # exception. Un autre 4xx (invalid_client…) est un incident de config — il doit
    # remonter, pas effacer un refresh_token valide. Règle unique : `oauth_flow`.
    body = (r.text or "")[:300]
    if r.status_code in (400, 401) and oauth_flow.grant_is_dead(r.status_code, body):
        raise AtlassianReauthRequired(body)
    r.raise_for_status()
    return r.json()


def access_token_for(sub: str) -> Optional[str]:
    """Access token Atlassian valide pour ce sub (refresh transparent si expiré),
    ou None si le user n'a pas connecté Atlassian."""
    cred = credentials_store.get_credential_with_meta("user", sub, _CONNECTOR)
    if not cred or not cred.get("secret"):
        return None
    meta = cred.get("meta") or {}
    access_token = meta.get("access_token")
    expires_at = meta.get("expires_at")
    needs_refresh = not access_token
    if not needs_refresh and expires_at:
        try:
            exp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if exp.timestamp() - time.time() < 60:  # 60s d'avance
                needs_refresh = True
        # noqa: SILENT — credential illisible ⇒ refresh forcé, jamais un jeton périmé servi
        except Exception:
            needs_refresh = True
    if needs_refresh:
        try:
            resp = _refresh(cred["secret"])
        except AtlassianReauthRequired as e:
            # Grant mort : NE PURGE PLUS le credential (oto#25 lot a) — effacer la
            # ligne rendait « révoqué » indiscernable de « jamais posé », un repli qui
            # masque un problème plutôt que de le nommer. On MARQUE rejetée à la
            # place, via l'aide PARTAGÉE (oto#25 lot b2, `connectors/health.py`) — même
            # mécanisme et même garde de portée que la sonde `verify` d'un connecteur
            # keyé, motif fournisseur BRUT en valeur de champ (pas la seule catégorie
            # opaque `credential_rejected`). La ligne — et le refresh token mort qu'elle
            # porte encore — reste en place ; elle se lève en la reposant (reconnexion :
            # `persist_token` écrase `meta`, cf. plus haut).
            connector_health.mark_rejected(
                credentials_store.USER, sub, _CONNECTOR, "", str(e) or None)
            return None
        access_token = resp["access_token"]
        new_refresh = resp.get("refresh_token", cred["secret"])  # rotation possible
        credentials_store.set_credential(
            "user", sub, _CONNECTOR, secret=new_refresh, set_by=sub,
            meta={"access_token": access_token,
                  "expires_at": oauth2_pkce.expires_at(resp.get("expires_in"))},
        )
    return access_token


def status_for(sub: str) -> dict:
    cred = credentials_store.get_credential_with_meta("user", sub, _CONNECTOR)
    return {"connected": bool(cred and cred.get("secret")),
            "set_at": cred.get("set_at") if cred else None}


def _link_state(sub: str) -> connector_link.LinkState:
    """État de lien pour `/api/me` — le credential vit au scope LEGACY ("user", sub),
    pas au scope MEMBRE des connecteurs keyés. C'est précisément pour ça que la
    lecture est déclarée ici et pas devinée par une boucle générique.

    La santé (oto#25 lot a) suit la même règle : `access.status_for` ne batch-lit
    `health_ko`/`health_reason` que sur le palier MEMBRE, donc jamais sur ce scope —
    on la lit nous-mêmes sur la ligne qu'on sait être la bonne."""
    st = status_for(sub)
    reason = credentials_store.credential_health("user", sub, _CONNECTOR)
    return connector_link.LinkState(linked=bool(st.get("connected")),
                                    set_at=st.get("set_at"),
                                    accounts=1 if st.get("connected") else 0,
                                    health_ko=True if reason is not None else None,
                                    health_reason=reason)


connector_link.register(_CONNECTOR, _link_state)


def _start_flow(ctx, values: dict) -> "connector_flow.FlowStart":
    """Le geste « connecter », déclaré comme celui de tout autre connecteur (#300).

    Il existait — mais **hors du point de passage** : une route REST écrite à la main
    rendait `{auth_url}` par coïncidence, sans que rien ne l'y oblige, et le garde-fou
    qui impose la forme commune ne voit que les capacités. Le front devait donc garder
    une fonction par connecteur là où le seam existe précisément pour qu'il n'ait pas à
    savoir lequel il branche.
    """
    return connector_flow.FlowStart(auth_url=build_auth_url(ctx.sub))


connector_flow.declare(
    _CONNECTOR,
    start=_start_flow,
    label="Autoriser oto chez Atlassian",
    callback_path="/api/atlassian/oauth/callback",
)


def disconnect(sub: str) -> bool:
    return credentials_store.clear_credential("user", sub, _CONNECTOR)
