"""Rédaction des champs sensibles du RÉSULTAT d'un tool — logique PARTAGÉE.

Extraite de `middleware.FieldRedactionMiddleware` (chemin protocole `tools/call`)
pour être réutilisée par `oto_call` (dispatch universel, ADR 0036) : le dispatch
exécute la cible via `Tool.run` **hors chaîne de middleware**, donc il doit
ré-appliquer la rédaction lui-même — sinon un connecteur à PII
(folk/pennylane/unipile) fuiterait par ce canal. « Derive don't duplicate ».

Politique (ADR 0009/0015) : la policy de l'org active gouverne l'exposition.
**Fail-closed** : une policy qui EXISTE mais échoue RETIENT la sortie (lève
`RedactionWithheld`) plutôt que de laisser fuiter le brut. Absence de policy
(`is_empty`), échec de résolution sur un service sans défaut serveur, ou payload
non-structuré = passe-through (sentinelle `PASSTHROUGH`).
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

# Sentinelle « rien à rédiger » — distincte de None (un payload peut légitimement
# valoir None). L'appelant renvoie alors le résultat d'ORIGINE inchangé (pas de
# re-sérialisation sur le chemin chaud du cas commun « aucune policy »).
PASSTHROUGH = object()


class RedactionWithheld(Exception):
    """La rédaction d'une policy EXISTANTE a échoué → sortie retenue (fail-closed)."""


def _resolve_field_filter(service: str):
    # Import tardif : `access` importe des stores → éviter un cycle au chargement.
    from . import access
    return access.resolve_field_filter(service)


def _service_has_server_default(service: str) -> bool:
    from . import field_filter_defaults
    return service in field_filter_defaults.SERVER_DEFAULTS


def extract_payload(result) -> dict | list | None:
    """Forme brute renvoyée par un tool à partir de son `ToolResult` :
    `structured_content` si dict, sinon le JSON du 1er bloc `content`. None si
    rien de structuré (texte libre / binaire)."""
    sc = getattr(result, "structured_content", None)
    if isinstance(sc, dict):
        return sc
    content = getattr(result, "content", None) or []
    block = content[0] if content else None
    text = getattr(block, "text", None)
    if isinstance(text, str):
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            return None
        if isinstance(data, (dict, list)):
            return data
    return None


def redact_payload(service: str, payload):
    """Applique la policy de rédaction de l'org active pour `service` (namespace)
    au `payload` brut (dict | list). Retourne le payload rédacté, ou `PASSTHROUGH`
    si rien ne s'applique. Lève `RedactionWithheld` si une policy existe mais lève."""
    if not isinstance(payload, (dict, list)):
        return PASSTHROUGH
    try:
        ff = _resolve_field_filter(service)
    except Exception:
        # Résolution de policy en échec (ex. DB) : policy inconnue. Service à PII
        # connu (défaut serveur déclaré) → fail-closed ; sinon passe-through pour
        # ne pas casser tous les tools sur un aléa DB.
        logger.exception("resolve_field_filter a échoué pour %s", service)
        if _service_has_server_default(service):
            raise RedactionWithheld(service)
        return PASSTHROUGH
    if ff.is_empty:
        return PASSTHROUGH
    # Une policy EXISTE pour ce service → fail-closed à partir d'ici.
    try:
        return ff.apply(payload)
    except Exception:
        logger.exception("rédaction de %s en échec — sortie retenue", service)
        raise RedactionWithheld(service)


def rebuild_result(result, redacted):
    """Réémet un `ToolResult` avec `redacted` sur les DEUX canaux : texte JSON +
    `structured_content` (seulement si l'original en portait un dict — sinon la
    donnée vivait dans le canal texte, le structuré reste vide)."""
    from fastmcp.tools.tool import ToolResult
    from mcp.types import TextContent
    sc = getattr(result, "structured_content", None)
    return ToolResult(
        content=[TextContent(type="text", text=json.dumps(redacted, default=str))],
        structured_content=redacted if isinstance(sc, dict) else None,
        meta=getattr(result, "meta", None),
        is_error=False,
    )


def withheld_result(name: str):
    """`ToolResult` d'erreur « sortie retenue » (fail-closed) pour l'outil `name`."""
    from fastmcp.tools.tool import ToolResult
    from mcp.types import TextContent
    return ToolResult(
        content=[TextContent(
            type="text",
            text=f"[oto] rédaction de « {name} » impossible — sortie retenue par sécurité.")],
        is_error=True,
    )
