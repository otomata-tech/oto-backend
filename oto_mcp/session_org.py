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

import contextvars
from typing import Optional

# session_id -> org_id (0 = perso/global). Insertion-ordered → éviction du plus ancien.
_OVERRIDES: dict[str, int] = {}
_CAP = 100_000

# ── Org de consultation (view-as, face REST) ────────────────────────────────
# Notion DISTINCTE de l'override de session (MCP) : sur le dashboard on consulte
# une org sans rien persister ni muter l'identité d'action (ADR 0023). Porté par
# un contextvar PER-REQUÊTE (isolé par tâche Starlette), posé par l'adaptateur
# REST APRÈS validation d'appartenance, lu par le seam `access.current_org`.
# 0 = consulter le profil perso ; None = pas de consultation (→ repli maison).
_VIEW_ORG: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(
    "oto_view_org", default=None)


def set_view_org(org_id: Optional[int]) -> contextvars.Token:
    """Pose l'org de consultation pour la requête courante (renvoie le token à reset)."""
    return _VIEW_ORG.set(org_id)


def reset_view_org(token: contextvars.Token) -> None:
    _VIEW_ORG.reset(token)


def current_view_org() -> Optional[int]:
    """Org de consultation de la requête courante (None = aucune)."""
    return _VIEW_ORG.get()


# ── View-as USER (« voir en tant que », face REST, LECTURE SEULE) ────────────
# Extension de la consultation à l'axe USER : un opérateur plateforme « voit en
# tant que » un autre user dans le dashboard. Contextvar per-requête posé par
# ViewAsMiddleware APRÈS validation (opérateur + cible existe + méthode GET), lu
# par `_authenticate` qui renvoie alors le **sub cible** → tout `/api/me/*` rend
# la vue de la cible. REST-ONLY et lecture seule : le MCP ne lit JAMAIS ce
# contextvar (zéro impersonation dans Claude), les mutations sont rejetées en amont.
_VIEW_USER: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "oto_view_user", default=None)


def set_view_user(sub: Optional[str]) -> contextvars.Token:
    """Pose le user de consultation pour la requête courante (renvoie le token à reset)."""
    return _VIEW_USER.set(sub)


def reset_view_user(token: contextvars.Token) -> None:
    _VIEW_USER.reset(token)


def current_view_user() -> Optional[str]:
    """Sub consulté pour la requête courante (None = aucun → sub réel)."""
    return _VIEW_USER.get()


# ── Axe ÉQUIPE (groupe) — même mécanique que l'org (ADR 0023 étendu) ─────────
# Le store ne garde QUE des group_id réels ; « pas de groupe » (niveau org) se
# DÉRIVE = override d'org présent SANS override de groupe ⇒ niveau org. Ça tient
# l'invariant « groupe actif ⊂ org active » sans jamais faire fuiter le home_group.
_GROUP_OVERRIDES: dict[str, int] = {}
_VIEW_GROUP: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(
    "oto_view_group", default=None)


def set_group_override(session_id: str, group_id: int) -> None:
    """Pose l'override d'équipe de session (un group_id réel)."""
    _GROUP_OVERRIDES.pop(session_id, None)
    _GROUP_OVERRIDES[session_id] = group_id
    while len(_GROUP_OVERRIDES) > _CAP:
        del _GROUP_OVERRIDES[next(iter(_GROUP_OVERRIDES))]


def clear_group_override(session_id: str) -> None:
    """Retire l'override d'équipe (retour niveau org pour la session)."""
    _GROUP_OVERRIDES.pop(session_id, None)


def get_group_override(session_id: Optional[str]) -> tuple[bool, Optional[int]]:
    if session_id is None or session_id not in _GROUP_OVERRIDES:
        return (False, None)
    return (True, _GROUP_OVERRIDES[session_id])


def current_group_override() -> tuple[bool, Optional[int]]:
    return get_group_override(current_session_id())


def set_view_group(group_id: Optional[int]) -> contextvars.Token:
    return _VIEW_GROUP.set(group_id)


def reset_view_group(token: contextvars.Token) -> None:
    _VIEW_GROUP.reset(token)


def current_view_group() -> Optional[int]:
    return _VIEW_GROUP.get()


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


# ── Org épinglée par sous-domaine (« 1 oto par org », endpoint scopé) ─────────
# Un endpoint `<slug>--mcp.oto.ninja` épingle l'org POUR LA CONNEXION. Le Host est
# sur CHAQUE requête HTTP → on l'enregistre per-requête sur deux supports : un
# contextvar (couvre les handlers de la MÊME requête, ex. l'initialize qui calcule
# la visibilité) ET un dict keyé par session_id (couvre les lectures hors-contextvar
# des appels d'outils, comme l'override de session). La GARDE d'appartenance vit
# dans `access.current_org` (sub connu) : un non-membre est ignoré → repli maison,
# zéro fuite. Hard-lock : `current_org` priorise ce candidat ⇒ `oto_use_org` no-op.
_SUBDOMAIN_ORG: dict[str, int] = {}
_SUBDOMAIN_CV: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(
    "oto_subdomain_org", default=None)


def set_subdomain_cv(org_id: int) -> contextvars.Token:
    return _SUBDOMAIN_CV.set(org_id)


def reset_subdomain_cv(token: contextvars.Token) -> None:
    _SUBDOMAIN_CV.reset(token)


def store_subdomain_org(session_id: str, org_id: int) -> None:
    """Mémorise l'org du sous-domaine pour la session MCP (lecture hors-contextvar)."""
    _SUBDOMAIN_ORG.pop(session_id, None)
    _SUBDOMAIN_ORG[session_id] = org_id
    while len(_SUBDOMAIN_ORG) > _CAP:
        del _SUBDOMAIN_ORG[next(iter(_SUBDOMAIN_ORG))]


def current_subdomain_candidate() -> Optional[int]:
    """Org candidate épinglée par le sous-domaine de la connexion courante, ou None.
    Candidate = AVANT garde d'appartenance (appliquée par `access.current_org`)."""
    v = _SUBDOMAIN_CV.get()
    if v is not None:
        return v
    sid = current_session_id()
    return _SUBDOMAIN_ORG.get(sid) if sid else None
