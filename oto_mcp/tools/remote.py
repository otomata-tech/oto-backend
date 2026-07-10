"""Connecteur `bridge` universel — middleware générique vers UN pont distant (ADR 0034).

UN connecteur du catalogue (`providers.py`, kind="remote") pour tout pont vers un
service distant qui détient le credential métier (contrat bridge ADR 0003 §4,
inchangé : `GET /describe` + `GET /call`, bearer M2M, lecture seule bornée côté
bridge). L'identité du service ponté vit dans la CONFIG d'org (base_url + label),
jamais dans le namespace → montrable au catalogue sans nom client.

AUCUN code ni credential client ici : le coffre plateforme ne tient que le moyen
d'appeler le pont — champs standard `base_url`/`token`/`label` du connecteur
`bridge` (cascade membre > groupe > org via `resolve_credential_fields`). La
visibilité suit le régime commun (activation × sélection 0019/0050 — hors socle,
installable depuis la library) ; l'exécution lève proprement sans credential. L'identité de
l'appelant est forwardée (`X-Oto-Sub`) pour que l'audit trail vive côté bridge.

(Le legacy per-namespace data-driven — découverte `meta.base_url`, tools
`<ns>_describe`/`<ns>_call`, règle de visibilité dédiée — a été retiré en B4 ;
le pilote mm/Movinmotion a migré sur ce connecteur.)
"""
from __future__ import annotations

import logging

import requests
from fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access
from ..auth_hooks import current_user_sub_from_token

TIMEOUT = 45  # le bridge a lui-même un timeout amont de 30s
log = logging.getLogger("oto_mcp.tools.remote")


def register(mcp: FastMCP) -> None:
    _register_bridge(mcp)


def _bridge_credential() -> tuple[str, str]:
    """Résout `(base_url, token)` du connecteur `bridge` — champs standard du
    coffre (cascade membre > groupe > org). Lève une McpError actionnable si
    l'org n'a pas configuré son pont."""
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
    """Forward vers le pont de l'org (bearer M2M + X-Oto-Sub d'audit)."""
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
