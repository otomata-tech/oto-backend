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
secrets (`access`) et le guide (`tools/orgs`) partagent la MÊME logique —
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


# Ordre d'AUTORITÉ **par palier**, du plus faible au plus fort. La hiérarchie n'était
# jusqu'ici lisible que dans la prose (docstring du module) et dans des comparaisons
# `== ORG_ADMIN` disséminées : un appelant qui doit **comparer** deux rôles (« garder le
# plus fort des deux », cf. `max_org_role`) n'avait rien à dériver et recopiait l'ordre
# chez lui — un rang recopié diverge au premier rôle ajouté ici.
# Deux tuples et non un seul : comparer un rôle d'org à un rôle d'équipe n'a pas de sens
# (l'escalade ENTRE paliers passe par `effective_group_role`, jamais par un rang).
ORG_ROLE_ORDER: tuple[str, ...] = (ORG_MEMBER, ORG_ADMIN)
GROUP_ROLE_ORDER: tuple[str, ...] = (GROUP_MEMBER, GROUP_ADMIN)


def _stronger(current: Optional[str], requested: Optional[str],
              order: tuple[str, ...]) -> Optional[str]:
    if current is None:
        return requested
    if requested is None:
        return current
    try:
        return current if order.index(current) > order.index(requested) else requested
    except ValueError:
        # Rôle hors hiérarchie : aucun rang à comparer, on ne devine pas. Le rôle
        # DEMANDÉ passe et c'est le store (enum `ORG_ROLES`/`GROUP_ROLES`) qui le
        # refuse s'il est invalide — le « garder » ici masquerait l'écriture illégale.
        return requested


def max_org_role(current: Optional[str], requested: Optional[str]) -> Optional[str]:
    """Le plus fort des deux rôles d'ORG **stockés** (None = aucune appartenance).

    Pour les écritures qui sont des **ajouts** et non des administrations de rôle :
    l'acceptation d'une invitation (`org_store.accept_invitation`) écrit par upsert et
    rétrogradait donc un org_admin invité en org_member (#297).
    ⚠️ Compare des rôles **de table**, jamais des rôles effectifs : l'escalade
    platform_admin/org_admin n'est pas une appartenance et n'a rien à faire dans une
    ligne `org_members` (`effective_org_role` la rendrait pourtant)."""
    return _stronger(current, requested, ORG_ROLE_ORDER)


def max_group_role(current: Optional[str], requested: Optional[str]) -> Optional[str]:
    """Le plus fort des deux rôles d'ÉQUIPE **stockés** — pendant de `max_org_role`
    au palier équipe (`org_group_members`). Mêmes réserves : rôles de table, pas
    d'escalade (`effective_group_role` rendrait `group_admin` pour l'org_admin
    parent, qui n'est membre d'aucune équipe)."""
    return _stronger(current, requested, GROUP_ROLE_ORDER)


def is_platform_admin(sub: str) -> bool:
    """platform_admin = **super_admin** : seul le tout-puissant escalade en masse
    (org_admin de toute org / group_admin de tout groupe). L'`admin` opérationnel
    n'est PAS platform_admin au sens de cette hiérarchie."""
    return access.is_super_admin(sub)


# --- palier org -------------------------------------------------------------

def effective_org_role(sub: str, org_id: int) -> Optional[str]:
    """Rôle EFFECTIF du sub dans l'org (escalade platform_admin incluse), ou None
    s'il n'a aucun droit dessus. `org_admin` > `org_member`.

    ⚠️ Sous un jeton de DÉLÉGATION, le porteur n'a aucun rôle hors de l'org de son
    travail — avant l'escalade plateforme (`verrou_org.py`)."""
    from . import verrou_org
    if verrou_org.hors(sub, org_id, route="org_role"):
        return None
    if is_platform_admin(sub):
        return ORG_ADMIN
    real = org_store.get_org_role(org_id, sub)  # 'org_admin' | 'org_member' | None
    if real is not None:
        return real
    # Opérateur plateforme CONSULTANT activement cette org (header X-Oto-Org REST posé) :
    # accès LECTEUR (org_member), jamais admin — c'est le seam unique lu par `is_org_member`
    # (autz `ORG_MEMBER`) ET `ownership.can_access` (contenu org). Borné au contexte de
    # consultation : hors consultation `current_view_org()` est None → aucun droit sur une
    # org tierce. Le middleware REST impose EN PLUS le GET-only (double garde read-only), et
    # le MCP ne pose jamais ce contextvar → aucun effet côté agent.
    from . import session_org
    if session_org.current_view_org() == org_id and access.is_platform_operator(sub):
        return ORG_MEMBER
    return None


def is_org_admin(sub: str, org_id: int) -> bool:
    return effective_org_role(sub, org_id) == ORG_ADMIN


def is_org_member(sub: str, org_id: int) -> bool:
    return effective_org_role(sub, org_id) is not None


# --- palier groupe (chef d'équipe / département) ----------------------------

def _equipe_hors_verrou(sub: str, g: Optional[dict]) -> bool:
    """L'équipe est-elle hors de l'org du travail du porteur (`verrou_org.py`) ? Le rôle
    d'équipe direct ne passe pas par `effective_org_role` : il se garde ici."""
    from . import verrou_org
    return g is not None and verrou_org.hors(sub, g.get("org_id"), route="group")


def can_admin_group(sub: str, group_id: int) -> bool:
    """Peut ADMINISTRER le groupe (membres, secrets, guide) ?

    Vrai pour le chef d'équipe (`group_admin` explicite) ET, par subsomption,
    pour l'org_admin du groupe parent et le platform_admin. Un org_admin n'a
    pas besoin d'être membre du groupe pour le gérer (il gère son org entière)."""
    g = group_store.get_group(group_id)
    if g is None:
        return False
    if _equipe_hors_verrou(sub, g):
        return False
    if is_org_admin(sub, g["org_id"]):
        return True
    return group_store.get_group_role(group_id, sub) == GROUP_ADMIN


def can_read_group(sub: str, group_id: int) -> bool:
    """Peut LIRE le groupe (détail, guide, liste des secrets sans valeur) ?

    Tout membre du groupe, plus quiconque peut l'administrer (org_admin/platform).
    Un simple membre de l'org NON membre du groupe ne le lit pas (les ressources
    de groupe sont scopées au groupe, comme les org_secrets le sont à l'org)."""
    if can_admin_group(sub, group_id):
        return True
    if _equipe_hors_verrou(sub, group_store.get_group(group_id)):
        return False
    return group_store.get_group_role(group_id, sub) is not None


def effective_group_role(sub: str, group_id: int) -> Optional[str]:
    """Rôle effectif dans le groupe (escalade incluse), ou None. Utilisé pour
    `/api/me` et l'UI (afficher les contrôles chef)."""
    if can_admin_group(sub, group_id):
        return GROUP_ADMIN
    if _equipe_hors_verrou(sub, group_store.get_group(group_id)):
        return None
    role = group_store.get_group_role(group_id, sub)
    return role if role is not None else None
