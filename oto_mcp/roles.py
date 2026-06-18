"""Résolution centrale des droits hiérarchiques (ADR 0012).

**Source unique** de la hiérarchie de rôles unifiée :

    platform_admin  >  org_admin  >  group_admin (chef d'équipe)  >  member

Un rôle supérieur **subsume** les inférieurs (escalade descendante) :
- `platform_admin` (= **super_admin**, `users.role='super_admin'`) agit comme
  org_admin de TOUTE org et group_admin de TOUT groupe. ⚠️ l'`admin`
  opérationnel (palier intermédiaire) ne subsume PAS les orgs : seul le
  super_admin escalade en masse (cf. `access.is_super_admin`).
- `org_admin` d'une org agit comme group_admin de TOUS les groupes de cette org.

Avant ADR 0012, l'escalade était recopiée à la main dans chaque combinateur
d'autz (`role == access.ADMIN or org_store.get_org_role(...) == 'org_admin'`).
Ce module la centralise pour que les combinateurs (`_authz`), la résolution de
secrets (`access`) et la doctrine (`tools/orgs`) partagent la MÊME logique —
ajouter un palier (le groupe) en un seul endroit, pas dans dix.

Sens unique (ADR 0004) : lit `access`/`org_store`/`group_store`, jamais l'inverse.
"""
from __future__ import annotations

from typing import Optional

from . import access, group_store, org_store

# Niveaux de la hiérarchie, du plus fort au plus faible (ordre = autorité).
PLATFORM_ADMIN = "platform_admin"
ORG_ADMIN = "org_admin"
ORG_MEMBER = "org_member"
GROUP_ADMIN = "group_admin"
GROUP_MEMBER = "group_member"


def is_platform_admin(sub: str) -> bool:
    """platform_admin = **super_admin** : seul le tout-puissant escalade en masse
    (org_admin de toute org / group_admin de tout groupe). L'`admin` opérationnel
    n'est PAS platform_admin au sens de cette hiérarchie."""
    return access.is_super_admin(sub)


# --- palier org -------------------------------------------------------------

def effective_org_role(sub: str, org_id: int) -> Optional[str]:
    """Rôle EFFECTIF du sub dans l'org (escalade platform_admin incluse), ou None
    s'il n'a aucun droit dessus. `org_admin` > `org_member`."""
    if is_platform_admin(sub):
        return ORG_ADMIN
    return org_store.get_org_role(org_id, sub)  # 'org_admin' | 'org_member' | None


def is_org_admin(sub: str, org_id: int) -> bool:
    return effective_org_role(sub, org_id) == ORG_ADMIN


def is_org_member(sub: str, org_id: int) -> bool:
    return effective_org_role(sub, org_id) is not None


# --- palier groupe (chef d'équipe / département) ----------------------------

def can_admin_group(sub: str, group_id: int) -> bool:
    """Peut ADMINISTRER le groupe (membres, secrets, doctrine, preset) ?

    Vrai pour le chef d'équipe (`group_admin` explicite) ET, par subsomption,
    pour l'org_admin du groupe parent et le platform_admin. Un org_admin n'a
    pas besoin d'être membre du groupe pour le gérer (il gère son org entière)."""
    g = group_store.get_group(group_id)
    if g is None:
        return False
    if is_org_admin(sub, g["org_id"]):
        return True
    return group_store.get_group_role(group_id, sub) == GROUP_ADMIN


def can_read_group(sub: str, group_id: int) -> bool:
    """Peut LIRE le groupe (détail, doctrine, liste des secrets sans valeur) ?

    Tout membre du groupe, plus quiconque peut l'administrer (org_admin/platform).
    Un simple membre de l'org NON membre du groupe ne le lit pas (les ressources
    de groupe sont scopées au groupe, comme les org_secrets le sont à l'org)."""
    if can_admin_group(sub, group_id):
        return True
    return group_store.get_group_role(group_id, sub) is not None


def effective_group_role(sub: str, group_id: int) -> Optional[str]:
    """Rôle effectif dans le groupe (escalade incluse), ou None. Utilisé pour
    `/api/me` et l'UI (afficher les contrôles chef)."""
    if can_admin_group(sub, group_id):
        return GROUP_ADMIN
    role = group_store.get_group_role(group_id, sub)
    return role if role is not None else None
