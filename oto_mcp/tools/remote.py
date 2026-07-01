"""Connecteur remote — middleware générique vers les bridges (ADR 0003).

Un connecteur remote est découvert de la **DONNÉE** (ADR 0003/0011) : il existe ssi
une org a posé un credential avec `meta.base_url` (l'endpoint du bridge) — zéro nom
client au registre. Pour chaque namespace découvert, enregistre 2 tools :
`<ns>_describe` (surface du bridge + doc) et `<ns>_call` (forward lecture seule).
AUCUN code ni credential client ici : le bridge (service HTTP distant) détient le
credential du système client ; le coffre plateforme ne tient que le moyen de
l'appeler (token M2M + `meta.base_url`, credential de l'org active — cf.
`access.resolve_remote_credential`). Le credential d'org EST le grant : la
**visibilité** masque un outil remote pour toute org qui ne détient pas son
credential (règle dédiée dans `session_visibility`, ADR 0031), et l'**exécution**
est gardée par `resolve_remote_credential` (lève sans credential d'org).
L'identité de l'appelant est forwardée (`X-Oto-Sub`) pour que l'audit trail vive
du côté du bridge.

Contexte stdio local (sub=None) : pas de coffre → raise actionnable vers la
CLI locale (le package bridge expose `oto <ns>` par entry-point, secrets locaux).
"""
from __future__ import annotations

import asyncio
import logging

import requests
from fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, connectors, credentials_store
from ..auth_hooks import current_user_sub_from_token

TIMEOUT = 45  # le bridge a lui-même un timeout amont de 30s
log = logging.getLogger("oto_mcp.tools.remote")


def register(mcp: FastMCP) -> None:
    # Bridge UNIVERSEL (ADR 0034) : namespace fixe `bridge`, credential = champs
    # standard du connecteur `bridge` du registre (base_url/token/label, cascade
    # d'org via resolve_credential_fields). Enregistré inconditionnellement — la
    # visibilité suit le régime commun (activation × masque user), l'exécution
    # lève proprement sans credential.
    _register_bridge(mcp)

    # LEGACY (ADR 0003/0011, retrait prévu ADR 0034 B4) — découverte data-driven :
    # un remote existe ssi une org a posé un credential avec `meta.base_url`. Figé
    # au boot (comme les mounts). Dégradation propre si le coffre/DB est indispo
    # au boot (pas de crash du serveur — register_all n'est pas wrappé ici).
    try:
        namespaces = credentials_store.list_remote_namespaces()
    except Exception as e:
        log.warning("remote: discovery échouée (%s) → 0 connecteur remote", e)
        return
    # jamais de collision avec le namespace fixe du bridge universel
    namespaces -= {"bridge"}
    if not namespaces:
        return
    for ns in sorted(namespaces):
        _register_one(mcp, ns)


def _bridge_credential() -> tuple[str, str]:
    """Résout `(base_url, token)` du connecteur `bridge` UNIVERSEL (ADR 0034) —
    champs standard du coffre (cascade membre > groupe > org), plus de `meta`.
    Lève une McpError actionnable si l'org n'a pas configuré son pont."""
    sub = current_user_sub_from_token()
    if sub is None:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message="Connecteur bridge indisponible en stdio local (credential d'org requis).",
        ))
    try:
        f = access.resolve_credential_fields("bridge")
    except Exception:
        f = {}
    base_url = (f.get("base_url") or "").strip().rstrip("/")
    token = (f.get("token") or "").strip()
    if not base_url or not token:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(
                "Connecteur bridge non configuré pour ton org : pose `base_url` "
                "(l'endpoint de ton service) + `token` (M2M) sur la carte Bridge du dashboard."
            ),
        ))
    return base_url, token


def _universal_bridge_get(route: str, params: dict | None = None) -> dict:
    """Forward vers le pont de l'org (bridge universel). Même contrat HTTP que le
    legacy `_bridge_get` (bearer M2M + X-Oto-Sub d'audit, erreurs actionnables)."""
    sub = current_user_sub_from_token()
    base_url, token = _bridge_credential()
    r = requests.get(
        f"{base_url}{route}",
        params=params,
        headers={"Authorization": f"Bearer {token}", "X-Oto-Sub": sub or ""},
        timeout=TIMEOUT,
    )
    if r.status_code == 401:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=("Bridge : token M2M refusé (rotaté ?). Re-pose le credential "
                     "sur la carte Bridge du dashboard de ton org."),
        ))
    if not r.ok:
        detail = ""
        try:
            detail = r.json().get("detail", "")
        except Exception:
            pass
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=f"Bridge : HTTP {r.status_code}{' — ' + detail if detail else ''}",
        ))
    return r.json()


def _register_bridge(mcp: FastMCP) -> None:
    @mcp.tool(
        name="bridge_describe",
        description=(
            "Ton bridge (service distant de ton org, connecteur `bridge`) — surface "
            "disponible : routes forwardables + doc. À appeler d'abord pour découvrir "
            "les paths utilisables par bridge_call."
        ),
    )
    def bridge_describe() -> dict:
        return _universal_bridge_get("/describe")

    @mcp.tool(
        name="bridge_call",
        description=(
            "Ton bridge (service distant de ton org) — appel LECTURE SEULE forwardé. "
            "`path` = route du système distant (voir bridge_describe). Le service "
            "distant borne les paths autorisés."
        ),
    )
    def bridge_call(path: str) -> dict:
        return _universal_bridge_get("/call", {"path": path})


def _bridge_get(ns: str, route: str, params: dict | None = None) -> dict:
    # Garde d'accès = la résolution du credential : `resolve_remote_credential(ns)`
    # lève si l'org active ne possède pas le credential remote (le credential d'org
    # EST le grant, ADR 0003/0031). Plus de `require_namespace` (relicat grant-only).
    sub = current_user_sub_from_token()
    if sub is None:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(
                f"Connecteur remote `{ns}` indisponible en stdio local "
                f"(credential d'org serveur requis) — utiliser la CLI locale `oto {ns}`."
            ),
        ))
    base_url, token = access.resolve_remote_credential(ns)
    r = requests.get(
        f"{base_url}{route}",
        params=params,
        headers={"Authorization": f"Bearer {token}", "X-Oto-Sub": sub},
        timeout=TIMEOUT,
    )
    if r.status_code == 401:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(
                f"Bridge `{ns}` : token M2M refusé (rotaté ?). Un admin doit "
                f"re-poser le credential d'org (`oto_admin_set_org_secret`)."
            ),
        ))
    if not r.ok:
        detail = ""
        try:
            detail = r.json().get("detail", "")
        except Exception:
            pass
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=f"Bridge `{ns}` : HTTP {r.status_code}{' — ' + detail if detail else ''}",
        ))
    return r.json()


def _register_one(mcp: FastMCP, ns: str) -> None:
    @mcp.tool(
        name=f"{ns}_describe",
        description=(
            f"Connecteur remote `{ns}` — surface disponible (routes forwardables + "
            f"doc). À appeler d'abord pour découvrir les paths utilisables par {ns}_call."
        ),
    )
    async def describe() -> dict:
        return await asyncio.to_thread(_bridge_get, ns, "/describe")

    @mcp.tool(
        name=f"{ns}_call",
        description=(
            f"Connecteur remote `{ns}` — appel LECTURE SEULE forwardé au bridge. "
            f"`path` = route du système distant (voir {ns}_describe). Le service "
            f"distant borne les paths autorisés."
        ),
    )
    async def call(path: str) -> dict:
        return await asyncio.to_thread(_bridge_get, ns, "/call", {"path": path})
