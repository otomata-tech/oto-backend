"""Connecteur `http` — client HTTP générique multi-auth (secret DANS le coffre oto).

À distinguer du bridge (`tools/remote.py`, ADR 0034) : le bridge forwarde vers un
service distant qui DÉTIENT le credential (custody hors plateforme, token M2M) ;
ici oto détient le secret de l'API cible (coffre AES chiffré, byo_org) et tape
l'API **directement**. L'org configure sur la carte HTTP : `base_url`, `auth_mode`
(bearer/header/query/basic/oauth2/none) + le(s) secret(s) du mode.

Adaptateur mince (ADR 0037) : le moteur (auth + forward) vit dans oto-core
(`oto.tools.http`) ; ici on résout le credential d'org et on traduit les erreurs
en McpError. Lecture seule (GET). C'est un « nœud HTTP » (webhook sortant) : la
protection SSRF est un contrôle d'egress réseau au niveau plateforme, pas du code
par-connecteur (comme Zapier/Make/n8n). Étant un tool MCP ordinaire, le résultat
repasse par la rédaction de champs (FieldRedactionMiddleware) comme tout connecteur.
"""
from __future__ import annotations

import logging

import requests
from fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS
from oto.tools.http import HttpConnectorClient

from .. import access
from ..auth_hooks import current_user_sub_from_token

log = logging.getLogger("oto_mcp.tools.http")
TIMEOUT = 45


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name="http_get",
        description=(
            "Appel HTTP GET lecture seule vers l'API configurée pour ton org "
            "(connecteur `http`). `path` = chemin relatif à la base_url (commence "
            "par /). `params` = query params optionnels. L'auth configurée (bearer, "
            "clé API, basic, oauth2) est injectée automatiquement."
        ),
    )
    def http_get(path: str, params: dict | None = None) -> dict:
        if not isinstance(path, str) or not path.startswith("/"):
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message="`path` doit commencer par / (chemin relatif à base_url).",
            ))
        client = _client()
        try:
            return client.get(path, params)
        except ValueError as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else 502
            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"API cible : HTTP {status}"))


def _client() -> HttpConnectorClient:
    """Résout le credential `http` de l'org et instancie le client oto-core.

    Lève une McpError actionnable si l'org n'a pas configuré son connecteur ou si
    la config est invalide (hôte non public anti-SSRF, mode/champ manquant)."""
    sub = current_user_sub_from_token()
    if sub is None:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message="Connecteur http indisponible en stdio local (credential d'org requis).",
        ))
    try:
        f = access.resolve_credential_fields("http")
    except Exception:
        f = {}
    base_url = (f.get("base_url") or "").strip()
    mode = (f.get("auth_mode") or "").strip()
    if not base_url or not mode:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(
                "Connecteur http non configuré pour ton org : pose `base_url` + "
                "`auth_mode` (+ le secret du mode) sur la carte HTTP du dashboard."
            ),
        ))
    try:
        return HttpConnectorClient(base_url, mode, f, timeout=TIMEOUT)
    except ValueError as e:
        raise McpError(ErrorData(code=INVALID_PARAMS, message=f"Connecteur http : {e}"))
