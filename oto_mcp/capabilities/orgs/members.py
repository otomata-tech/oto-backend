"""Capacités d'écriture sur les membres d'org (ADR 0009, barreau 2).

Réconcilie la divergence d'autz : la MÊME opération était platform-admin-only
en MCP et org_admin self-service en REST. Unifiée sur **`ORG_ADMIN_OF`**
(org_admin de l'org, platform-admin par escalade) — décision utilisateur. Les
deux chemins REST historiques (self `/api/orgs/{id}/…` + admin
`/api/admin/orgs/{id}/…`) sont conservés via le multi-binding (même
handler+autz) pour ne casser aucune vue du dashboard.

Contrat MCP : `org_id` (entier) remplace l'ancien `org` (id-ou-nom).
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from ... import db, org_store
from .._authz import ORG_ADMIN_OF, SUB_ONLY
from .._types import AuthzDenied, Capability, DeclaredError, ResolvedCtx, RestBinding
from ..registry import CAPABILITIES

_ID = {"id": "org_id"}  # placeholder de route {id} → champ Input org_id


def _resolve_target(target: str) -> str:
    """Email (d'un user déjà connecté) ou sub direct → sub. Lève AuthzDenied neutre."""
    target = (target or "").strip()
    if not target:
        raise AuthzDenied(400, "missing_target", "Cible (email ou sub) requise.")
    if "@" in target:
        # ⚠️ Une adresse ne désigne PAS un compte. Le même email peut porter
        # plusieurs comptes — le nôtre et celui d'un tenant tiers, qualifiés par
        # émetteur (ADR 0052). Jusqu'au 05/09/2026 on en prenait UN, celui que la
        # base rendait en premier, sans ordre fixé et sans le dire.
        #
        # Ce que ça coûtait, mesuré : une même adresse personnelle porte deux
        # comptes, 91 et 98 appels sur trente jours. Filtrer le monitoring par
        # cette adresse rendait 91 et taisait 98 — un chiffre PLAUSIBLE, jamais un
        # zéro, donc rien pour alerter. Et la même résolution sert à suspendre un
        # compte et à changer un rôle : on y jouait à pile ou face entre deux
        # homonymes.
        #
        # On refuse donc, en NOMMANT les candidats : l'appelant repasse avec un
        # sub, qui est sans ambiguïté. Deviner à sa place serait pire que refuser.
        users = db.get_users_by_email(target)
        if not users:
            raise AuthzDenied(400, "unknown_user",
                              f"Aucun user connu avec l'email `{target}` (doit s'être connecté une fois).")
        if len(users) > 1:
            subs = ", ".join(f"`{u['sub']}`" for u in users)
            raise AuthzDenied(
                400, "ambiguous_email",
                f"L'email `{target}` désigne {len(users)} comptes : {subs}. "
                "Une adresse n'identifie pas un compte — deux émetteurs peuvent la "
                "porter. Reprends avec le `sub` de celui que tu vises.")
        return users[0]["sub"]
    return target


def _count_org_admins(org_id: int) -> int:
    return sum(1 for m in org_store.list_org_members(org_id) if m["org_role"] == "org_admin")


def _check_role(role: str) -> str:
    if role not in org_store.ORG_ROLES:
        raise AuthzDenied(400, "invalid_role", f"Rôle invalide : {role!r}.")
    return role


def _require_org_exists(org_id: int) -> None:
    if not org_store.get_org(org_id):
        raise AuthzDenied(404, "unknown_org", f"Org #{org_id} inconnue.")


class MemberWritten(BaseModel):
    """Écho d'un ajout de membre ou d'un changement de rôle.

    ⚠️ `sub` est le sub **RÉSOLU** : l'entrée accepte un email, la réponse ne le
    renvoie jamais — un client qui a envoyé un email doit garder sa propre trace
    pour rapprocher requête et réponse.

    ⚠️ **L'ajout est un UPSERT, pas une création** : ajouter quelqu'un qui est déjà
    membre ne rend pas de conflit, ça écrase son rôle. `POST /members` sur un membre
    existant est donc un changement de rôle déguisé — et il porte le MÊME anti-lockout
    que `POST /members/{sub}` (409 `last_org_admin` sur le dernier org_admin) depuis
    #273 ; avant ce correctif il était la route par laquelle on rétrogradait ce que
    l'autre refusait.

    ⚠️ Effet de bord invisible dans la réponse : une **première** adhésion peut
    devenir l'org MAISON de la personne (si elle n'avait que son espace personnel) —
    on change donc son contexte de travail par défaut, pas seulement son
    appartenance."""
    ok: bool
    org_id: int
    sub: str
    # Le rôle EFFECTIF après écriture (validé contre `org_store.ORG_ROLES`).
    role: str


class MemberRemoved(BaseModel):
    """Retrait d'un membre par un org_admin. ⚠️ `removed` ne vaut **jamais** `false` :
    l'absence d'appartenance lève un 404 et le dernier org_admin un 409. C'est une
    constante d'écho, pas un verdict à tester.

    `sub` est le sub RÉSOLU (l'entrée acceptait un email)."""
    ok: bool
    org_id: int
    sub: str
    removed: bool


class AddMemberInput(BaseModel):
    org_id: int
    target: str                       # email ou sub
    role: str = "org_member"


class SetMemberRoleInput(BaseModel):
    org_id: int
    sub: str                          # membre cible (sub)
    role: str


class RemoveMemberInput(BaseModel):
    org_id: int
    target: str                       # email ou sub (route {sub} → target)


class LeaveOrgInput(BaseModel):
    org_id: int


def _write_member_role(org_id: int, sub: str, role: str, *, require_member: bool) -> dict:
    """Écrit le rôle d'un membre — le geste métier commun aux DEUX capacités d'écriture.

    `org_store.add_org_member` est un **upsert** : « ajouter » et « changer le rôle »
    touchent la même ligne, donc c'est la même opération et elle doit porter les mêmes
    gardes. Tant que l'anti-lockout vivait dans le seul `_set_member_role`, `POST /members`
    rétrogradait le dernier org_admin que `POST /members/{sub}` refusait de toucher (#273) —
    une règle tenue à deux endroits n'est tenue qu'au premier qu'on relit. D'où ce point
    de passage unique : toute nouvelle surface d'écriture de rôle passe par ici.

    `require_member` = la SEULE divergence légitime entre les deux : `set_role` exige une
    appartenance préexistante (404 sinon), `add` l'accepte absente — c'est son objet.
    """
    current = org_store.get_org_role(org_id, sub)
    if current is None and require_member:
        raise AuthzDenied(404, "not_a_member", "Cible non-membre de l'org.")
    # Anti-lockout : ne pas rétrograder le dernier org_admin, par quelque route que ce soit.
    if current == "org_admin" and role != "org_admin" and _count_org_admins(org_id) <= 1:
        raise AuthzDenied(409, "last_org_admin", "Impossible de rétrograder le dernier org_admin.")
    org_store.add_org_member(org_id, sub, role)
    return {"ok": True, "org_id": org_id, "sub": sub, "role": role}


def _add_member(ctx: ResolvedCtx, inp: AddMemberInput) -> dict:
    _require_org_exists(inp.org_id)
    role = _check_role(inp.role)
    target_sub = _resolve_target(inp.target)
    return _write_member_role(inp.org_id, target_sub, role, require_member=False)


def _set_member_role(ctx: ResolvedCtx, inp: SetMemberRoleInput) -> dict:
    _require_org_exists(inp.org_id)
    role = _check_role(inp.role)
    return _write_member_role(inp.org_id, inp.sub, role, require_member=True)


def _remove_member(ctx: ResolvedCtx, inp: RemoveMemberInput) -> dict:
    target_sub = _resolve_target(inp.target)
    # Anti-lockout : ne pas retirer le dernier org_admin.
    if org_store.get_org_role(inp.org_id, target_sub) == "org_admin" and _count_org_admins(inp.org_id) <= 1:
        raise AuthzDenied(409, "last_org_admin", "Impossible de retirer le dernier org_admin.")
    if not org_store.remove_org_member(inp.org_id, target_sub):
        raise AuthzDenied(404, "not_a_member", "Cible non-membre de l'org.")
    return {"ok": True, "org_id": inp.org_id, "sub": target_sub, "removed": True}


def _leave_org(ctx: ResolvedCtx, inp: LeaveOrgInput) -> dict:
    """Auto-retrait : l'appelant quitte une org dont il est membre (SUB_ONLY). Distinct
    de `org.member.remove` (org_admin retire un TIERS). L'org active bascule côté store
    (`remove_org_member` promeut la plus ancienne restante ; l'org perso est le repli)."""
    _require_org_exists(inp.org_id)
    # On ne quitte pas son espace perso (c'est le repli d'identité, jamais supprimable).
    if org_store.is_personal_org(inp.org_id):
        raise AuthzDenied(409, "personal_org", "On ne peut pas quitter son espace personnel.")
    role = org_store.get_org_role(inp.org_id, ctx.sub)
    if role is None:
        raise AuthzDenied(404, "not_a_member", "Tu n'es pas membre de cette org.")
    # Anti-lockout : le dernier org_admin doit nommer un successeur avant de partir.
    if role == "org_admin" and _count_org_admins(inp.org_id) <= 1:
        raise AuthzDenied(409, "last_org_admin",
                          "Tu es le dernier admin — nomme un autre admin avant de quitter l'org.")
    if not org_store.remove_org_member(inp.org_id, ctx.sub):
        raise AuthzDenied(404, "not_a_member", "Tu n'es pas membre de cette org.")
    return {"ok": True, "org_id": inp.org_id, "left": True}


class LeftOrg(BaseModel):
    """Auto-retrait réussi. `ok`/`left` sont redondants par HISTOIRE (le dashboard lit
    `ok`), pas par design — ne pas en déduire un motif d'enveloppe."""
    ok: bool
    org_id: int
    left: bool


CAPABILITIES += [
    Capability(
        key="org.member.add", handler=_add_member, Input=AddMemberInput,
        authz=ORG_ADMIN_OF("org_id"), Output=MemberWritten,
        description="Add a member (by email or sub) to an org you administer. role: org_member|org_admin.",
        # MCP fusionné dans oto_admin_org_member(op=add). REST conservé (dashboard).
        rest=(RestBinding("POST", "/api/orgs/{id}/members", _ID),
              RestBinding("POST", "/api/admin/orgs/{id}/members", _ID)),
    ),
    Capability(
        key="org.member.set_role", handler=_set_member_role, Input=SetMemberRoleInput,
        authz=ORG_ADMIN_OF("org_id"), Output=MemberWritten,
        description="Change a member's role in an org you administer (org_member|org_admin).",
        rest=(RestBinding("POST", "/api/orgs/{id}/members/{sub}", _ID),
              RestBinding("POST", "/api/admin/orgs/{id}/members/{sub}", _ID)),
    ),
    Capability(
        key="org.member.remove", handler=_remove_member, Input=RemoveMemberInput,
        authz=ORG_ADMIN_OF("org_id"), Output=MemberRemoved,
        description="Remove a member (by email or sub) from an org you administer.",
        # MCP fusionné dans oto_admin_org_member(op=remove). REST conservé (dashboard).
        rest=(RestBinding("DELETE", "/api/orgs/{id}/members/{sub}", {"id": "org_id", "sub": "target"}),
              RestBinding("DELETE", "/api/admin/orgs/{id}/members/{sub}", {"id": "org_id", "sub": "target"})),
    ),
    Capability(
        key="me.leave_org", handler=_leave_org, Input=LeaveOrgInput,
        authz=SUB_ONLY, Output=LeftOrg,
        # Dans l'ORDRE des gardes du handler : c'est un contrat (le premier refus qui
        # s'applique est rendu, les suivants ne sont pas évalués).
        errors=(DeclaredError(404, "unknown_org", "l'org n'existe pas"),
                DeclaredError(409, "personal_org",
                              "on ne quitte pas son espace personnel"),
                DeclaredError(404, "not_a_member", "tu n'es pas membre de cette org"),
                DeclaredError(409, "last_org_admin",
                              "tu es le dernier admin — nomme un successeur avant")),
        description="Leave an org you belong to (self-removal). Refused for your personal org or if you are its last admin.",
        # Self-service dashboard (REST-only) : quitter depuis « paramètres » de l'org.
        rest=RestBinding("DELETE", "/api/me/orgs/{id}/membership", {"id": "org_id"}),
    ),
]
