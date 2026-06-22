"""Org de session — override éphémère per-conversation (ADR 0023, barreau R1).

`oto_use_org` (MCP) ne mute plus l'« org maison » persistante (colonne
`users.active_org`) : il pose un **override de session**, vécu le temps d'une
conversation claude.ai. Une nouvelle conversation repart sur la maison.

Stockage : un dict en mémoire **keyé par `session_id`** (sync — `ctx.session_id`
est une propriété sync du contexte fastmcp), PAS l'état de session async
(`ctx.get_state`/`set_state` ne sont pas lisibles depuis le code sync chaud comme
`resolve_api_key`). Repose sur l'**isolation des sessions MCP par conversation**
(claude.ai : vérifié). État éphémère assumé : perdu au restart → tout retombe sur
la maison (direction sûre). Borné (`_CAP`) pour ne pas fuir.

Sentinelle : `0` = profil **perso/global** (cohérent ADR 0015 `org_id=0`), pour
distinguer « override = perso » (posé par `oto_clear_org`) de « pas d'override »
(→ repli maison).
"""
from __future__ import annotations

from typing import Optional

# session_id -> org_id (0 = perso/global). Insertion-ordered → éviction du plus ancien.
_OVERRIDES: dict[str, int] = {}
_CAP = 100_000


def current_session_id() -> Optional[str]:
    """`session_id` de la session MCP courante, ou None hors contexte MCP (REST,
    code hors requête). Sert de clé de l'override ET de discriminant MCP/REST."""
    try:
        from fastmcp.server.dependencies import get_context

        sid = getattr(get_context(), "session_id", None)
        return sid or None
    except Exception:
        return None


def set_override(session_id: str, org_id: Optional[int]) -> None:
    """Pose l'override de session (`org_id=None` → perso, stocké 0)."""
    _OVERRIDES.pop(session_id, None)  # ré-insère en queue (récence)
    _OVERRIDES[session_id] = org_id or 0
    while len(_OVERRIDES) > _CAP:
        del _OVERRIDES[next(iter(_OVERRIDES))]  # évince le plus ancien


def get_override(session_id: Optional[str]) -> tuple[bool, Optional[int]]:
    """`(present, org_id)`. `present=False` → pas d'override (repli maison).
    `org_id=None` quand l'override est « perso »."""
    if session_id is None or session_id not in _OVERRIDES:
        return (False, None)
    v = _OVERRIDES[session_id]
    return (True, None if v == 0 else v)


def current_override() -> tuple[bool, Optional[int]]:
    """Override de la session courante (convenience MCP)."""
    return get_override(current_session_id())
