"""Les règles d'autz de la couche capacité (ADR 0009 §7) — liste FERMÉE.

Chaque règle prend `(RawCtx, input)` et renvoie un `ResolvedCtx`, ou lève
`AuthzDenied` (neutre). Elles **réutilisent** la logique d'autz existante
(`access`, `org_store`, et le résolveur de hiérarchie `roles`) — source unique,
pas de duplication.

L'escalade descendante (platform_admin > org_admin > group_admin > member) est
portée par `roles.py` (ADR 0012), pas recopiée ici : `ORG_ADMIN_OF`,
`GROUP_ADMIN_OF`, etc. délèguent au résolveur central. Ajouter un palier = un
seul endroit.

Depuis le retrait du transport stdio (2026-06-13) le serveur est toujours
authentifié : plus de branche `sub is None` → accès complet. `sub` absent = refus.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from .. import access, group_store, roles
from ._types import AuthzDenied, RawCtx, ResolvedCtx


def _require_sub(raw: RawCtx) -> str:
    if not raw.sub:
        raise AuthzDenied(401, "auth_required", "Authentification requise.")
    return raw.sub


def SUB_ONLY(raw: RawCtx, inp: Optional[BaseModel] = None) -> ResolvedCtx:
    """Tout user authentifié (datastore, méta user-tools, oto_use_org)."""
    sub = _require_sub(raw)
    return ResolvedCtx(sub=sub, org_id=access.current_org(sub),
                       role=access.get_user_role(sub))


def ORG_MEMBER(raw: RawCtx, inp: Optional[BaseModel] = None) -> ResolvedCtx:
    """Membre d'une org active — injecte `org_id` depuis l'état serveur (jamais
    d'un param client). Verrouille l'IDOR cross-org par construction."""
    sub = _require_sub(raw)
    org_id = access.current_org(sub)
    if org_id is None:
        raise AuthzDenied(400, "no_active_org",
                          "Aucune org active — choisis-en une avec oto_use_org.")
    return ResolvedCtx(sub=sub, org_id=org_id, role=access.get_user_role(sub))


def ORG_ADMIN(raw: RawCtx, inp: Optional[BaseModel] = None) -> ResolvedCtx:
    """Org-admin de l'org ACTIVE — écriture self-service scopée à l'org active
    (miroir écriture d'`ORG_MEMBER`). `org_id` injecté depuis l'état serveur, jamais
    d'un param client. Escalade super_admin via `roles.is_org_admin` (parité exacte
    avec le legacy `_resolve_org_write`/`_active_org_edit` : seul le super escalade)."""
    sub = _require_sub(raw)
    org_id = access.current_org(sub)
    if org_id is None:
        raise AuthzDenied(400, "no_active_org",
                          "Aucune org active — choisis-en une avec oto_use_org.")
    if not roles.is_org_admin(sub, org_id):
        raise AuthzDenied(403, "forbidden", "Réservé à un org_admin de ton org active.")
    return ResolvedCtx(sub=sub, org_id=org_id, role=access.get_user_role(sub))


def PLATFORM_ADMIN(raw: RawCtx, inp: Optional[BaseModel] = None) -> ResolvedCtx:
    """Admin opérationnel (admin ou super_admin) — supervision plateforme sans
    l'escalade en masse vers les orgs tierces (réservée à SUPER_ADMIN)."""
    sub = _require_sub(raw)
    if not access.is_platform_operator(sub):
        raise AuthzDenied(403, "forbidden", "Réservé à un admin plateforme.")
    return ResolvedCtx(sub=sub, org_id=access.current_org(sub),
                       role=access.get_user_role(sub))


def SUPER_ADMIN(raw: RawCtx, inp: Optional[BaseModel] = None) -> ResolvedCtx:
    """Super admin uniquement — le tout-puissant (rôles plateforme, keys, tokens,
    écriture sur orgs tierces, création d'org, entitlements)."""
    sub = _require_sub(raw)
    if not access.is_super_admin(sub):
        raise AuthzDenied(403, "forbidden", "Réservé au super admin.")
    return ResolvedCtx(sub=sub, org_id=access.current_org(sub),
                       role=access.get_user_role(sub))


def NAMESPACE_GRANT(namespace: str):
    """Grant per-user OU entitlement d'org sur un namespace gouverné (escalade
    platform_admin incluse). Renvoie une règle paramétrée par `namespace`."""
    def rule(raw: RawCtx, inp: Optional[BaseModel] = None) -> ResolvedCtx:
        sub = _require_sub(raw)
        role = access.get_user_role(sub)
        if not access.is_super_admin(sub) and namespace not in access.granted_namespaces_for(sub):
            raise AuthzDenied(403, "namespace_not_granted",
                              f"Accès au namespace `{namespace}` non accordé.")
        return ResolvedCtx(sub=sub, org_id=access.current_org(sub), role=role)
    return rule


def ADMIN_BY_OP(by_op: dict, *, field: str = "op"):
    """Autz **op-aware** : choisit la règle d'autz selon `input.<field>` (typiquement
    `op`). Permet à un outil consolidé `*_op` de réunir des verbes à paliers d'autz
    différents (ex. lecture `PLATFORM_ADMIN`, mutation `SUPER_ADMIN`) **sans** redescendre
    l'autz dans le handler : l'autz reste DÉCLARÉE au niveau de la capacité, juste
    paramétrée par op (esprit ADR 0009 §7 préservé — pas de drift, pas d'oubli). Chaque
    branche est une règle fermée de ce module ; un op hors map = refus net (jamais
    fail-open). La validation de `op` reste portée par le `Literal` de l'Input."""
    def rule(raw: RawCtx, inp: Optional[BaseModel] = None) -> ResolvedCtx:
        op = getattr(inp, field, None) if inp is not None else None
        chosen = by_op.get(op)
        if chosen is None:
            raise AuthzDenied(400, "unsupported_op",
                              f"op `{op}` non supporté (attendu : {sorted(by_op)}).")
        return chosen(raw, inp)
    return rule


def RESOURCE_GOVERN(*, type_field: str = "resource_type", id_field: str = "resource_id",
                    op_field: str = "op", list_ops: tuple[str, ...] = ("list",)):
    """Gouvernance d'une ressource possédée (ADR 0030) : owner ∪ escalade `roles.py`,
    résolu par `ownership.can_govern(sub, resource_type, resource_id)`. Couvre owner
    self-service ET super_admin/org_admin/group_admin en une règle. Les ops de
    `list_ops` (qui n'ont pas de `resource_id`) sont autorisées à tout authentifié —
    le handler FILTRE aux ressources gouvernables. Import paresseux d'`ownership`
    (évite tout cycle au chargement des modules de capacités)."""
    def rule(raw: RawCtx, inp: Optional[BaseModel] = None) -> ResolvedCtx:
        sub = _require_sub(raw)
        op = getattr(inp, op_field, None) if inp is not None else None
        if op in list_ops:
            return ResolvedCtx(sub=sub, org_id=access.current_org(sub),
                               role=access.get_user_role(sub))
        rtype = getattr(inp, type_field, None) if inp is not None else None
        rid = getattr(inp, id_field, None) if inp is not None else None
        if not rtype or rid is None:
            raise AuthzDenied(400, "missing_resource",
                              "`resource_type` et `resource_id` requis.")
        from .. import ownership
        if not ownership.can_govern(sub, rtype, str(rid)):
            raise AuthzDenied(403, "forbidden",
                              "Gouvernance de cette ressource refusée.")
        return ResolvedCtx(sub=sub, org_id=access.current_org(sub),
                           role=access.get_user_role(sub))
    return rule


def _field_int(inp: Optional[BaseModel], field: str, code: str, label: str) -> int:
    val = getattr(inp, field, None) if inp is not None else None
    if val is None:
        raise AuthzDenied(400, code, f"Champ `{field}` requis.")
    return int(val)


def ORG_MEMBER_OF(field: str):
    """Membre de l'org désignée par `input.<field>` (lecture d'une org par id de
    path, ≠ org active) — escalade platform_admin incluse via `roles`. Miroir
    lecture d'`ORG_ADMIN_OF`."""
    def rule(raw: RawCtx, inp: Optional[BaseModel] = None) -> ResolvedCtx:
        sub = _require_sub(raw)
        org_id = _field_int(inp, field, "missing_org", field)
        if not roles.is_org_member(sub, org_id):
            raise AuthzDenied(403, "forbidden", f"Réservé aux membres de l'org #{org_id}.")
        return ResolvedCtx(sub=sub, org_id=org_id, role=access.get_user_role(sub))
    return rule


def ORG_ADMIN_OF(field: str):
    """Org-admin de l'org désignée par `input.<field>` — escalade platform_admin
    incluse via `roles` (ADR 0012). Porte la garde « dernier admin » au niveau
    handler/store."""
    def rule(raw: RawCtx, inp: Optional[BaseModel] = None) -> ResolvedCtx:
        sub = _require_sub(raw)
        org_id = _field_int(inp, field, "missing_org", field)
        if not roles.is_org_admin(sub, org_id):
            raise AuthzDenied(403, "forbidden", f"Réservé à un org_admin de l'org #{org_id}.")
        return ResolvedCtx(sub=sub, org_id=org_id, role=access.get_user_role(sub))
    return rule


def GROUP_MEMBER_OF(field: str):
    """Lecture d'un groupe désigné par `input.<field>` : membre du groupe, OU
    org_admin du groupe parent, OU platform_admin (escalade descendante `roles`).
    Injecte `group_id` + l'`org_id` parent dans le ResolvedCtx."""
    def rule(raw: RawCtx, inp: Optional[BaseModel] = None) -> ResolvedCtx:
        sub = _require_sub(raw)
        group_id = _field_int(inp, field, "missing_group", field)
        g = group_store.get_group(group_id)
        if g is None:
            raise AuthzDenied(404, "unknown_group", f"Groupe #{group_id} inconnu.")
        if not roles.can_read_group(sub, group_id):
            raise AuthzDenied(403, "forbidden", f"Réservé aux membres du groupe #{group_id}.")
        return ResolvedCtx(sub=sub, org_id=g["org_id"], group_id=group_id,
                           role=access.get_user_role(sub))
    return rule


def GROUP_ADMIN_OF(field: str):
    """Écriture sur un groupe désigné par `input.<field>` : chef d'équipe
    (`group_admin`), OU org_admin du groupe parent, OU platform_admin (escalade
    descendante `roles`, ADR 0012). Injecte `group_id` + `org_id` parent."""
    def rule(raw: RawCtx, inp: Optional[BaseModel] = None) -> ResolvedCtx:
        sub = _require_sub(raw)
        group_id = _field_int(inp, field, "missing_group", field)
        g = group_store.get_group(group_id)
        if g is None:
            raise AuthzDenied(404, "unknown_group", f"Groupe #{group_id} inconnu.")
        if not roles.can_admin_group(sub, group_id):
            raise AuthzDenied(403, "forbidden",
                              f"Réservé au chef d'équipe (ou org_admin) du groupe #{group_id}.")
        return ResolvedCtx(sub=sub, org_id=g["org_id"], group_id=group_id,
                           role=access.get_user_role(sub))
    return rule
