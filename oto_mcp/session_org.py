"""Contextes d'exécution — jetons d'appel (ADR 0038) + consultation REST (ADR 0023).

⚠️ **Les BRACELETS de session org/groupe sont INERTES depuis ADR 0038 B3** : plus
écrits par `oto_use_org`/`oto_use_group` (devenus hints sans état), plus lus par
`access.current_org`/`current_group`. Raisons : claude.ai renouvelle le
`Mcp-Session-Id` à CHAQUE appel (bracelet jamais relu, #72) et un session_id
recyclé cross-compte faisait hériter le scope (#108). Le scope est porté par
l'appel (`org=`/`project=`/`group=`, contextvars `_CALL_*` ci-dessous) ou retombe
sur la maison. Les stores `_OVERRIDES`/`_GROUP_OVERRIDES` et leurs fonctions sont
**conservés transitoirement** (WIP en vol les importe) — à supprimer au prochain
nettoyage. Le bracelet PROJET (`_PROJECT_OVERRIDES`) reste actif (B3b à venir).

Sentinelle : `0` = profil **perso/global** (cohérent ADR 0015 `org_id=0`).
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


# ── Org de l'APPEL (jeton explicite per-requête — modèle sans état de session) ─
# Override porté par un PARAMÈTRE d'appel (`org=`), PAS par un état de session :
# celui-ci ne survit pas chez claude.ai (un Mcp-Session-Id neuf est frappé à CHAQUE
# tool call → un override keyé session_id n'est jamais relu par l'appel suivant).
# Contextvar PER-REQUÊTE (isolé par tâche Starlette) posé par l'adaptateur capacité
# APRÈS validation d'appartenance (is_org_member), lu EN PRIORITÉ par
# `access.current_org`. Robuste au stateless : vit le temps de l'appel, rien à
# persister ni à évincer. C'est la première pierre du modèle « contexte par
# identifiants d'appel » (remplace à terme l'override de session ci-dessous).
_CALL_ORG: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(
    "oto_call_org", default=None)


def set_call_org(org_id: int) -> contextvars.Token:
    """Épingle l'org de l'appel courant (renvoie le token à reset en fin d'appel)."""
    return _CALL_ORG.set(org_id)


def reset_call_org(token: contextvars.Token) -> None:
    _CALL_ORG.reset(token)


def current_call_org() -> Optional[int]:
    """Org épinglée par le paramètre `org=` de l'appel courant, ou None. Déjà gardée
    (is_org_member) à la pose par l'adaptateur → `current_org` la rend telle quelle."""
    return _CALL_ORG.get()


# ── Autres axes-contexte d'appel (même modèle que _CALL_ORG, posés par le même
# middleware) — project/group/run_id/account. Généralisation du contexte par
# identifiants d'appel (remplace à terme les overrides de session par axe). Chacun
# est gardé/dérivé à la pose par le middleware ; None = axe non fourni pour l'appel.
_CALL_PROJECT: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(
    "oto_call_project", default=None)
_CALL_GROUP: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(
    "oto_call_group", default=None)
_CALL_RUN: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "oto_call_run", default=None)
_CALL_ACCOUNT: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "oto_call_account", default=None)


def set_call_project(project_id: int) -> contextvars.Token:
    return _CALL_PROJECT.set(project_id)


def reset_call_project(token: contextvars.Token) -> None:
    _CALL_PROJECT.reset(token)


def current_call_project() -> Optional[int]:
    """Projet épinglé par `project=` de l'appel courant (déjà gardé can_access), ou None."""
    return _CALL_PROJECT.get()


def set_call_group(group_id: int) -> contextvars.Token:
    return _CALL_GROUP.set(group_id)


def reset_call_group(token: contextvars.Token) -> None:
    _CALL_GROUP.reset(token)


def current_call_group() -> Optional[int]:
    """Groupe épinglé par `group=` de l'appel courant (déjà gardé can_read_group), ou None."""
    return _CALL_GROUP.get()


def set_call_run(run_id: str) -> contextvars.Token:
    return _CALL_RUN.set(run_id)


def reset_call_run(token: contextvars.Token) -> None:
    _CALL_RUN.reset(token)


def current_call_run() -> Optional[str]:
    """run_id explicite de l'appel courant (corrélation calllog), ou None."""
    return _CALL_RUN.get()


def set_call_account(account: str) -> contextvars.Token:
    return _CALL_ACCOUNT.set(account)


def reset_call_account(token: contextvars.Token) -> None:
    _CALL_ACCOUNT.reset(token)


def current_call_account() -> Optional[str]:
    """Compte (identité connecteur) épinglé par `account=` de l'appel courant, ou None."""
    return _CALL_ACCOUNT.get()


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


# ── Projet de session (bracelet éphémère, ADR 0032 §4 B2.2) ──────────────────
# Mirroir de l'override d'org : un projet « actif » DANS la conversation, posé par
# `oto_use_project`, lu par la résolution d'identité pour appliquer la surcharge
# connecteur PRÉFAITE du projet (jamais déclarée à la volée). Pas de « projet maison »
# persistant (un projet est toujours explicite) ⇒ pas d'override = None. Keyé par
# session_id (sync), MCP-only ; meurt avec la conversation. Borné (`_CAP`).
_PROJECT_OVERRIDES: dict[str, int] = {}


def set_project_override(session_id: str, project_id: int) -> None:
    """Pose le projet actif de la session (le bracelet sélectionne, ne déclare rien)."""
    _PROJECT_OVERRIDES.pop(session_id, None)  # ré-insère en queue (récence)
    _PROJECT_OVERRIDES[session_id] = project_id
    while len(_PROJECT_OVERRIDES) > _CAP:
        del _PROJECT_OVERRIDES[next(iter(_PROJECT_OVERRIDES))]


def clear_project_override(session_id: str) -> None:
    """Retire le projet actif (retour « hors projet » pour la session)."""
    _PROJECT_OVERRIDES.pop(session_id, None)


def get_project_override(session_id: Optional[str]) -> Optional[int]:
    """`project_id` actif de la session, ou None (pas de projet épinglé)."""
    if session_id is None:
        return None
    return _PROJECT_OVERRIDES.get(session_id)


def current_project_override() -> Optional[int]:
    """Projet actif de la session courante (convenience MCP)."""
    return get_project_override(current_session_id())


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
