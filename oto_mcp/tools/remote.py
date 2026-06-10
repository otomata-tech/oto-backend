"""Connecteur remote — middleware générique vers les bridges (ADR 0003).

Pour chaque connecteur `kind="remote"` du registre, enregistre 2 tools :
`<ns>_describe` (surface du bridge + doc) et `<ns>_call` (forward lecture
seule). AUCUN code ni credential client ici : le bridge (service HTTP distant,
ex. movinmotion-backoffice-bridge) détient le credential du système client ;
le coffre plateforme ne tient que le moyen de l'appeler (token M2M +
`meta.base_url`, credential de l'org active — cf. `access.resolve_remote_credential`).

Gating identique aux connecteurs in-process : namespace grant-only
(deny-by-default via la visibilité) + backstop `require_namespace` au
call-time. L'identité de l'appelant est forwardée (`X-Oto-Sub`) pour que
l'audit trail vive du côté du bridge.

Contexte stdio local (sub=None) : pas de coffre → raise actionnable vers la
CLI locale (le package bridge expose `oto <ns>` par entry-point, secrets locaux).
"""
from __future__ import annotations

import asyncio

import requests
from fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, connectors
from ..auth_hooks import current_user_sub_from_token

TIMEOUT = 45  # le bridge a lui-même un timeout amont de 30s


def register(mcp: FastMCP) -> None:
    for c in connectors.REMOTE_CONNECTORS:
        _register_one(mcp, c)


def _bridge_get(connector: connectors.Connector, route: str, params: dict | None = None) -> dict:
    ns = connector.namespaces[0]
    access.require_namespace(ns)
    sub = current_user_sub_from_token()
    if sub is None:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(
                f"Connecteur remote `{connector.name}` indisponible en stdio local "
                f"(credential d'org serveur requis) — utiliser la CLI locale `oto {ns}`."
            ),
        ))
    base_url, token = access.resolve_remote_credential(connector.name)
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
                f"Bridge `{connector.name}` : token M2M refusé (rotaté ?). Un admin "
                f"doit re-poser le credential d'org (`oto_admin_set_org_secret`)."
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
            message=f"Bridge `{connector.name}` : HTTP {r.status_code}{' — ' + detail if detail else ''}",
        ))
    return r.json()


def _register_one(mcp: FastMCP, c: connectors.Connector) -> None:
    ns = c.namespaces[0]

    @mcp.tool(
        name=f"{ns}_describe",
        description=(
            f"{c.label} — surface disponible du connecteur (routes forwardables + doc). "
            f"À appeler d'abord pour découvrir les paths utilisables par {ns}_call. "
            f"{c.help}".strip()
        ),
    )
    async def describe() -> dict:
        return await asyncio.to_thread(_bridge_get, c, "/describe")

    @mcp.tool(
        name=f"{ns}_call",
        description=(
            f"{c.label} — appel LECTURE SEULE au connecteur ({c.help}). "
            f"`path` = route du système distant (voir {ns}_describe pour la surface). "
            f"Le service distant borne les paths autorisés."
        ),
    )
    async def call(path: str) -> dict:
        return await asyncio.to_thread(_bridge_get, c, "/call", {"path": path})
