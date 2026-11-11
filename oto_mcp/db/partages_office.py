"""Les partages d'org posés d'office sur les agents, et leur passage en lecture.

La révision `0035_agents_partages_a_l_org` a posé, sur chaque agent existant au
02/10/2026, un partage `editor` à son org entière, marqué
(`granted_by = runner_triggers.MARQUE_MIGRATION_0035`). Aucune personne ne l'a posé :
il ouvrait l'écriture d'un agent à tous les membres de son org. La révision
`20261111_2000_office_en_lecture` le rabat en lecture (`viewer`/`read`) : les
membres gardent la vue, perdent l'écriture ; le propriétaire et les admins de l'org ne
perdent rien (leur droit ne passe pas par ce partage).

Un seul choix de lignes (`_EDITEUR_D_OFFICE`) sert la révision et le compte à blanc
(`oto-mcp maintenance partages-office --dry-run`) : ce que le compte annonce est ce que
la révision rabat. Un partage reposé par une personne a perdu la marque (l'upsert
réécrit `granted_by`) et n'est jamais choisi.

Le partage rabattu reçoit sa propre marque (`MARQUE_RABATTU_EN_LECTURE`) : le retour
arrière remet en `editor`, sous la marque 0035, exactement ces lignes-là — un partage
reposé ou retiré depuis par une personne n'en fait plus partie.
"""
from __future__ import annotations

from ._conn import _connect
from .runner_triggers import MARQUE_MIGRATION_0035

REVISION = "20261111_2000_office_en_lecture"
MARQUE_RABATTU_EN_LECTURE = f"migration:{REVISION}"


def _partage_d_org_d_agent(marque: str, role: str) -> str:
    return ("resource_type = 'runner_trigger' AND principal_type = 'org' "
            f"AND granted_by = '{marque}' AND role = '{role}'")


_EDITEUR_D_OFFICE = _partage_d_org_d_agent(MARQUE_MIGRATION_0035, "editor")
_RABATTU = _partage_d_org_d_agent(MARQUE_RABATTU_EN_LECTURE, "viewer")

# Le compte par org d'un ensemble de lignes `principal_id` (l'org, en texte) : la forme
# commune du compte à blanc et du journal de la révision.
_PAR_ORG = "SELECT principal_id::bigint AS org_id, count(*) AS n FROM {source} " \
           "GROUP BY 1 ORDER BY 1"

#: Lecture seule : les partages d'office encore en écriture, par org.
COMPTE_PAR_ORG = _PAR_ORG.format(
    source=f"resource_grants WHERE {_EDITEUR_D_OFFICE}")

#: La révision : rabat en lecture, sous sa marque, et rend le compte par org.
RABATTRE = (
    "WITH r AS (UPDATE resource_grants "
    f"SET role = 'viewer', permission = 'read', granted_by = '{MARQUE_RABATTU_EN_LECTURE}' "
    f"WHERE {_EDITEUR_D_OFFICE} RETURNING principal_id) "
    + _PAR_ORG.format(source="r"))

#: Le retour arrière : remet en écriture, sous la marque 0035, ce que la révision a
#: rabattu et que personne n'a repris depuis.
RETABLIR = (
    "WITH r AS (UPDATE resource_grants "
    f"SET role = 'editor', permission = 'write', granted_by = '{MARQUE_MIGRATION_0035}' "
    f"WHERE {_RABATTU} RETURNING principal_id) "
    + _PAR_ORG.format(source="r"))


def par_org(lignes) -> dict[int, int]:
    """`{org_id: nombre}` depuis les lignes de `COMPTE_PAR_ORG`, `RABATTRE` ou
    `RETABLIR`."""
    return {int(r["org_id"]): int(r["n"]) for r in lignes}


def compter_a_blanc() -> dict[int, int]:
    """Le compte par org de `COMPTE_PAR_ORG`, dans une transaction `READ ONLY` bornée à
    15 s : rien ne s'écrit, une lecture qui traîne s'arrête."""
    with _connect() as conn:
        with conn.transaction():
            conn.execute("SET TRANSACTION READ ONLY")
            conn.execute("SET LOCAL statement_timeout = '15s'")
            return par_org(conn.execute(COMPTE_PAR_ORG).fetchall())


def lignes_du_compte(compte: dict[int, int]) -> list[str]:
    """Ce que le journal et le compte à blanc impriment : des identifiants et des
    nombres, jamais un nom ni un contenu."""
    lignes = [f"org_id={org} n={n}" for org, n in sorted(compte.items())]
    lignes.append(f"total={sum(compte.values())} orgs={len(compte)}")
    return lignes
