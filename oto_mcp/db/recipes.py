"""Le stockage des recettes : identité, versions, décision.

Ici le SQL et rien d'autre : qui a le droit de lire, proposer ou publier se juge dans
`capabilities/recipes.py`, et ce qu'une version contient se valide dans
`recipes/contrat.py`. Même découpe que `db/functions.py`, dont ce module reprend la
forme.

⚠️ **Une version est IMMUABLE.** Aucune fonction ici ne réécrit le corps d'une version :
corriger, c'est en proposer une nouvelle. Seuls changent son statut et, côté recette,
le pointeur vers la version publiée.
"""
from __future__ import annotations

import json
from typing import Iterable, Optional

import psycopg

from ._conn import _connect

# Ce que la liste et l'historique rendent d'une version : tout SAUF le corps, qui ne se
# lit qu'en demandant CETTE version.
_VERSION_COLS = ("v.version, v.status, v.note, v.proposed_by, v.proposed_at, "
                 "v.decided_by, v.decided_at, v.test_report")
_RECIPE_COLS = ("r.id, r.owner_type, r.owner_id, r.slug, r.title, r.description, "
                "r.published_version, r.created_by, r.created_at, r.updated_at")
_LATEST = ("(SELECT MAX(version) FROM recipe_versions v WHERE v.recipe_id = r.id) "
           "AS latest_version")


class RecipeExists(Exception):
    """Ce propriétaire a déjà une recette de ce slug."""


class VersionConflict(Exception):
    """La dernière version n'est pas celle que l'appelant a lue."""

    def __init__(self, attendue: int, courante: int):
        super().__init__(f"dernière version {courante}, l'appelant a lu la {attendue}")
        self.attendue, self.courante = attendue, courante


def create_recipe(*, owner_type: str, owner_id: str, slug: str, title: str,
                  description: str, created_by: str, body: dict,
                  note: Optional[str]) -> dict:
    """Crée la recette ET sa version 1, proposée, dans une seule transaction."""
    with _connect() as conn:
        with conn.transaction():
            try:
                rec = conn.execute(
                    "INSERT INTO recipes (owner_type, owner_id, slug, title, description, "
                    "created_by) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                    (owner_type, str(owner_id), slug, title, description or "", created_by),
                ).fetchone()
            except psycopg.errors.UniqueViolation as e:
                raise RecipeExists(slug) from e
            _insert_version(conn, rec["id"], 1, body=body, note=note, proposed_by=created_by)
    return get_recipe(owner_type, owner_id, slug)


def _insert_version(conn, recipe_id: int, version: int, *, body: dict,
                    note: Optional[str], proposed_by: str) -> None:
    conn.execute(
        "INSERT INTO recipe_versions (recipe_id, version, body, note, proposed_by) "
        "VALUES (%s, %s, %s::jsonb, %s, %s)",
        (recipe_id, version, json.dumps(body), note, proposed_by))
    conn.execute("UPDATE recipes SET updated_at = NOW() WHERE id = %s", (recipe_id,))


def propose_version(recipe_id: int, *, expected_version: int, body: dict,
                    note: Optional[str], proposed_by: str) -> int:
    """Ajoute la version suivante, PROPOSÉE. Rend son numéro.

    `expected_version` est la dernière version que l'appelant a lue : si une autre est
    passée entre-temps, on refuse plutôt que d'empiler deux versions écrites sans se
    voir. La ligne de la recette est verrouillée le temps du calcul du numéro."""
    with _connect() as conn:
        with conn.transaction():
            conn.execute("SELECT id FROM recipes WHERE id = %s FOR UPDATE", (recipe_id,))
            derniere = conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS v FROM recipe_versions "
                "WHERE recipe_id = %s", (recipe_id,)).fetchone()["v"]
            if derniere != expected_version:
                raise VersionConflict(expected_version, derniere)
            _insert_version(conn, recipe_id, derniere + 1, body=body, note=note,
                            proposed_by=proposed_by)
    return derniere + 1


def get_recipe(owner_type: str, owner_id: str, slug: str) -> Optional[dict]:
    """La fiche d'une recette et le numéro de sa dernière version, ou `None`."""
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {_RECIPE_COLS}, {_LATEST} FROM recipes r "
            "WHERE r.owner_type = %s AND r.owner_id = %s AND r.slug = %s",
            (owner_type, str(owner_id), slug)).fetchone()
    return dict(row) if row else None


def list_recipes(owners: Iterable[tuple[str, str]]) -> list[dict]:
    """Les recettes de ces propriétaires, par slug. Une requête, sans les corps."""
    owners = list(owners)
    if not owners:
        return []
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {_RECIPE_COLS}, {_LATEST} FROM recipes r "
            "WHERE (r.owner_type, r.owner_id) IN "
            f"({','.join(['(%s, %s)'] * len(owners))}) ORDER BY r.slug",
            [v for pair in owners for v in pair]).fetchall()
    return [dict(r) for r in rows]


def get_version(recipe_id: int, version: int) -> Optional[dict]:
    """UNE version, corps compris, ou `None`."""
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {_VERSION_COLS}, v.body FROM recipe_versions v "
            "WHERE v.recipe_id = %s AND v.version = %s", (recipe_id, version)).fetchone()
    return dict(row) if row else None


def list_versions(recipe_id: int) -> list[dict]:
    """L'historique, de la plus récente à la plus ancienne, sans les corps."""
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {_VERSION_COLS} FROM recipe_versions v WHERE v.recipe_id = %s "
            "ORDER BY v.version DESC", (recipe_id,)).fetchall()
    return [dict(r) for r in rows]


def decide_version(recipe_id: int, version: int, *, status: str, decided_by: str,
                   test_report: Optional[dict]) -> None:
    """Publie ou refuse UNE version. Publier pointe la recette sur elle — c'est aussi
    le retour arrière : republier une version antérieure la remet en service."""
    if status not in ("publiee", "refusee"):
        raise ValueError(f"statut de décision inconnu : {status}")
    with _connect() as conn:
        with conn.transaction():
            conn.execute(
                "UPDATE recipe_versions SET status = %s, decided_by = %s, "
                "decided_at = NOW(), test_report = %s::jsonb "
                "WHERE recipe_id = %s AND version = %s",
                (status, decided_by, json.dumps(test_report) if test_report else None,
                 recipe_id, version))
            if status == "publiee":
                conn.execute("UPDATE recipes SET published_version = %s, "
                             "updated_at = NOW() WHERE id = %s", (version, recipe_id))
