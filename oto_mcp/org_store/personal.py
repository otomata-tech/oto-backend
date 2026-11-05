"""L'org MAISON d'un compte, et le rattrapage de boot. **Plus aucune org perso.**

Décision du 05/11/2026, toutes instances : plus d'« espace personnel ». Plus
aucune org ne naît avec l'étiquette `orgs.personal_of`, et les règles qui
l'accompagnaient (non quittable, non archivable, maison des objets sans org) tombent.
L'invariant « tout compte a une org active » reste, lui, entier :

- qui s'inscrit SANS invitation reçoit une org ORDINAIRE (`ensure_home_org`), nommée
  d'après le domaine de son email pro, sinon d'après lui — sans étiquette, sans règle
  à part. Elle est marquée `orgs.auto_created` : l'inscription l'a posée, pas lui.
  Cette marque ne sert qu'à trois choses — céder la place d'org active à l'org qu'il
  rejoint ensuite TANT QU'IL Y EST SEUL (`db.org_d_inscription.seule_d_inscription`,
  lu par `members.add_org_member_in` : peuplée, c'est une org d'équipe), relâcher celle qui est restée vide
  quand il accepte une invitation (`liberer_perso_vierge`), et ne pas la compter dans
  son quota de créations (`orgs.count_orgs_created_by`) ;
- la MAISON d'un compte (là où se rangent un partage nominatif reçu et un objet
  personnel sans org de création) est désormais son org ACTIVE (`home_org`).

`personal_of` reste en base (colonne + index, `db/_init.py`), sans écrivain ; elle est
vidée par `scripts/retirer_orgs_perso.py` (manuel), sa suppression du schéma est un lot
à part.

Étage 1 du package : consomme `orgs` (création) et `members` (adhésion, maison).
"""
from __future__ import annotations

import logging
from typing import Optional

from . import members
from . import orgs
from ..db import _connect

_log = logging.getLogger(__name__)


# Domaines d'email grand public : ils ne nomment pas une entreprise.
_DOMAINES_PUBLICS = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "outlook.fr", "hotmail.com",
    "hotmail.fr", "live.com", "live.fr", "msn.com", "yahoo.com", "yahoo.fr",
    "icloud.com", "me.com", "mac.com", "aol.com", "proton.me", "protonmail.com",
    "gmx.com", "gmx.fr", "orange.fr", "wanadoo.fr", "free.fr", "sfr.fr",
    "laposte.net", "bbox.fr", "yandex.com", "mail.com",
})


def home_org(sub: str) -> Optional[int]:
    """La MAISON de `sub` : son org active (vivante), ou None s'il n'en a aucune.
    Lecture seule — ce qui a besoin d'une maison qui existe appelle `ensure_home_org`."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT m.org_id FROM org_members m JOIN orgs o ON o.id = m.org_id "
            " WHERE m.sub = %s AND m.is_active AND o.archived_at IS NULL "
            "UNION ALL SELECT s.org_id FROM org_service_accounts s "
            "  JOIN orgs o ON o.id = s.org_id "
            " WHERE s.sub = %s AND o.archived_at IS NULL LIMIT 1",
            (sub, sub),
        ).fetchone()
        return int(row["org_id"]) if row else None


def is_personal_org(org_id: int) -> bool:
    """Toujours False : plus d'org perso (05/11/2026). Gardé pour la
    surface `personal` des fiches et listes (front, `oto_list_orgs`), qui reste
    servie à False le temps que ses lecteurs l'oublient."""
    return False


def is_auto_created(org_id: int) -> bool:
    """True si l'org a été posée par l'inscription d'un compte qui n'en avait aucune
    (`ensure_home_org`) — une org ordinaire, la marque ne lui donne aucune règle à part."""
    with _connect() as conn:
        row = conn.execute("SELECT auto_created FROM orgs WHERE id = %s", (org_id,)).fetchone()
        return bool(row and row["auto_created"])


def _home_label(email: Optional[str], name: Optional[str]) -> str:
    """Nom de l'org posée à l'inscription : le domaine d'un email PRO (« Acme » pour
    …@acme.io), sinon le nom de la personne, sinon le début de son email."""
    domaine = (email or "").rsplit("@", 1)[-1].strip().lower() if email and "@" in email else ""
    if domaine and domaine not in _DOMAINES_PUBLICS:
        racine = domaine.split(".")[0]
        if racine:
            return racine.capitalize()
    return (name or (email.split("@")[0] if email else None) or "Mon org").strip() or "Mon org"


def _create_home_org(sub: str, email: Optional[str], name: Optional[str]) -> int:
    oid = orgs.create_org(_home_label(email, name), created_by=sub)
    with _connect() as conn:
        conn.execute("UPDATE orgs SET auto_created = TRUE WHERE id = %s", (oid,))
    members.add_org_member(oid, sub, org_role="org_admin", actor=None)  # le système
    _log.info("ensure_home_org: org #%s créée pour %s (aucune autre org)", oid, sub)
    # Onboarding = un projet (ADR 0032 §7) : le projet « Découverte » est semé dans
    # l'org posée à l'inscription, une seule fois, ici. Best-effort.
    from .. import discovery
    discovery.seed_for_org(sub, oid)
    return oid


def ensure_home_org(sub: str, email: Optional[str] = None, name: Optional[str] = None) -> int:
    """Garantit que `sub` a une org active et la rend. Idempotent, jamais d'org perso :
    son org active si elle vit ; sinon la plus ancienne de ses orgs vivantes, qui
    redevient active ; sinon — et SEULEMENT alors — une org ordinaire neuve."""
    pid = home_org(sub)
    if pid is not None:
        return pid
    vivantes = members.list_orgs_for_user(sub)
    if vivantes:
        pid = int(vivantes[0]["org_id"])
    else:
        pid = _create_home_org(sub, email, name)
    members.set_active_org(sub, pid)
    return pid


# Les seuls comptes à rattraper : sans org active VIVANTE.
_A_RATTRAPER = """
    SELECT u.sub, u.email, u.name FROM users u
     WHERE NOT EXISTS (SELECT 1 FROM org_members m JOIN orgs o ON o.id = m.org_id
                        WHERE m.sub = u.sub AND m.is_active AND o.archived_at IS NULL)
    -- Le compte de service d'une org (`members.assurer_compte_de_service`) n'a ni
    -- ligne de membre ni org à lui, et ne doit pas en recevoir.
    EXCEPT SELECT u.sub, u.email, u.name FROM users u
      JOIN org_service_accounts s ON s.sub = u.sub
"""


def backfill_personal_orgs() -> dict:
    """Idempotent (boot) : chaque compte a une org active (`ensure_home_org`). Membre
    d'orgs vivantes dont aucune n'est active → la plus ancienne redevient active ;
    membre d'aucune → une org ordinaire neuve. Rend `{"users": <comptes rattrapés>}`.
    (Le nom est resté pour `server.py` ; il ne crée plus d'org perso.)

    ⚠️ Ne TOUCHE PAS aux ressources (cf. ADR 0030 §8 : `owner_type='user'` est le
    scope membre, pas un vestige à rattraper)."""
    counts = {"users": 0}
    with _connect() as conn:
        users = conn.execute(_A_RATTRAPER).fetchall()
    for u in users:
        try:
            ensure_home_org(u["sub"], u.get("email"), u.get("name"))
        except Exception:
            _log.warning("backfill_personal_orgs: ensure échoué %s", u["sub"], exc_info=True)
            continue
        counts["users"] += 1
    return counts


# L'org qu'un invité PAR LIEN reçoit avant d'accepter (`upsert_user` tourne au premier
# appel authentifié, avant que le front ne présente le jeton) : posée par l'inscription
# (`auto_created`), née il y a moins d'une heure, seul membre, et rien dedans hormis le
# projet « Découverte » semé à sa création — ni tableau, ni clé déposée (coffre de l'org
# ou du membre), ni compte connecté (même déconnecté : la ligne reste la preuve de
# propriété), ni partage nominatif reçu. Au-delà, ce n'est plus un artefact
# d'inscription : on n'y touche pas.
def _requete_perso_vierge() -> str:
    """La requête du critère ci-dessus. Dans une fonction, pas au niveau du module :
    la garde des partages échus (`tests/test_partages_echeance_39.py`) attribue chaque
    lecture de `resource_grants` à la fonction qui la porte."""
    from ..db._partage_vivant import PARTAGE_VIVANT_G
    from ..db.org_d_inscription import seule_d_inscription
    return f"""
    SELECT o.id FROM orgs o
     WHERE o.created_by = %(sub)s AND {seule_d_inscription('o', '%(sub)s')}
       AND o.archived_at IS NULL AND o.id <> %(rejointe)s
       AND o.created_at > now() - interval '1 hour'
       AND NOT EXISTS (SELECT 1 FROM projects p
                        WHERE ((p.owner_type = 'org' AND p.owner_id = o.id::text)
                               OR p.context_org_id = o.id)
                          AND p.name <> %(decouverte)s)
       AND NOT EXISTS (SELECT 1 FROM user_datastores d
                        WHERE (d.owner_type = 'org' AND d.owner_id = o.id::text)
                           OR d.context_org_id = o.id)
       AND NOT EXISTS (SELECT 1 FROM connector_credentials c
                        WHERE (c.entity_type = 'org' AND c.entity_id = o.id::text)
                           OR (c.entity_type = 'member'
                               AND c.entity_id LIKE o.id::text || ':%%'))
       AND NOT EXISTS (SELECT 1 FROM unipile_accounts a WHERE a.org_id = o.id)
       AND NOT EXISTS (SELECT 1 FROM resource_grants g
                        WHERE g.principal_type = 'user' AND g.principal_id = %(sub)s
                          -- même exception que `ownership._PARTAGES_LISTES_DANS_L_ORG`
                          AND g.resource_type <> 'runner_trigger'
                          AND {PARTAGE_VIVANT_G})
"""


def liberer_perso_vierge(sub: str, org_rejointe: int) -> Optional[int]:
    """Archive l'org VIERGE que l'inscription de `sub` lui a posée, une fois qu'il a
    rejoint `org_rejointe` (acceptation d'invitation). Rend l'id archivé, ou None si
    rien à relâcher. Sa maison ne bouge pas : `add_org_member` a déjà promu l'org
    rejointe, et `archive_org` rebascule de toute façon qui l'avait pour active."""
    from .. import discovery
    with _connect() as conn:
        row = conn.execute(_requete_perso_vierge(), {"sub": sub, "rejointe": int(org_rejointe),
                                           "decouverte": discovery.PROJECT_NAME}).fetchone()
    if not row:
        return None
    oid = int(row["id"])
    if not orgs.archive_org(oid):
        return None
    _log.info("liberer_perso_vierge: org vierge #%s de %s archivée (a rejoint #%s)",
              oid, sub, org_rejointe)
    return oid
