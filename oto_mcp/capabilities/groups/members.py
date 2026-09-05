"""Capacités d'écriture sur les membres d'un groupe (ADR 0012).

Autz = `GROUP_ADMIN_OF` (chef d'équipe, org_admin parent, ou platform_admin par
escalade `roles`). INVARIANT : on n'ajoute au groupe qu'un membre DÉJÀ dans l'org
parente (l'appartenance groupe est subordonnée à l'org).

**Garantie anti-lockout (#280) : une équipe a toujours quelqu'un qui peut
l'administrer, le responsable d'organisation compris.** Ce n'est PAS « l'équipe garde
un chef explicite » : la hiérarchie de `roles.py` fait qu'un `org_admin` administre
toutes les équipes de son org, donc retirer ou rétrograder le dernier chef d'une
équipe est **normal et autorisé** — l'ancienne garde `last_group_admin` protégeait
contre un verrouillage qui n'existait pas. Ce qui est refusé, c'est le seul état
vraiment mort : zéro chef explicite ET zéro responsable dans l'org parente (état
atteignable — `oto_admin_org op=create` crée une org sans aucun membre).
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from ... import db, group_store, org_store
from .._authz import GROUP_ADMIN_OF
from .. import _identite
from .._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from ..registry import CAPABILITIES

_GID = {"id": "group_id"}


def _resolve_target(target: str) -> str:
    """Cible d'un ajout/retrait de membre : email accepté, ambiguïté refusée.

    ⚠️ Un groupe porte des données. Ajouter le mauvais homonyme les ouvre à
    quelqu'un qu'on ne visait pas, et le retirer laisse l'autre dedans — les
    deux erreurs sont silencieuses. Le refus vient de `_identite`.
    """
    target = (target or "").strip()
    if not target:
        raise AuthzDenied(400, "missing_target", "Cible (email ou sub) requise.")
    if "@" in target:
        porteurs = _identite.porteurs_de(target)
        if not porteurs:
            raise AuthzDenied(400, "unknown_user",
                              f"Aucun user connu avec l'email `{target}`.")
        return porteurs[0]["sub"]
    return target


def _check_role(role: str) -> str:
    if role not in group_store.GROUP_ROLES:
        raise AuthzDenied(400, "invalid_role", f"Rôle de groupe invalide : {role!r}.")
    return role


class AddGroupMemberInput(BaseModel):
    group_id: int
    target: str
    role: str = "group_member"


class SetGroupMemberRoleInput(BaseModel):
    group_id: int
    sub: str
    role: str


class RemoveGroupMemberInput(BaseModel):
    group_id: int
    target: str


# --- Sorties ----------------------------------------------------------------

class GroupMemberWritten(BaseModel):
    """Écho d'un ajout de membre d'équipe ou d'un changement de rôle.

    ⚠️ `sub` est le sub **RÉSOLU** : l'entrée accepte un email, la réponse ne le
    renvoie jamais — garder sa propre trace pour rapprocher requête et réponse.

    ⚠️ **L'ajout est un UPSERT** (`ON CONFLICT DO UPDATE`) : ajouter quelqu'un déjà
    membre de l'équipe ne rend pas de conflit, ça écrase son rôle. `POST /members` sur
    un membre existant est donc un changement de rôle déguisé — et il porte depuis #280
    la MÊME garde que `POST /members/{sub}` (les deux passent par `_write_member_role`) ;
    avant ce correctif il était la route par laquelle on rétrogradait ce que l'autre
    refusait, comme #273 un palier plus haut.

    ⚠️ **Rétrograder le dernier chef explicite d'une équipe PASSE** (et c'est voulu) :
    un `org_admin` administre toutes les équipes de son org, donc l'équipe reste
    administrable. Le seul refus est `group_unadministrable` (409) — plus personne, org
    comprise, ne pourrait l'administrer. Ne pas lire un succès ici comme « l'équipe a
    encore un chef » : elle peut légitimement n'en avoir **aucun** d'explicite (cf.
    `GroupCreated` : créée par quelqu'un d'extérieur à l'org).

    ⚠️ Aucun effet sur l'équipe ACTIVE : ajouter quelqu'un ne le fait pas travailler
    sous cette équipe (il faut qu'il la choisisse). Tant qu'il ne l'a pas fait, le
    secret partagé de l'équipe ne le sert pas."""
    ok: bool
    group_id: int
    sub: str
    # Rôle EFFECTIF après écriture, validé contre `group_store.GROUP_ROLES`.
    role: str


class GroupMemberRemoved(BaseModel):
    """Retrait d'un membre d'équipe. ⚠️ `removed` ne vaut **jamais** `false` : l'absence
    d'appartenance lève un 404, et un retrait qui laisserait l'équipe sans personne pour
    l'administrer (org comprise) un 409 `group_unadministrable`. C'est une constante
    d'écho, pas un verdict à tester.

    ⚠️ Retirer le dernier chef explicite PASSE dès lors que l'org a un responsable — il
    administre l'équipe (#280). Le retrait n'est donc pas plus strict que la
    rétrogradation : même garde, même critère.

    ⚠️ Effet de bord invisible dans la réponse : si l'équipe retirée était l'équipe
    ACTIVE de la personne, elle se retrouve **sans équipe active** — donc au niveau org
    à son appel suivant, et **plus servie par le secret partagé de l'équipe** (bascule
    silencieuse vers le secret d'org ou le grant plateforme, sans erreur).

    `sub` est le sub RÉSOLU (l'entrée acceptait un email)."""
    ok: bool
    group_id: int
    sub: str
    removed: bool


def _guard_group_stays_administrable(group_id: int, org_id: int,
                                     current_role: Optional[str],
                                     new_role: Optional[str]) -> None:
    """Garantie : une équipe a toujours quelqu'un qui peut l'administrer, le responsable
    d'organisation compris. `new_role=None` = retrait du membre.

    Trois sorties sans refus, dans l'ordre du moins cher au plus cher : personne ne perd
    son galon ; il reste un chef explicite ; l'org a un responsable — qui administre
    TOUTES ses équipes (`roles.can_admin_group`), donc rien à protéger. Reste le seul
    état mort, refusé.

    ⚠️ Le `platform_admin` est délibérément HORS du décompte : il administre toute équipe
    par escalade, donc l'inclure rendrait cette garde inatteignable (il en existe toujours
    un). La garantie porte sur l'administration de l'ORG, pas sur l'échappatoire
    plateforme.
    """
    if current_role != "group_admin" or new_role == "group_admin":
        return
    if group_store.count_group_admins(group_id) > 1:
        return
    if any(m["org_role"] == "org_admin" for m in org_store.list_org_members(org_id)):
        return
    raise AuthzDenied(409, "group_unadministrable",
                      "Personne ne pourrait plus administrer cette équipe : elle perdrait "
                      "son dernier chef et son org n'a aucun responsable. Nommer un "
                      "responsable d'org, ou un autre chef d'équipe, d'abord.")


def _write_member_role(group_id: int, org_id: int, sub: str, role: str, *,
                       require_member: bool) -> dict:
    """Écrit le rôle d'un membre d'équipe — le geste métier commun aux DEUX capacités.

    `group_store.add_group_member` est un **upsert** : « ajouter » et « changer le rôle »
    touchent la même ligne, donc c'est la même opération et elle doit porter les mêmes
    gardes. Tant que l'anti-lockout vivait dans le seul `_set_member_role`,
    `POST /members` rétrogradait le dernier chef que `POST /members/{sub}` refusait de
    toucher (#280, même défaut qu'#273 un palier plus haut) — une règle tenue à deux
    endroits n'est tenue qu'au premier qu'on relit. D'où ce point de passage unique :
    toute nouvelle surface d'écriture de rôle d'équipe passe par ici.

    `require_member` = la SEULE divergence légitime entre les deux : `set_role` exige une
    appartenance préexistante (404 sinon), `add` l'accepte absente — c'est son objet.
    (L'invariant « déjà membre de l'org parente » reste chez `_add_member` : la 404 de
    `set_role` le subsume, un membre d'équipe étant nécessairement membre de l'org.)
    """
    current = group_store.get_group_role(group_id, sub)
    if current is None and require_member:
        raise AuthzDenied(404, "not_a_member", "Cible non-membre du groupe.")
    _guard_group_stays_administrable(group_id, org_id, current, role)
    group_store.add_group_member(group_id, sub, role)
    return {"ok": True, "group_id": group_id, "sub": sub, "role": role}


def _add_member(ctx: ResolvedCtx, inp: AddGroupMemberInput) -> dict:
    role = _check_role(inp.role)
    target_sub = _resolve_target(inp.target)
    # ctx.org_id = org parente injectée par GROUP_ADMIN_OF (jamais un param client).
    if org_store.get_org_role(ctx.org_id, target_sub) is None:
        raise AuthzDenied(409, "not_org_member",
                          "La cible doit d'abord être membre de l'org parente.")
    return _write_member_role(inp.group_id, ctx.org_id, target_sub, role,
                              require_member=False)


def _set_member_role(ctx: ResolvedCtx, inp: SetGroupMemberRoleInput) -> dict:
    role = _check_role(inp.role)
    return _write_member_role(inp.group_id, ctx.org_id, inp.sub, role,
                              require_member=True)


def _remove_member(ctx: ResolvedCtx, inp: RemoveGroupMemberInput) -> dict:
    target_sub = _resolve_target(inp.target)
    # Même garantie que l'écriture de rôle : sans quoi le retrait resterait le chemin
    # strict d'une garde que la rétrogradation autorise — donc contournable en deux
    # appels (rétrograder puis retirer), c.-à-d. décoratif.
    _guard_group_stays_administrable(inp.group_id, ctx.org_id,
                                     group_store.get_group_role(inp.group_id, target_sub),
                                     None)
    if not group_store.remove_group_member(inp.group_id, target_sub):
        raise AuthzDenied(404, "not_a_member", "Cible non-membre du groupe.")
    return {"ok": True, "group_id": inp.group_id, "sub": target_sub, "removed": True}


CAPABILITIES += [
    Capability(
        key="group.member.add", handler=_add_member, Input=AddGroupMemberInput,
        authz=GROUP_ADMIN_OF("group_id"), Output=GroupMemberWritten,
        description=("Add a member (by email or sub) to a group you lead. The target "
                     "must already belong to the parent org. role: group_member|group_admin."),
        rest=RestBinding("POST", "/api/groups/{id}/members", _GID),
    ),
    Capability(
        key="group.member.set_role", handler=_set_member_role, Input=SetGroupMemberRoleInput,
        authz=GROUP_ADMIN_OF("group_id"), Output=GroupMemberWritten,
        description="Change a member's role in a group you lead (group_member|group_admin).",
        rest=RestBinding("POST", "/api/groups/{id}/members/{sub}", _GID),
    ),
    Capability(
        key="group.member.remove", handler=_remove_member, Input=RemoveGroupMemberInput,
        authz=GROUP_ADMIN_OF("group_id"), Output=GroupMemberRemoved,
        description="Remove a member (by email or sub) from a group you lead.",
        rest=RestBinding("DELETE", "/api/groups/{id}/members/{sub}",
                         {"id": "group_id", "sub": "target"}),
    ),
]
