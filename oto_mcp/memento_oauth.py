"""Memento OAuth — flow web per-user (otomata#16, B2).

Memento est un MCP autonome (mcp.mento.cc/mcp) authentifié OAuth 2.1 + DCR via
Supabase. Pour le **fédérer** dans oto (connecteur `kind="mount"`, cf.
tools/mount.py), chaque user oto connecte SON compte memento : oto agit en client
OAuth confidentiel (enregistré par DCR, creds en SOPS `MEMENTO_OAUTH_CLIENT_*`),
stocke le token per-user dans le coffre, et le proxy l'injecte par requête.

Flow (calqué sur google_oauth.py, + PKCE requis par Supabase) :
1. `/api/memento/oauth/start` (auth Logto) → URL authorize Supabase avec un
   `state` HMAC-signé portant {sub, code_verifier} + le code_challenge S256.
2. L'user consent sur memento → Supabase redirige vers
   `/api/memento/oauth/callback?code=…&state=…`.
3. On vérifie le state, échange le code (client_secret_basic + code_verifier)
   contre {access_token, refresh_token, expires_in}, persiste dans le coffre
   (`connector_credentials`, connector='memento', entity=user).
4. `access_token_for(sub)` : lit le coffre, refresh transparent si expiré.

Le refresh_token (sensible, long-lived) est le `secret` chiffré du coffre ;
l'access_token (bearer ~1h, dérivé) vit dans `meta` — même modèle que Google.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import datetime, timezone
from typing import Optional

from . import connectors, credentials_store

_AS = "https://doebdriroupduqpggcsj.supabase.co/auth/v1"
_AUTH_URL = f"{_AS}/oauth/authorize"
_TOKEN_URL = f"{_AS}/oauth/token"
_MCP_RESOURCE = "https://mcp.mento.cc/mcp"
_CONNECTOR = "memento"
_STATE_TTL = 600  # 10 min


def _client_id() -> str:
    v = os.environ.get("MEMENTO_OAUTH_CLIENT_ID")
    if not v:
        raise RuntimeError("MEMENTO_OAUTH_CLIENT_ID env var manquante")
    return v


def _client_secret() -> str:
    v = os.environ.get("MEMENTO_OAUTH_CLIENT_SECRET")
    if not v:
        raise RuntimeError("MEMENTO_OAUTH_CLIENT_SECRET env var manquante")
    return v


def _state_secret() -> bytes:
    v = os.environ.get("OTO_MCP_OAUTH_STATE_SECRET")
    if not v:
        raise RuntimeError("OTO_MCP_OAUTH_STATE_SECRET env var manquante")
    return v.encode()


def _redirect_uri() -> str:
    base = os.environ.get("OTO_MCP_PUBLIC_URL", "https://mcp.oto.ninja").rstrip("/")
    return f"{base}/api/memento/oauth/callback"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# --- PKCE + state (verifier embarqué dans le state HMAC-signé) --------------
#
# Note sécu (audit 2026-06-13, accepté). Le code_verifier PKCE est porté DANS le
# state (b64url lisible, HMAC pour l'intégrité seulement, pas le secret) plutôt
# que stocké côté serveur. C'est volontairement acceptable ici : oto est un
# client OAuth *confidentiel* (échange en client_secret_basic, cf. _basic_auth),
# donc PKCE n'est qu'une défense en profondeur — exposer le verifier n'ouvre
# aucune attaque tant que le client_secret (jamais hors serveur) est requis pour
# échanger le code. Pas de store de state serveur à introduire pour ce flow.

def _pkce_pair() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(48))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def make_state(sub: str, verifier: str) -> str:
    payload = json.dumps({"sub": sub, "v": verifier, "ts": int(time.time())},
                         separators=(",", ":")).encode()
    sig = hmac.new(_state_secret(), payload, hashlib.sha256).digest()
    return f"{_b64url(payload)}.{_b64url(sig)}"


def verify_state(state: str) -> Optional[tuple[str, str]]:
    """Renvoie (sub, code_verifier) si valide+non expiré, sinon None."""
    if not state or "." not in state:
        return None
    p_b64, sig_b64 = state.split(".", 1)
    try:
        payload, sig = _b64url_decode(p_b64), _b64url_decode(sig_b64)
    except Exception:
        return None
    if not hmac.compare_digest(sig, hmac.new(_state_secret(), payload, hashlib.sha256).digest()):
        return None
    try:
        data = json.loads(payload)
    except Exception:
        return None
    if int(time.time()) - int(data.get("ts", 0)) > _STATE_TTL:
        return None
    sub, v = data.get("sub"), data.get("v")
    return (sub, v) if isinstance(sub, str) and isinstance(v, str) else None


def build_auth_url(sub: str) -> str:
    from urllib.parse import urlencode
    verifier, challenge = _pkce_pair()
    params = {
        "client_id": _client_id(),
        "redirect_uri": _redirect_uri(),
        "response_type": "code",
        "scope": "openid",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": make_state(sub, verifier),
        "resource": _MCP_RESOURCE,
    }
    return f"{_AUTH_URL}?{urlencode(params)}"


# --- échange + persistance --------------------------------------------------

def _basic_auth() -> str:
    return base64.b64encode(f"{_client_id()}:{_client_secret()}".encode()).decode()


def exchange_code(code: str, verifier: str) -> dict:
    import requests
    r = requests.post(
        _TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _redirect_uri(),
            "code_verifier": verifier,
            "resource": _MCP_RESOURCE,
        },
        headers={"Authorization": f"Basic {_basic_auth()}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def _expires_at(expires_in) -> Optional[str]:
    n = int(expires_in or 0)
    if not n:
        return None
    return datetime.fromtimestamp(time.time() + n, tz=timezone.utc).isoformat()


def persist_token(sub: str, token_response: dict) -> None:
    refresh_token = token_response.get("refresh_token")
    if not refresh_token:
        raise RuntimeError("Memento n'a pas émis de refresh_token (vérifie scope/offline).")
    credentials_store.set_credential(
        "user", sub, _CONNECTOR, secret=refresh_token, set_by=sub,
        meta={
            "access_token": token_response.get("access_token"),
            "expires_at": _expires_at(token_response.get("expires_in")),
        },
    )


class MementoReauthRequired(Exception):
    """Le refresh token memento est mort (invalid_grant) → l'user doit reconnecter."""


def _refresh(refresh_token: str) -> dict:
    import requests
    r = requests.post(
        _TOKEN_URL,
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        headers={"Authorization": f"Basic {_basic_auth()}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    # Refresh token révoqué/expiré → 400 invalid_grant : pas une erreur serveur,
    # c'est une réauth à faire. On le distingue d'un vrai incident (5xx, réseau).
    if r.status_code in (400, 401):
        body = (r.text or "")[:300]
        raise MementoReauthRequired(body)
    r.raise_for_status()
    return r.json()


def access_token_for(sub: str) -> Optional[str]:
    """Access token memento valide pour ce sub (refresh transparent si expiré),
    ou None si le user n'a pas connecté memento."""
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
        except Exception:
            needs_refresh = True
    if needs_refresh:
        try:
            resp = _refresh(cred["secret"])
        except MementoReauthRequired:
            # Grant mort : on purge le credential pour que status_for/list_workspaces
            # repassent en « non connecté » et que l'UI propose de reconnecter.
            credentials_store.clear_credential("user", sub, _CONNECTOR)
            return None
        access_token = resp["access_token"]
        new_refresh = resp.get("refresh_token", cred["secret"])  # Supabase rotate parfois
        credentials_store.set_credential(
            "user", sub, _CONNECTOR, secret=new_refresh, set_by=sub,
            meta={"access_token": access_token, "expires_at": _expires_at(resp.get("expires_in"))},
        )
    return access_token


async def _call_memento_tool(sub: str, tool: str, args: dict) -> Optional[dict]:
    """Appelle un verbe du MCP memento v3 (`load`/`list`/`get`/`admin`…, sans préfixe
    `mem_` depuis le cutover v3) avec le token per-user (même endpoint que la
    fédération `tools/mount.py`). Renvoie le payload brut, ou None si le user n'a pas
    connecté memento. La curation reste sur me.mento.cc — on ne fait que lire/relayer
    pour le browse dashboard."""
    token = access_token_for(sub)
    if not token:
        return None
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    client = Client(StreamableHttpTransport(
        _MCP_RESOURCE, headers={"Authorization": f"Bearer {token}"}))
    async with client:
        result = await client.call_tool(tool, args)
    data = getattr(result, "data", None)
    if not isinstance(data, dict):
        data = json.loads(result.content[0].text)
    return data


_VIEWER_BASE = "https://me.mento.cc"


async def list_workspaces(sub: str) -> Optional[dict]:
    """Topologie read-only des KB memento du user (orientation dashboard), traduite
    du modèle v3 (`admin(action=orgs)`, 1 org = 1 base) vers la carte que le
    dashboard consomme : {default, orgs[{org, name, myRole, personal, workspaces[]}],
    shared[], pinned[]} — le `slug` d'un workspace = l'UUID de base v3, repassé tel
    quel à list_pages. None si non connecté."""
    data = await _call_memento_tool(sub, "admin", {"action": "orgs"})
    if data is None:
        return None
    default: Optional[str] = None
    orgs = []
    for o in data.get("orgs", []):
        base = o.get("base")
        workspaces = []
        if base:
            workspaces.append({
                "slug": base["id"], "name": base["name"], "summary": "",
                "visibility": "org", "myRole": o.get("myRole"),
            })
            if o.get("personal"):
                default = base["id"]
        orgs.append({"org": o.get("slug"), "name": o.get("name"),
                     "myRole": o.get("myRole"), "personal": bool(o.get("personal")),
                     "workspaces": workspaces})
    return {"default": default, "orgs": orgs, "shared": [], "pinned": []}


async def list_pages(sub: str, workspace: Optional[str] = None,
                     cursor: Optional[str] = None, limit: int = 100) -> Optional[dict]:
    """Énumère les pages d'une base v3 (`list(kind=pages)`, curseur opaque à repasser
    tel quel). `workspace` = UUID de base (omis = la seule base accessible — memento
    lève s'il y en a plusieurs). Renvoie {workspace, items[], totalCount, hasMore,
    cursor}. None si non connecté."""
    args: dict = {"kind": "pages", "limit": limit}
    if workspace:
        args["base"] = workspace
    if cursor:
        args["cursor"] = cursor
    data = await _call_memento_tool(sub, "list", args)
    if data is None:
        return None
    items = [{
        "id": it.get("id"),
        "title": it.get("title") or "(sans titre)",
        "docPath": it.get("description") or "",
        "status": "ACTIVE",  # v3 ne liste que les pages actives
        "updatedAt": it.get("updated_at"),
    } for it in data.get("items", [])]
    return {"workspace": workspace, "items": items,
            "totalCount": data.get("totalCount"),
            "hasMore": data.get("cursor") is not None,
            "cursor": data.get("cursor")}


async def get_document(sub: str, *, doc_id: str) -> Optional[dict]:
    """Rend une page v3 entière (`get(id, kind=page)`) sous la forme document que le
    dashboard consomme : {document: {id, title, url, blocks[]}} — v3 porte un body
    markdown unique (plus de blocs), rendu comme un bloc unique. Lookup par id
    seulement (le `path` v2 n'existe plus). None si non connecté."""
    page = await _call_memento_tool(sub, "get", {"id": doc_id, "kind": "page"})
    if page is None:
        return None
    body = page.get("body") or page.get("description") or ""
    return {"document": {
        "id": page.get("id"),
        "title": page.get("title"),
        "url": f"{_VIEWER_BASE}/v3/page/{page.get('id')}",
        "blocks": ([{"id": f"{page.get('id')}:body", "type": "markdown",
                     "content": body}] if body else []),
    }}


def status_for(sub: str) -> dict:
    # Présence seule (jamais le secret) : un statut de connexion n'a pas à
    # déchiffrer le refresh_token. credential_status lit `set_at` sans toucher
    # `secret_enc` → /api/me ne 500 plus sur une enveloppe illisible, et la
    # surface d'attaque est réduite (cf. credentials_store.credential_status).
    st = credentials_store.credential_status("user", sub, _CONNECTOR)
    return {"connected": st is not None,
            "set_at": st["set_at"] if st else None}


def disconnect(sub: str) -> bool:
    return credentials_store.clear_credential("user", sub, _CONNECTOR)
