"""L'org que l'INSCRIPTION pose (`orgs.auto_created`), et le critère « encore seule ».

Plus d'org perso (05/11/2026, `org_store/personal.py`) : qui s'inscrit sans invitation
reçoit une org ORDINAIRE, marquée `auto_created` — l'inscription l'a posée, pas lui.
La marque est permanente : une org d'entreprise née d'une inscription la garde quand
ses collègues la rejoignent. Elle ne suffit donc pas à dire « l'équivalent de l'ex-org
perso » : il faut aussi que son seul membre soit le compte considéré. C'est le critère
`seule_d_inscription`, écrit une fois, ici, et lu par :

- l'adhésion (`org_store.members.add_org_member_in`) : une org d'inscription SEULE cède
  la place d'org active à l'org qu'on rejoint ; une org d'inscription PEUPLÉE est une
  vraie org d'équipe, elle ne la cède pas ;
- le relâchement à l'acceptation d'une invitation (`org_store.personal.liberer_perso_vierge`) ;
- le périmètre d'un export (`export_perimetre.perimetre.resoudre`), qui y range l'org
  d'inscription seule de chaque compte, comme il rangeait son org perso.

La colonne naît dans `CREATE TABLE orgs` (`db/schema/orgs.py`) ; une base existante la
reçoit de la révision `20261105_1700_orgs_auto_created` ou du démarrage, même DDL
(`DDL_COLONNE`), ordre indifférent.
"""
from __future__ import annotations

COLONNE = "auto_created"
DDL_COLONNE = (f"ALTER TABLE orgs ADD COLUMN IF NOT EXISTS {COLONNE} "
               "BOOLEAN NOT NULL DEFAULT FALSE")

#: Les orgs perso d'avant gardent leur rôle d'org posée par l'inscription (céder la
#: place d'active, rester hors quota) : la révision les marque. Idempotent.
MARQUER_LES_ORGS_PERSO = (f"UPDATE orgs SET {COLONNE} = TRUE "
                          f"WHERE personal_of IS NOT NULL AND NOT {COLONNE}")


def seule_d_inscription(org: str, sub: str) -> str:
    """Prédicat SQL : l'org d'alias `org` est née d'une inscription ET n'a aucun membre
    autre que `sub` (une expression SQL : paramètre ou colonne). Borné : `NOT EXISTS`
    s'arrête au premier autre membre, sur l'index `(org_id, sub)` de `org_members`."""
    return (f"({org}.{COLONNE} AND NOT EXISTS (SELECT 1 FROM org_members autre "
            f"WHERE autre.org_id = {org}.id AND autre.sub <> {sub}))")
