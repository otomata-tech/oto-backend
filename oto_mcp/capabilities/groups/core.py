"""Capacités du sous-palier GROUPE (départements / équipes, ADR 0012).

CRUD groupe + groupe actif. Co-déclarées comme les capacités org (ADR 0009) :
un handler core + Input pydantic + règle d'autz (combinateurs `roles`-aware) +
bindings MCP/REST. L'autz d'écriture passe par `GROUP_ADMIN_OF` (chef d'équipe,
org_admin parent ou platform_admin par escalade) ; la création par
`ORG_ADMIN_OF` (créer un groupe = acte d'org_admin) ; les lectures par
`ORG_MEMBER_OF`/`GROUP_MEMBER_OF` ; le switch self-serve par `SUB_ONLY`.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from ... import access, group_store, org_store, session_org
from .._authz import GROUP_ADMIN_OF, GROUP_MEMBER_OF, ORG_ADMIN_OF, ORG_MEMBER_OF, SUB_ONLY
from .._types import AuthzDenied, Capability, DeclaredError, ResolvedCtx, RestBinding
from ..registry import CAPABILITIES

# Le domaine FERMÉ d'un rôle d'équipe (validé à l'écriture par `GROUP_ROLES`) (#437).
GroupRole = Literal[group_store.GROUP_ROLES]

_GID = {"id": "group_id"}
_OID = {"id": "org_id"}


class CreateGroupInput(BaseModel):
    org_id: int
    name: str = Field(min_length=1, max_length=80)
    description: str = ""


class OrgIdInput(BaseModel):
    org_id: int


class GroupIdInput(BaseModel):
    group_id: int


class UseGroupInput(BaseModel):
    group_id: int


class UpdateGroupInput(BaseModel):
    group_id: int
    name: Optional[str] = None
    description: Optional[str] = None


# --- Sorties ----------------------------------------------------------------
#
# ⚠️ Lire d'abord l'invariant du palier, et surtout SES EXCEPTIONS. La règle qu'on
# retient d'ADR 0012 est « une équipe RÉTRÉCIT ce que l'org expose, jamais l'inverse »
# — vraie pour l'appartenance (`group.member.add` refuse un non-membre de l'org) et
# pour la visibilité d'outils (denylist additif). **Elle est FAUSSE pour deux des
# capacités décrites ici**, et un intégrateur qui l'applique en bloc se trompera :
#   · `group.secret.set` — le secret d'équipe passe AVANT celui de l'org dans la
#     cascade : l'équipe REDIRIGE ce que l'org avait posé, elle ne le restreint pas.
#   · `group.invite.create` — un chef d'équipe invite dans SON équipe, et l'invité
#     rejoint l'ORG au passage : l'équipe fait grossir la population de l'org.

class GroupCreated(BaseModel):
    """Équipe créée. ⚠️ **Créer une équipe ne bascule rien** — contrairement à
    `org.create`, qui fait de l'org neuve ton org maison. L'équipe fraîche n'est ni
    ton équipe active ni ta maison ; il faut `PUT /api/me/home-group` pour l'habiter.

    ⚠️ **« Tu deviens chef d'équipe » n'est pas garanti** : le créateur n'est fait
    `group_admin` que s'il est RÉELLEMENT membre de l'org parente. Un platform_admin
    (ou un opérateur qui passe l'autz par escalade sans appartenir à l'org) crée donc
    une équipe **sans aucun chef** — et rien dans cette réponse ne le dit. L'équipe
    n'est alors administrable que par un org_admin, jamais depuis son propre palier.

    ⚠️ `description` n'est **pas** rendue : impossible de confirmer ce qui a été
    enregistré sans relire `GET /api/groups/{id}`.

    Un nom déjà pris dans l'org (comparaison INSENSIBLE à la casse) est refusé en 409
    `group_exists` — et le renommage (`GroupUpdated`) fait le MÊME contrôle (#281)."""
    # `id` et `group_id` portent LA MÊME valeur (doublon de compat front, hérité des
    # deux conventions de nommage du dashboard) — pas deux identifiants distincts.
    id: int
    group_id: int
    org_id: int
    # Le nom APRÈS strip : peut différer de l'entrée.
    name: str


class GroupBrief(BaseModel):
    """Fiche courte d'une équipe.

    ⚠️ **`my_role: null` ne veut PAS dire « tu n'as aucun droit sur cette équipe »** :
    c'est le rôle EXPLICITE (une ligne d'appartenance), sans l'escalade de `roles.py`.
    Un org_admin gouverne toutes les équipes de son org sans être membre d'aucune —
    il lit donc `my_role: null` sur des équipes qu'il peut renommer, supprimer, dont
    il peut poser les secrets. Un front qui conditionne ses boutons de gestion à
    `my_role == "group_admin"` cache l'administration à ceux qui l'ont. La source du
    droit, ce sont les drapeaux de `GET /api/groups/{id}/instructions`, jamais ce
    champ : `can_edit` pour l'administration de l'équipe, et — depuis #695 —
    `can_write_instructions` / `can_delete_instructions` pour les procédures, qui ne
    se déduisent NI de `my_role` ni de `can_edit` (un simple membre écrit une
    procédure sans rien administrer).

    ⚠️ `member_count` compte les mêmes lignes explicites : une équipe « à 0 membre »
    peut être pleinement gouvernée (et son secret partagé pleinement actif)."""
    id: int
    group_id: int
    org_id: int
    name: str
    # Toujours présente (chaîne vide si jamais renseignée) — pas de null à gérer.
    description: str
    member_count: int
    my_role: Optional[GroupRole] = None


class GroupList(BaseModel):
    """Équipes d'une org. ⚠️ **Ce n'est pas « mes équipes »** : l'autz est
    `ORG_MEMBER_OF`, donc un simple membre voit TOUTES les équipes de l'org, y compris
    celles où il n'entre pas (`my_role: null`). Pour la vue « les miennes + laquelle
    est active », c'est la console `oto_group`, pas cette route.

    Une liste vide veut bien dire « aucune équipe dans cette org » — c'est le seul
    endroit de ce module où le vide n'est pas ambigu."""
    org_id: int
    groups: list[GroupBrief]


class UseGroupResult(BaseModel):
    """⚠️ **Deux réponses distinctes selon la SURFACE, pas selon un paramètre** (même
    dissymétrie que `org.use_org`) :

    - Face MCP (`oto_use_group`) : **pur hint, rien n'est muté** (ADR 0038). On valide
      l'appartenance et on renvoie `{group, name, org, session_state: null, how_to}`.
      `session_state: null` ne dit pas « effacé » mais « ce concept n'existe plus » :
      le scope se porte par appel (`_group=`).
    - Face REST (`PUT /api/me/active-group`) : **écriture, et DOUBLE**. Elle pose
      l'équipe maison ET bascule l'org maison sur l'org parente (invariant équipe ⊂
      org, atomique). `active_org` n'est donc pas un écho informatif : c'est la trace
      d'une seconde mutation que l'appelant n'a pas demandée. Un dashboard qui ne
      rafraîchit que l'indicateur d'équipe affiche une org périmée.

    Les deux jeux de clés sont mutuellement exclusifs.

    ⚠️ Le 403 `not_a_member` recouvre **deux** causes côté REST : tu n'es pas membre
    de l'équipe, **ou** tu l'es sans être membre de l'org parente (incohérence de
    données). Le message est le même dans les deux cas."""
    # Face MCP
    group: Optional[int] = None
    org: Optional[int] = None
    session_state: Optional[str] = None
    how_to: Optional[str] = None
    # Face REST
    active_group: Optional[int] = None
    active_org: Optional[int] = None
    # Commun aux deux faces.
    name: Optional[str] = None


class ClearGroupResult(BaseModel):
    """⚠️ Deux réponses selon la surface, comme `UseGroupResult` :

    - Face MCP (`oto_clear_group`) : **no-op** — `{session_state: null, how_to}`, rien
      n'est écrit.
    - Face REST (`DELETE /api/me/active-group`) : efface l'équipe maison → `active_group:
      null`.

    ⚠️ **Asymétrie avec `org.clear`** : on peut parfaitement rester SANS équipe (c'est
    le niveau org, le régime nominal), là où on n'est jamais sans org (le DELETE d'org
    bascule sur l'espace personnel). Ne pas transposer le raisonnement d'un palier à
    l'autre : ici `null` est un état de repos valide, pas une anomalie à corriger.

    L'opération est idempotente et ne rend jamais 404 : effacer quand rien n'est actif
    répond 200."""
    active_group: Optional[int] = None
    session_state: Optional[str] = None
    how_to: Optional[str] = None


class HomeGroupSet(BaseModel):
    """Équipe MAISON posée (`PUT /api/me/home-group`, dashboard — pas de binding MCP :
    l'agent ne mute pas les défauts).

    ⚠️ **C'est une double écriture** : poser l'équipe maison pose AUSSI l'org parente
    en org maison (invariant équipe ⊂ org). `home_org` n'est donc pas la relecture
    d'une valeur inchangée — c'est le second champ que l'appel vient de modifier,
    potentiellement pour toutes les conversations déjà ouvertes de l'utilisateur.

    ⚠️ 403 `not_a_member` couvre là aussi deux causes (pas membre de l'équipe / pas
    membre de l'org parente)."""
    home_group: int
    home_org: int
    name: Optional[str] = None


class GroupMemberEntry(BaseModel):
    """Un membre d'équipe.

    ⚠️ **`active` ne dit rien de l'état du compte** : ce n'est ni « actif » au sens
    activé, ni « a utilisé oto récemment ». C'est le pointeur d'équipe COURANTE de
    CETTE personne — vrai si c'est l'équipe qu'elle a choisie pour travailler, faux
    sinon. Un membre parfaitement légitime lit `active: false` dès qu'il travaille
    sous une autre équipe. Le rendre en « compte actif / inactif » est un contresens,
    et il a une conséquence concrète : **le secret partagé de l'équipe ne sert QUE les
    membres dont `active` est vrai ici** (la cascade lit l'équipe active).

    ⚠️ `email`/`name` valent `null` quand le sub n'a pas de ligne `users` (invité
    rattaché sans première connexion, compte machine) — jamais une erreur, et jamais
    une raison de masquer la ligne : `sub` reste l'identifiant qui fait foi.

    `role` est le rôle EXPLICITE (`group_admin`|`group_member`) — mêmes réserves que
    `GroupBrief.my_role` sur l'escalade."""
    sub: str
    email: Optional[str] = None
    name: Optional[str] = None
    role: GroupRole
    active: bool


class GroupSecretEntry(BaseModel):
    """Un credential partagé de l'équipe — **métadonnées seules** : le coffre ne
    restitue aucun secret, à personne.

    ⚠️ `base_url` est une clé **ABSENTE**, pas `null`, quand le connecteur n'en porte
    pas : elle n'est ajoutée que si elle existe. Un client typé strictement doit
    tolérer son absence plutôt que tester `!== null`.

    ⚠️ Sa présence ici ne dit pas qu'un membre l'utilise : la cascade est
    `clé membre > secret de l'équipe ACTIVE > secret d'org > grant plateforme`. Un
    membre qui a sa propre clé, ou dont l'équipe active est une autre, ne verra jamais
    ce credential."""
    provider: str
    set_by: Optional[str] = None
    set_at: Optional[str] = None
    base_url: Optional[str] = None


class GroupDetail(BaseModel):
    """Fiche complète d'une équipe (autz `GROUP_MEMBER_OF`, donc lisible par un
    org_admin non membre via l'escalade).

    ⚠️ `secrets` liste ce que l'ÉQUIPE partage, pas ce dont ses membres disposent :
    ni les clés personnelles, ni le secret d'org, ni les grants plateforme n'y
    figurent. Une liste vide ne veut donc pas dire « cette équipe n'a accès à rien »."""
    group: GroupBrief
    members: list[GroupMemberEntry]
    secrets: list[GroupSecretEntry]


class GroupUpdated(BaseModel):
    """Renommage / re-description. ⚠️ **`ok: true` ne dit pas qu'une écriture a eu
    lieu** : c'est une constante d'écho. Envoyer `name: null, description: null` (rien
    à changer) répond exactement pareil, et le booléen que le store renvoie
    (« la ligne existait ») est jeté avant la réponse.

    Renommer vers un nom déjà pris dans l'org (comparaison insensible à la casse, le
    groupe s'excluant lui-même) rend **409 `group_exists`**, comme la création (#281).
    ⚠️ Ce texte a dit le CONTRAIRE jusqu'au 29/08/2026 (« heurte la contrainte UNIQUE,
    erreur serveur ») ; un front tiers l'avait lu et prévenait le conflit de son côté.
    Le refus est déclaré dans l'OpenAPI, avec son test de rejeu sur la route servie.

    Rien n'est réécho (ni le nouveau nom, ni la description) : relire `GET /api/groups/{id}`
    pour confirmer l'état."""
    ok: bool
    group_id: int


class GroupDeleted(BaseModel):
    """Suppression d'une équipe. **Irréversible et large** : partent avec elle les
    appartenances, le guide d'équipe et son historique de versions (cascade FK) et
    **les credentials partagés de l'équipe** (purgés explicitement du coffre, hors FK).
    La réponse ne dit rien de ce qui a été détruit.

    ⚠️ Conséquence invisible ici : les membres qui étaient servis par le secret
    d'équipe basculent **silencieusement** sur le secret d'org ou le grant plateforme
    à leur appel suivant — sans erreur, sous une autre identité côté fournisseur.

    `deleted: false` est quasi inatteignable (l'autz refuse déjà une équipe inconnue
    en 403) : ne pas en faire un cas d'usage, c'est un témoin de course."""
    ok: bool
    group_id: int
    deleted: bool


def _group_brief(g: dict, sub: str) -> dict:
    members = group_store.list_group_members(g["id"])
    return {
        "id": g["id"], "group_id": g["id"], "org_id": g["org_id"],
        "name": g["name"], "description": g.get("description", ""),
        "member_count": len(members),
        "my_role": group_store.get_group_role(g["id"], sub),
    }


def _create_group(ctx: ResolvedCtx, inp: CreateGroupInput) -> dict:
    if not org_store.get_org(inp.org_id):
        raise AuthzDenied(404, "unknown_org", f"Org #{inp.org_id} inconnue.")
    name = inp.name.strip()
    # Collision de nom (l'index UNIQUE (org_id, name) la rejetterait aussi, mais on
    # lève une erreur actionnable plutôt qu'une IntegrityError opaque).
    if any(g["name"].lower() == name.lower() for g in group_store.list_groups(inp.org_id)):
        raise AuthzDenied(409, "group_exists",
                          f"Un groupe `{name}` existe déjà dans cette org.")
    gid = group_store.create_group(inp.org_id, name, inp.description, created_by=ctx.sub)
    # Le créateur devient chef d'équipe du groupe (s'il est membre de l'org).
    if org_store.get_org_role(inp.org_id, ctx.sub) is not None:
        group_store.add_group_member(gid, ctx.sub, "group_admin")
    return {"id": gid, "group_id": gid, "org_id": inp.org_id, "name": inp.name.strip()}


def _list_groups(ctx: ResolvedCtx, inp: OrgIdInput) -> dict:
    out = []
    for g in group_store.list_groups(inp.org_id):
        out.append(_group_brief(g, ctx.sub))
    return {"org_id": inp.org_id, "groups": out}


class NoInput(BaseModel):
    pass


def _list_my_groups(ctx: ResolvedCtx, inp: NoInput) -> dict:
    """Groupes de l'org active du sub + son rôle + le groupe actif."""
    org_id = access.current_org(ctx.sub)
    if org_id is None:
        return {"org_id": None, "active_group": None, "groups": []}
    active_group = access.current_group(ctx.sub)
    mine = {g["group_id"]: g["group_role"] for g in
            group_store.list_groups_for_user(ctx.sub, org_id)}
    groups = []
    for g in group_store.list_groups(org_id):
        groups.append({
            "id": g["id"], "group_id": g["id"], "name": g["name"],
            "description": g.get("description", ""),
            "member_count": len(group_store.list_group_members(g["id"])),
            "my_role": mine.get(g["id"]),
            "active": g["id"] == active_group,
        })
    return {"org_id": org_id, "active_group": active_group, "groups": groups}


def _use_group(ctx: ResolvedCtx, inp: UseGroupInput) -> dict:
    """MCP = hint SANS ÉTAT (ADR 0038 B3 — le bracelet de session est retiré) :
    valide l'appartenance et renvoie le geste fiable (`_group=` par appel, qui
    co-pose l'org parente). REST = pose l'équipe MAISON persistante."""
    g = group_store.get_group(inp.group_id)
    if not g:
        raise AuthzDenied(404, "unknown_group", f"Groupe #{inp.group_id} inconnu.")
    sid = session_org.current_session_id()
    if sid is not None:  # MCP : hint sans état
        if not group_store.is_group_member(ctx.sub, inp.group_id):
            raise AuthzDenied(403, "not_a_member",
                              "Tu n'es pas membre de ce groupe — demande au chef d'équipe.")
        return {
            "group": inp.group_id, "name": g["name"], "org": g["org_id"],
            "session_state": None,
            "how_to": (f"Aucun état de session (ADR 0038) : passe `group={inp.group_id}` "
                       "sur chaque appel scopé équipe (l'org parente en est dérivée). "
                       "L'équipe par défaut ne se change que dans le dashboard."),
        }
    if not group_store.set_active_group(ctx.sub, inp.group_id):  # REST : maison (persiste)
        raise AuthzDenied(403, "not_a_member",
                          "Tu n'es pas membre de ce groupe — demande au chef d'équipe.")
    return {"active_group": inp.group_id, "name": g["name"], "active_org": g["org_id"]}


def _clear_group(ctx: ResolvedCtx, inp: NoInput) -> dict:
    """Retour au niveau org. MCP = hint sans état (plus de bracelet, ADR 0038 B3) ;
    REST = efface l'équipe maison."""
    sid = session_org.current_session_id()
    if sid is not None:
        return {"session_state": None,
                "how_to": ("Aucun état de session à effacer (ADR 0038). Sans `_group=`, "
                           "l'appel est au niveau org (ton équipe maison ne s'applique "
                           "que dans ton org maison) — le défaut durable se change dans "
                           "le dashboard.")}
    group_store.clear_active_group(ctx.sub)
    # Un membre d'équipe n'est jamais « sans équipe » (access.current_group) :
    # rendre l'équipe EFFECTIVE, pas un None qui mentirait.
    return {"active_group": access.current_group(ctx.sub)}


def _set_home_group(ctx: ResolvedCtx, inp: UseGroupInput) -> dict:
    """Pose l'équipe MAISON persistante (défaut des nouvelles conversations) + son
    org parente — depuis n'importe quelle face, ≠ oto_use_group (session)."""
    g = group_store.get_group(inp.group_id)
    if not g:
        raise AuthzDenied(404, "unknown_group", f"Groupe #{inp.group_id} inconnu.")
    if not group_store.set_active_group(ctx.sub, inp.group_id):
        raise AuthzDenied(403, "not_a_member",
                          "Tu n'es pas membre de ce groupe — demande au chef d'équipe.")
    return {"home_group": inp.group_id, "name": g["name"], "home_org": g["org_id"]}


def _group_detail(ctx: ResolvedCtx, inp: GroupIdInput) -> dict:
    g = group_store.get_group(inp.group_id)
    if not g:
        raise AuthzDenied(404, "unknown_group", f"Groupe #{inp.group_id} inconnu.")
    from ... import db
    members = []
    for m in group_store.list_group_members(inp.group_id):
        u = db.get_user(m["sub"]) or {}
        members.append({"sub": m["sub"], "email": u.get("email"), "name": u.get("name"),
                        "role": m["group_role"], "active": m["is_active"]})
    return {
        "group": _group_brief(g, ctx.sub),
        "members": members,
        "secrets": group_store.list_group_secrets(inp.group_id),
    }


def _update_group(ctx: ResolvedCtx, inp: UpdateGroupInput) -> dict:
    # Même conflit métier que `group.create`, donc même réponse : sans ce contrôle,
    # l'index UNIQUE (org_id, name) remontait une IntegrityError en 500 — un renommage
    # refusé répondait autrement qu'une création refusée (#281). Le groupe s'exclut
    # lui-même : se renommer en son propre nom n'est pas un conflit.
    if inp.name is not None:
        name = inp.name.strip()
        grp = group_store.get_group(inp.group_id)
        if grp and any(g["name"].lower() == name.lower() and g["id"] != inp.group_id
                       for g in group_store.list_groups(grp["org_id"])):
            raise AuthzDenied(409, "group_exists",
                              f"Un groupe `{name}` existe déjà dans cette org.")
    group_store.update_group(inp.group_id, name=inp.name, description=inp.description)
    return {"ok": True, "group_id": inp.group_id}


def _delete_group(ctx: ResolvedCtx, inp: GroupIdInput) -> dict:
    deleted = group_store.delete_group(inp.group_id)
    return {"ok": True, "group_id": inp.group_id, "deleted": deleted}


CAPABILITIES += [
    Capability(
        key="group.create", handler=_create_group, Input=CreateGroupInput,
        authz=ORG_ADMIN_OF("org_id"), Output=GroupCreated,
        errors=(DeclaredError(409, "group_exists",
                              "un groupe de ce nom existe déjà dans l'org (casse ignorée)"),),
        description=("Create a group (department/team) inside an org you administer. "
                     "You become its team lead (group_admin)."),
        rest=RestBinding("POST", "/api/orgs/{id}/groups", _OID),
    ),
    Capability(
        key="group.list", handler=_list_groups, Input=OrgIdInput,
        authz=ORG_MEMBER_OF("org_id"), Output=GroupList,
        description="List the groups (departments) of an org you belong to.",
        rest=RestBinding("GET", "/api/orgs/{id}/groups", _OID),
    ),
    Capability(
        key="group.use", handler=_use_group, Input=UseGroupInput, authz=SUB_ONLY,
        Output=UseGroupResult,
        description=("Resolve a group (department) you belong to and get the RELIABLE "
                     "way to act under it. NO session state (ADR 0038): pass "
                     "`group=<id>` directly on each group-scoped call (its parent org "
                     "is derived). Your default group is changed in the dashboard "
                     "only. The group decides which group guide and shared "
                     "secrets apply."),
        mcp="oto_use_group",
        rest=RestBinding("PUT", "/api/me/active-group"),  # REST : équipe maison
    ),
    Capability(
        key="group.clear", handler=_clear_group, Input=NoInput, authz=SUB_ONLY,
        Output=ClearGroupResult,
        description=("No-op hint (ADR 0038: no session state). Without a `_group=` "
                     "token a call is at org level; the durable default is changed "
                     "in the dashboard only."),
        mcp="oto_clear_group",
        rest=RestBinding("DELETE", "/api/me/active-group"),  # REST : efface l'équipe maison
    ),
    Capability(
        key="group.set_home", handler=_set_home_group, Input=UseGroupInput, authz=SUB_ONLY,
        Output=HomeGroupSet,
        description=("Set the HOME group (department) — persistent default. UI-ONLY "
                     "(décision 2026-07-06, comme org.set_home) : pas de binding MCP, "
                     "l'agent ne mute pas le défaut (il pose aussi l'org parente en "
                     "maison — double mutation)."),
        rest=RestBinding("PUT", "/api/me/home-group"),
    ),
    Capability(
        key="group.get", handler=_group_detail, Input=GroupIdInput,
        authz=GROUP_MEMBER_OF("group_id"), Output=GroupDetail,
        description="Group detail (members, shared secrets).",
        rest=RestBinding("GET", "/api/groups/{id}", _GID),
    ),
    Capability(
        key="group.update", handler=_update_group, Input=UpdateGroupInput,
        authz=GROUP_ADMIN_OF("group_id"), Output=GroupUpdated,
        errors=(DeclaredError(409, "group_exists",
                              "le nouveau nom est déjà pris dans l'org (casse ignorée)"),),
        description="Rename / re-describe a group you lead.",
        rest=RestBinding("PATCH", "/api/groups/{id}", _GID),
    ),
    Capability(
        key="group.delete", handler=_delete_group, Input=GroupIdInput,
        authz=GROUP_ADMIN_OF("group_id"), Output=GroupDeleted,
        description="Delete a group you lead (members/guide/secrets purged).",
        rest=RestBinding("DELETE", "/api/groups/{id}", _GID),
    ),
]
