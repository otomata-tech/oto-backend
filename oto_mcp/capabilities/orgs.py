"""Capacités du domaine orgs (ADR 0009). Barreau 1 : `org.use_org`.

`oto_use_org` (MCP) et `PUT /api/me/active-org` (REST) étaient câblés séparément
(drift de surface). Une seule `Capability` les co-déclare ; les deux adaptateurs
en dérivent.
"""
from __future__ import annotations

import os

from pydantic import BaseModel, Field

from .. import org_store, session_org
from ._authz import SUB_ONLY
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

_MAX_ORGS_PER_USER = int(os.environ.get("OTO_MCP_MAX_ORGS_PER_USER", "10"))


class NoInput(BaseModel):
    pass


class CreateOrgInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)


def _create_org(ctx: ResolvedCtx, inp: CreateOrgInput) -> dict:
    """Self-serve : crée un espace, en fait l'admin, le bascule actif."""
    if org_store.count_orgs_created_by(ctx.sub) >= _MAX_ORGS_PER_USER:
        raise AuthzDenied(429, "org_quota",
                          f"Limite de {_MAX_ORGS_PER_USER} espaces créés atteinte.")
    name = inp.name.strip()
    if not name:
        raise AuthzDenied(400, "invalid_name", "Nom d'espace requis.")
    org_id = org_store.create_org(name, created_by=ctx.sub)
    org_store.add_org_member(org_id, ctx.sub, "org_admin")
    # Nouvelle org = ton org maison (défaut) — effective immédiatement, y compris dans
    # cette conversation (le seam `current_org` retombe sur la maison sans jeton ; plus
    # de bracelet de session, ADR 0038 B3).
    org_store.set_active_org(ctx.sub, org_id)
    return {"org_id": org_id, "name": name, "active_org": org_id, "org_role": "org_admin"}


class UseOrgInput(BaseModel):
    org: str  # id (ex "3") ou nom exact — contrat unifié MCP + REST


def _use_org(ctx: ResolvedCtx, inp: UseOrgInput) -> dict:
    """Hint SANS ÉTAT (ADR 0038 B3 — le bracelet de session est retiré) : valide
    l'appartenance et renvoie le geste fiable. Le scope d'un appel est porté par
    l'appel (`org=`/`project=`/`group=`) ou retombe sur l'org maison — jamais par
    un état serveur. `org` = id/nom."""
    try:
        org_id = org_store.resolve_org_for_user(ctx.sub, inp.org)  # garantit l'appartenance
    except ValueError as e:
        raise AuthzDenied(404, "unknown_org", str(e))
    o = org_store.get_org(org_id)
    return {
        "org": org_id, "name": o["name"] if o else None, "session_state": None,
        "how_to": (f"Aucun état de session (ADR 0038) : passe `org={org_id}` sur chaque "
                   "appel scopé org (connecteurs, data_*, capacités l'acceptent). "
                   "L'org par défaut (maison) ne se change que dans le dashboard — "
                   "jamais depuis l'agent."),
    }


def _set_home_org(ctx: ResolvedCtx, inp: UseOrgInput) -> dict:
    """Pose l'**org maison** persistante — le défaut de TOUT appel sans jeton.
    **UI-ONLY (décision 2026-07-06)** : muter le défaut depuis l'agent polluait
    toutes les autres conversations (vécu : « workaround fiable » spontané des
    agents après le retrait du bracelet) → le binding MCP est retiré, seule
    l'action « définir par défaut » du dashboard y accède (`PUT /api/me/active-org`)."""
    try:
        org_id = org_store.resolve_org_for_user(ctx.sub, inp.org)
    except ValueError as e:
        raise AuthzDenied(404, "unknown_org", str(e))
    org_store.set_active_org(ctx.sub, org_id)  # colonne = org maison
    o = org_store.get_org(org_id)
    # `active_org` en écho pour compat front (l'ex-face REST d'use_org rendait ça).
    return {"home_org": org_id, "active_org": org_id, "name": o["name"] if o else None}


def _clear_org(ctx: ResolvedCtx, inp: NoInput) -> dict:
    """Retour à l'espace par défaut. MCP = hint sans état (plus de bracelet à
    retirer, ADR 0038 B3 : sans jeton, chaque appel résout déjà la maison) ; REST
    = bascule la maison sur l'**org perso** de l'user (jamais org-less)."""
    sid = session_org.current_session_id()
    if sid is not None:
        return {"session_state": None,
                "how_to": ("Aucun état de session à effacer (ADR 0038) : sans `org=`, "
                           "chaque appel résout ton org maison (elle ne se change que "
                           "dans le dashboard).")}
    pid = org_store.ensure_personal_org(ctx.sub)     # REST : maison = org perso
    org_store.set_active_org(ctx.sub, pid)
    return {"active_org": pid}


CAPABILITIES += [
    Capability(
        key="org.create",
        handler=_create_org,
        Input=CreateOrgInput,
        authz=SUB_ONLY,
        description=(
            "Create your own organization (workspace). You become its org_admin "
            "and it becomes your active org. Self-serve — any authenticated user."
        ),
        mcp="oto_create_org",
        rest=RestBinding("POST", "/api/me/orgs"),
        refresh_visibility=True,  # bascule l'org active → toolbox de la nouvelle org
    ),
    Capability(
        key="org.use_org",
        handler=_use_org,
        Input=UseOrgInput,
        authz=SUB_ONLY,
        description=(
            "Resolve an organization you belong to (by id or name) and get the "
            "RELIABLE way to act under it. This tool holds NO session state "
            "(ADR 0038): to act under another org, pass `org=<id>` directly on "
            "each org-scoped call (connectors, data_*, capabilities all accept "
            "it). Without a token, every call resolves your home org — which is "
            "changed in the DASHBOARD only, never by the agent."
        ),
        mcp="oto_use_org",
    ),
    Capability(
        key="org.set_home",
        handler=_set_home_org,
        Input=UseOrgInput,
        authz=SUB_ONLY,
        description=(
            "Set the HOME organization — the persistent default of every call "
            "without an org token. UI-ONLY (dashboard « définir par défaut ») : "
            "no MCP binding, the agent must not mutate the default (ADR 0038)."
        ),
        rest=RestBinding("PUT", "/api/me/active-org"),  # « définir par défaut » dashboard
        refresh_visibility=True,  # l'org effective (maison) change → recompute la toolbox
    ),
    Capability(
        key="org.clear",
        handler=_clear_org,
        Input=NoInput,
        authz=SUB_ONLY,
        description=(
            "No-op hint (ADR 0038: no session state — without an `org=` token "
            "every call already resolves your home org, which is changed in the "
            "dashboard only)."
        ),
        mcp="oto_clear_org",
        rest=RestBinding("DELETE", "/api/me/active-org"),  # REST : maison = org perso
    ),
]
