"""Connecteur remote — middleware générique vers les bridges (ADR 0003).

Un connecteur remote est découvert de la **DONNÉE** (ADR 0003/0011) : il existe ssi
une org a posé un credential avec `meta.base_url` (l'endpoint du bridge) — zéro nom
client au registre. Pour chaque namespace découvert, enregistre 2 tools :
`<ns>_describe` (surface du bridge + doc) et `<ns>_call` (forward lecture seule).
AUCUN code ni credential client ici : le bridge (service HTTP distant) détient le
credential du système client ; le coffre plateforme ne tient que le moyen de
l'appeler (token M2M + `meta.base_url`, credential de l'org active — cf.
`access.resolve_remote_credential`). Le credential d'org EST le grant (visibilité
deny-by-default via un set grant-only runtime rempli au boot).

Gating identique aux connecteurs in-process : namespace grant-only
(deny-by-default via la visibilité) + backstop `require_namespace` au
call-time. L'identité de l'appelant est forwardée (`X-Oto-Sub`) pour que
l'audit trail vive du côté du bridge.

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

from .. import access, connectors, credentials_store, tool_visibility
from ..auth_hooks import current_user_sub_from_token

TIMEOUT = 45  # le bridge a lui-même un timeout amont de 30s
log = logging.getLogger("oto_mcp.tools.remote")


def register(mcp: FastMCP) -> None:
    # Découverte data-driven : un remote existe ssi une org a posé un credential
    # avec `meta.base_url`. Figé au boot (comme les mounts) — un nouveau remote
    # apparaît au prochain restart. Dégradation propre si le coffre/DB est indispo
    # au boot (pas de crash du serveur — register_all n'est pas wrappé ici).
    try:
        namespaces = credentials_store.list_remote_namespaces()
    except Exception as e:
        log.warning("remote: discovery échouée (%s) → 0 connecteur remote", e)
        return
    if not namespaces:
        return
    # Deny-by-default sans entrée registre : grant-only runtime (le credential
    # d'org EST le grant, cf. access.granted_namespaces_for).
    tool_visibility.register_runtime_grant_only(namespaces)
    for ns in sorted(namespaces):
        _register_one(mcp, ns)


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
