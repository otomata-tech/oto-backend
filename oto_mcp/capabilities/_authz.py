"""Les 5 règles d'autz de la couche capacité (ADR 0009 §7) — liste FERMÉE.

Chaque règle prend `(RawCtx, input)` et renvoie un `ResolvedCtx`, ou lève
`AuthzDenied` (neutre). Elles **réutilisent** la logique d'autz existante
(`access`, `org_store`) — source unique, pas de duplication.

Depuis le retrait du transport stdio (2026-06-13) le serveur est toujours
authentifié : plus de branche `sub is None` → accès complet. `sub` absent = refus.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from .. import access, org_store
from ._types import AuthzDenied, RawCtx, ResolvedCtx


def _require_sub(raw: RawCtx) -> str:
    if not raw.sub:
        raise AuthzDenied(401, "auth_required", "Authentification requise.")
    return raw.sub


def SUB_ONLY(raw: RawCtx, inp: Optional[BaseModel] = None) -> ResolvedCtx:
    """Tout user authentifié (datastore, méta user-tools, oto_use_org)."""
    sub = _require_sub(raw)
    return ResolvedCtx(sub=sub, org_id=org_store.get_active_org(sub),
                       role=access.get_user_role(sub))


def ORG_MEMBER(raw: RawCtx, inp: Optional[BaseModel] = None) -> ResolvedCtx:
    """Membre d'une org active — injecte `org_id` depuis l'état serveur (jamais
    d'un param client). Verrouille l'IDOR cross-org par construction."""
    sub = _require_sub(raw)
    org_id = org_store.get_active_org(sub)
    if org_id is None:
        raise AuthzDenied(400, "no_active_org",
                          "Aucune org active — choisis-en une avec oto_use_org.")
    return ResolvedCtx(sub=sub, org_id=org_id, role=access.get_user_role(sub))


def PLATFORM_ADMIN(raw: RawCtx, inp: Optional[BaseModel] = None) -> ResolvedCtx:
    """Admin plateforme uniquement."""
    sub = _require_sub(raw)
    role = access.get_user_role(sub)
    if role != access.ADMIN:
        raise AuthzDenied(403, "forbidden", "Réservé au platform admin.")
    return ResolvedCtx(sub=sub, org_id=org_store.get_active_org(sub), role=role)


def NAMESPACE_GRANT(namespace: str):
    """Grant per-user OU entitlement d'org sur un namespace gouverné (escalade
    platform_admin incluse). Renvoie une règle paramétrée par `namespace`."""
    def rule(raw: RawCtx, inp: Optional[BaseModel] = None) -> ResolvedCtx:
        sub = _require_sub(raw)
        role = access.get_user_role(sub)
        if role != access.ADMIN and namespace not in access.granted_namespaces_for(sub):
            raise AuthzDenied(403, "namespace_not_granted",
                              f"Accès au namespace `{namespace}` non accordé.")
        return ResolvedCtx(sub=sub, org_id=org_store.get_active_org(sub), role=role)
    return rule


def ORG_MEMBER_OF(field: str):
    """Membre de l'org désignée par `input.<field>` (lecture d'une org par id de
    path, ≠ org active) — OU platform_admin (escalade). Ajoutée au barreau 2d
    (ADR 0009) pour les lectures par id. Miroir lecture d'`ORG_ADMIN_OF`."""
    def rule(raw: RawCtx, inp: Optional[BaseModel] = None) -> ResolvedCtx:
        sub = _require_sub(raw)
        role = access.get_user_role(sub)
        org_id = getattr(inp, field, None) if inp is not None else None
        if org_id is None:
            raise AuthzDenied(400, "missing_org", f"Champ `{field}` requis.")
        org_id = int(org_id)
        is_member = role == access.ADMIN or org_store.get_org_role(org_id, sub) is not None
        if not is_member:
            raise AuthzDenied(403, "forbidden", f"Réservé aux membres de l'org #{org_id}.")
        return ResolvedCtx(sub=sub, org_id=org_id, role=role)
    return rule


def ORG_ADMIN_OF(field: str):
    """Org-admin de l'org désignée par `input.<field>` — OU platform_admin
    (escalade conservée, cf. api_routes_orgs._is_org_admin). Porte la garde
    « dernier admin » au niveau handler/store."""
    def rule(raw: RawCtx, inp: Optional[BaseModel] = None) -> ResolvedCtx:
        sub = _require_sub(raw)
        role = access.get_user_role(sub)
        org_id = getattr(inp, field, None) if inp is not None else None
        if org_id is None:
            raise AuthzDenied(400, "missing_org", f"Champ `{field}` requis.")
        org_id = int(org_id)
        is_admin = role == access.ADMIN or org_store.get_org_role(org_id, sub) == "org_admin"
        if not is_admin:
            raise AuthzDenied(403, "forbidden", f"Réservé à un org_admin de l'org #{org_id}.")
        return ResolvedCtx(sub=sub, org_id=org_id, role=role)
    return rule
