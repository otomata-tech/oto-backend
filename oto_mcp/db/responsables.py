"""Le RESPONSABLE d'une procédure ou d'une page : la personne à qui s'adresser.

Une équipe qui partage ses procédures et sa mémoire veut savoir à QUI poser une
question sur l'une d'elles, sans demander à un agent qui l'a écrite. `owner_sub` nomme
ce compte. C'est un NOM, pas un droit : il n'ouvre ni ne ferme rien — quiconque pouvait
écrire la procédure ou la page le peut toujours, et peut la confier à quelqu'un d'autre.

⚠️ À ne pas confondre avec le PALIER propriétaire d'une procédure (`owner_type` /
`owner_id` : l'org, l'équipe ou la personne qui la POSSÈDE, ADR 0030) : `owner_sub` est
une personne de ce palier, rien de plus.

Tant que personne ne l'a posé (`NULL`), le responsable LU est l'auteur : `created_by`
pour une page, l'auteur de la PREMIÈRE version pour une procédure. Rien n'est écrit
pour le déduire : une ligne d'avant n'a pas à être migrée, et changer l'auteur d'origine
n'arrive jamais.

Les deux colonnes sont posées par la révision `20261120_1108_responsables` ET par le
démarrage, sous garde de catalogue — même DDL.
"""
from __future__ import annotations

from typing import Optional

from ._conn import _connect

COLONNE = "owner_sub"
# Nullables, sans défaut : catalogue seul, aucune réécriture.
DDL_COLONNE_PAGES = f"ALTER TABLE docs ADD COLUMN IF NOT EXISTS {COLONNE} TEXT"
DDL_COLONNE_PROCEDURES = f"ALTER TABLE org_instructions ADD COLUMN IF NOT EXISTS {COLONNE} TEXT"


def poser_responsable_de_page(doc_id: int, sub: str) -> bool:
    """Confie la page à `sub`. Ne touche ni le corps, ni `updated_at`, ni les versions :
    nommer quelqu'un n'est pas modifier la page. False si la page n'existe plus."""
    with _connect() as conn:
        cur = conn.execute(f"UPDATE docs SET {COLONNE} = %s WHERE id = %s", (sub, doc_id))
        return (cur.rowcount or 0) > 0


def responsable_de_procedure(owner_type: str, owner_id: str, slug: str) -> Optional[str]:
    """Le responsable LU d'une procédure : celui qu'on a posé, sinon l'auteur de sa
    première version gardée, sinon l'auteur de la ligne vivante (une procédure jamais
    réécrite n'a qu'elle). None si personne n'est connu."""
    with _connect() as conn:
        row = conn.execute(
            f"SELECT COALESCE(i.{COLONNE}, "
            "  (SELECT r.set_by FROM org_instruction_revisions r "
            "    WHERE r.owner_type = i.owner_type AND r.owner_id = i.owner_id "
            "      AND r.slug = i.slug AND r.set_by IS NOT NULL "
            "    ORDER BY r.version ASC LIMIT 1), "
            "  i.set_by) AS sub "
            "FROM org_instructions i "
            "WHERE i.owner_type = %s AND i.owner_id = %s AND i.slug = %s",
            (owner_type, str(owner_id), slug)).fetchone()
    return row["sub"] if row else None


def poser_responsable_de_procedure(owner_type: str, owner_id: str, slug: str,
                                   sub: str) -> bool:
    """Confie la procédure à `sub`. Ne monte PAS la version et n'archive aucun
    instantané : le corps et la vitrine sont intacts, et un agent qui a lu la version N
    la retrouve telle quelle. False si la procédure n'existe pas."""
    with _connect() as conn:
        cur = conn.execute(
            f"UPDATE org_instructions SET {COLONNE} = %s "
            "WHERE owner_type = %s AND owner_id = %s AND slug = %s",
            (sub, owner_type, str(owner_id), slug))
        return (cur.rowcount or 0) > 0


def est_membre_de_l_org(org_id: int, sub: str) -> bool:
    """`sub` est-il membre de l'org (une ligne `org_members`, son org active ou non) ?
    Un compte de service n'en est pas un : on ne confie pas une procédure à un robot."""
    with _connect() as conn:
        return conn.execute(
            "SELECT 1 FROM org_members WHERE org_id = %s AND sub = %s",
            (int(org_id), sub)).fetchone() is not None
