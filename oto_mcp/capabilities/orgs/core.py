"""Capacités du domaine orgs (ADR 0009). Barreau 1 : `org.use_org`.

`oto_use_org` (MCP) et `PUT /api/me/active-org` (REST) étaient câblés séparément
(drift de surface). Une seule `Capability` les co-déclare ; les deux adaptateurs
en dérivent.
"""
from __future__ import annotations

import os
from typing import Optional

from pydantic import BaseModel, Field

from ... import access, config, db, org_store, session_org, tenancy
from .._authz import SUB_ONLY
from .._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from ..registry import CAPABILITIES

_MAX_ORGS_PER_USER = int(os.environ.get("OTO_MCP_MAX_ORGS_PER_USER", "10"))
#: Plafond d'un compte HORS plafond (`_uncapped`) : une borne entière explicite plutôt
#: que `null`, parce que `quota.cap`/`quota.remaining` sont des ENTIERS REQUIS du
#: contrat de `GET /api/me/orgs` que des fronts épinglent (décision du 25/09/2026).
#: Assez haute pour ne jamais mordre ; le refus ne teste que `remaining == 0`.
PLAFOND_LEVE = 1_000_000


class NoInput(BaseModel):
    pass


class CreateOrgInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class OrgCreated(BaseModel):
    """Espace créé. ⚠️ **Créer bascule** : l'org neuve devient ton org MAISON dans la
    foulée (`active_org` == `org_id`), donc le défaut de tous tes appels sans jeton
    `_org=`, y compris depuis d'autres conversations déjà ouvertes. Un client qui
    enchaîne des créations change de contexte à chaque fois sans l'avoir demandé."""
    org_id: int
    # Le nom APRÈS strip — peut différer de l'entrée (espaces de bord retirés).
    name: str
    # Écho de l'org maison désormais posée. Même valeur que `org_id` : redondance de
    # compat front, pas un second concept.
    active_org: int
    # Toujours "org_admin" : le créateur est admin de son espace, par construction.
    org_role: str


class HomeOrgSet(BaseModel):
    """Org MAISON posée (`PUT /api/me/active-org`, action « définir par défaut » du
    dashboard). `home_org` et `active_org` portent **la même valeur** — le second est
    un écho de compat pour le front, hérité de l'ex-face REST d'`use_org` ; ne pas y
    lire deux pointeurs distincts."""
    home_org: int
    active_org: int
    # None si l'org a disparu entre la résolution et la relecture (course) — jamais
    # un signe que l'écriture a échoué : `home_org` fait foi.
    name: Optional[str] = None


class ClearOrgResult(BaseModel):
    """⚠️ **Deux réponses distinctes selon la SURFACE, pas selon un paramètre.**

    - Face MCP (`oto_clear_org`) : **pur no-op**. Depuis ADR 0038 il n'y a plus d'état
      de session à effacer ; on renvoie `{session_state: null, how_to}` et **rien n'est
      muté**. `session_state: null` ne veut donc pas dire « effacé », mais « ce concept
      n'existe plus ».
    - Face REST (`DELETE /api/me/active-org`) : **écriture**. Le verbe DELETE ne remet
      pas l'org maison à « aucune » — il la bascule sur l'**espace personnel** de
      l'utilisateur (créé au besoin), dont l'id revient dans `active_org`. On n'est
      jamais org-less.

    Les deux clés sont donc mutuellement exclusives : `active_org` en REST,
    `session_state`+`how_to` en MCP."""
    active_org: Optional[int] = None
    session_state: Optional[str] = None
    how_to: Optional[str] = None


def _uncapped(sub: str) -> bool:
    """Hors plafond : le super_admin, et l'admin de SON tenant (même lecture que
    `_authz.TENANT_ADMIN_OF` — slug dérivé du préfixe du sub, jamais déclaré). Ce sont
    les comptes qui montent des espaces POUR d'autres (onboarding client, démos) : un
    plafond pensé contre l'emballement d'un self-serve les bloquait au 11ᵉ client, et
    l'archivage — le remède annoncé — n'a pas de sens pour un espace client vivant.
    Le super_admin d'abord : c'est le cas courant, et il épargne la lecture tenant."""
    if access.is_super_admin(sub):
        return True
    return db.is_tenant_admin(tenancy.current().tenant_of(sub), sub)


def org_quota(sub: str) -> dict:
    """Où en est ce compte face au plafond de création — **source unique**, lue par le
    refus d'`org.create` ET par la liste `org.list` (`oto_list_orgs`).

    Le premier signal (#464, 15/08) portait deux demandes ; ec976b0 n'avait traité que
    la première (que le refus dise le compte et le remède). La seconde était : rendre le
    compte lisible **avant** le mur, pour qu'une procédure prévienne à 9 sur 10 au lieu
    d'échouer au 11ᵉ. Un plafond qui ne s'apprend qu'en s'y cognant coûte un tour à
    chaque procédure qui crée des espaces — et celle qui l'a signalé en crée un par run.

    Le rendre en UN endroit est le point : deux comptes qui divergeraient (une liste qui
    annonce une place restante là où la création refuse) seraient pires que le mur muet
    d'origine — on aurait remplacé un silence par un mensonge. `remaining` est borné à 0
    plutôt que de partir négatif : un compte peut dépasser le plafond par un chemin qui
    ne passe pas par ici (création admin, baisse de `OTO_MCP_MAX_ORGS_PER_USER`), et
    « -3 places restantes » ne veut rien dire pour l'appelant.

    `created` compte ce qui OCCUPE une place — ni les archivées, ni l'espace personnel
    (cf. `org_store.count_orgs_created_by`, qui porte la règle et son pourquoi).

    Compte hors plafond (`_uncapped`) : `cap` vaut `PLAFOND_LEVE`, une borne entière
    nommée, et `remaining` ce qu'il en reste — jamais `null` : ces deux champs sont des
    entiers requis du contrat que des fronts épinglent. Le refus ne teste que `== 0` et
    ne mord donc pas ; `created` reste rendu, il est toujours vrai."""
    created = org_store.count_orgs_created_by(sub)
    cap = PLAFOND_LEVE if _uncapped(sub) else _MAX_ORGS_PER_USER
    return {"created": created, "cap": cap, "remaining": max(0, cap - created)}


def _create_org(ctx: ResolvedCtx, inp: CreateOrgInput) -> dict:
    """Self-serve : crée un espace, en fait l'admin, le bascule actif."""
    # Le compte est relu pour le DIRE : un refus qui n'annonce que son plafond laisse
    # l'appelant sans moyen de savoir ce qui l'occupe ni comment redescendre — et
    # l'archivage, seul geste qui libère une place, n'est deviné par personne.
    quota = org_quota(ctx.sub)
    if quota["remaining"] == 0:
        raise AuthzDenied(
            429, "org_quota",
            f"Limite d'espaces créés atteinte : {quota['created']}/{quota['cap']}. "
            "Archive un espace que tu n'utilises plus pour libérer une place — "
            "l'archivage est réversible et ton espace personnel n'est pas compté. "
            "Ce compte se lit sans se cogner au mur : `oto_list_orgs` le rend "
            "(bloc `quota`).")
    name = inp.name.strip()
    if not name:
        raise AuthzDenied(400, "invalid_name", "Nom d'espace requis.")
    # Le front qui héberge l'org est DÉRIVÉ du tenant de son créateur (registre
    # d'émetteurs), jamais déclaré. Sans ça une org créée depuis un front tiers
    # repart à NULL, donc ses invitations pointent `oto.cx` ET s'augmentent d'un
    # magic-link minté sur NOTRE Logto — inerte contre l'émetteur du tenant. L'invité
    # se crée alors un compte CHEZ NOUS, accepte avec celui-là, et son compte du
    # tenant n'est membre de rien : deux identités pour une personne, et l'org
    # inatteignable depuis son propre front (vécu le 15/08 sur l'org d'un tenant tiers).
    # b6e1d27 a donné aux invitations la marque de l'org ; il manquait qui la pose.
    front_base_url, front_brand = config.front_for(ctx.sub)
    org_id = org_store.create_org(name, created_by=ctx.sub,
                                  front_base_url=front_base_url,
                                  front_brand=front_brand)
    org_store.add_org_member(org_id, ctx.sub, "org_admin", actor=ctx.sub)
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
    l'appel (`_org=`/`_project=`/`_group=`) ou retombe sur l'org maison — jamais par
    un état serveur. `org` = id/nom."""
    try:
        org_id = org_store.resolve_org_for_user(ctx.sub, inp.org)  # garantit l'appartenance
    except ValueError as e:
        raise AuthzDenied(404, "unknown_org", str(e))
    o = org_store.get_org(org_id)
    return {
        "org": org_id, "name": o["name"] if o else None, "session_state": None,
        "how_to": (f"Aucun état de session (ADR 0038) : passe `_org={org_id}` sur chaque "
                   "appel scopé org (connecteurs, data_*, capacités l'acceptent). "
                   f"Puis recharge tes instructions contextuelles de cette org (readme "
                   f"d'org+équipe, guides, procédures — FIGÉES à la connexion, elles ne "
                   f"suivent PAS seules) via `oto_context(_org={org_id})`. "
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
                "how_to": ("Aucun état de session à effacer (ADR 0038) : sans `_org=`, "
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
        authz=SUB_ONLY, Output=OrgCreated,
        description=(
            "Create your own organization (workspace). You become its org_admin "
            "and it becomes your active org. Self-serve — any authenticated user."
        ),
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
            "(ADR 0038): to act under another org, pass `_org=<id>` directly on "
            "each org-scoped call (connectors, data_*, capabilities all accept "
            "it — the underscore prefix avoids clashing with a tool's own `org` "
            "argument, e.g. this one's target). Without a token, every call "
            "resolves your home org — which is changed in the DASHBOARD only, "
            "never by the agent."
        ),
        mcp="oto_use_org",
    ),
    Capability(
        key="org.set_home",
        handler=_set_home_org,
        Input=UseOrgInput,
        authz=SUB_ONLY, Output=HomeOrgSet,
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
        authz=SUB_ONLY, Output=ClearOrgResult,
        description=(
            "No-op hint (ADR 0038: no session state — without an `_org=` token "
            "every call already resolves your home org, which is changed in the "
            "dashboard only)."
        ),
        mcp="oto_clear_org",
        rest=RestBinding("DELETE", "/api/me/active-org"),  # REST : maison = org perso
    ),
]
