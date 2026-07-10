"""Garde-fou de l'inventaire `_SUB_COLUMNS` (migrate_sub, bascule de tenant #35/#56).

La boucle de `migrate_sub` fait des UPDATE nus dans UNE transaction : une entrée
pointant une table/colonne ABSENTE fait échouer tout le merge — silencieusement,
puisque rien ne l'exerce en CI. Vécu (Phase H B1, 10/07) : `user_grants` droppée
par 0044 §F mais restée listée → migrate_sub cassé pendant deux jours.

Ce test fige le contrat : chaque `(table, col)` de l'inventaire doit exister dans
le DDL — colonne déclarée dans le bloc CREATE TABLE de `_schema.py`, OU ajoutée
par un `ALTER TABLE <t> ADD COLUMN IF NOT EXISTS <col>` de `_init.py`. Dropper
une table/colonne sans retirer son entrée casse ce test au lieu de casser le
merge en prod.
"""
import pathlib
import re

from oto_mcp.db._schema import _SCHEMA
from oto_mcp.db.users import _SUB_COLUMNS

_INIT_SRC = (pathlib.Path(__file__).resolve().parent.parent
             / "oto_mcp" / "db" / "_init.py").read_text(encoding="utf-8")


def _create_blocks(schema_sql: str) -> dict[str, str]:
    """{table: corps du CREATE} — parse suffisant pour vérifier la présence d'un
    nom de colonne (les DDL du repo sont réguliers : un bloc par table)."""
    blocks = {}
    for m in re.finditer(
            r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\);", schema_sql, re.S):
        blocks[m.group(1)] = m.group(2)
    return blocks


def test_sub_columns_inventory_matches_ddl():
    blocks = _create_blocks(_SCHEMA)
    problems = []
    for table, col in _SUB_COLUMNS:
        body = blocks.get(table)
        if body is None:
            problems.append(f"{table}.{col} : table absente de _schema.py")
            continue
        in_create = re.search(rf"^\s*{col}\b", body, re.M) is not None
        in_alter = re.search(
            rf"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col}\b",
            _INIT_SRC) is not None
        if not (in_create or in_alter):
            problems.append(f"{table}.{col} : colonne introuvable (CREATE et ALTER)")
    assert not problems, (
        "entrées _SUB_COLUMNS mortes (migrate_sub échouerait en prod) :\n  "
        + "\n  ".join(problems)
        + "\nRetirer l'entrée de l'inventaire (ou restaurer la colonne)."
    )
