"""Les SUGGESTIONS d'un commentaire : un passage, et le texte proposé à sa place
(`docs/commentaires-de-procedure.md` §Suggestions).

Une suggestion est un commentaire RACINE qui cite un passage (`quote`) et porte
`suggestion`, le texte qui le remplacerait (`""` = retirer le passage, distinct de
`NULL` = un simple commentaire). Qui peut écrire la procédure ou la page l'ACCEPTE ou la
REFUSE : `resolution` (`accepted` | `rejected`), `resolved_by`, `resolved_at`.

**Rien n'est appliqué ici.** Le passage est une ancre du texte AFFICHÉ, pas du markdown
stocké : c'est l'éditeur de l'application qui remplace le passage et enregistre une
nouvelle version (le chemin d'écriture ordinaire, avec ses propres droits), PUIS marque
la suggestion acceptée. Une suggestion en attente ne bloque, n'exige et ne vérifie rien :
aucune écriture de procédure ou de page ne la lit.

Les colonnes sont posées sur `procedure_comments` et `doc_comments` par la révision
`20261126_1200_suggestions` ET par le démarrage, sous garde de catalogue —
même DDL, ordre indifférent.
"""
from __future__ import annotations

from typing import Optional

from ._conn import _connect

COLONNES = ("suggestion", "resolution", "resolved_by", "resolved_at")
RESOLUTIONS = ("accepted", "rejected")
TABLES = ("procedure_comments", "doc_comments")


def ddl(table: str) -> str:
    """Les quatre colonnes, nullables, sans défaut : catalogue seul, aucune réécriture."""
    assert table in TABLES
    return (f"ALTER TABLE {table} "
            "ADD COLUMN IF NOT EXISTS suggestion TEXT, "
            "ADD COLUMN IF NOT EXISTS resolution TEXT, "
            "ADD COLUMN IF NOT EXISTS resolved_by TEXT, "
            "ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ")


DDL_PROCEDURES = ddl("procedure_comments")
DDL_PAGES = ddl("doc_comments")


def resoudre(table: str, cle: str, parent: int, comment_id: int, *, resolution: str,
             sub: Optional[str], colonnes: str) -> Optional[dict]:
    """Accepte ou refuse la suggestion `comment_id` de `parent` (`cle` = la colonne qui
    le désigne). Rend la ligne, ou `None` si ce n'est pas une suggestion vivante EN
    ATTENTE : une suggestion se résout une fois."""
    assert table in TABLES and resolution in RESOLUTIONS
    with _connect() as conn:
        r = conn.execute(
            f"UPDATE {table} SET resolution = %s, resolved_by = %s, resolved_at = now() "
            f"WHERE id = %s AND {cle} = %s AND deleted_at IS NULL AND parent_id IS NULL "
            f"AND suggestion IS NOT NULL AND resolution IS NULL RETURNING {colonnes}",
            (resolution, sub, int(comment_id), int(parent))).fetchone()
    return dict(r) if r else None
