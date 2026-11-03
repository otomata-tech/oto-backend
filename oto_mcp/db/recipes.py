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
from typing import Any, Iterable, Optional

import psycopg

from ._conn import _connect

# Ce que la liste et l'historique rendent d'une version : tout SAUF le corps, qui ne se
# lit qu'en demandant CETTE version.
_VERSION_COLS = ("v.version, v.status, v.note, v.proposed_by, v.proposed_at, "
                 "v.decided_by, v.decided_at, v.test_report")
_RECIPE_COLS = ("r.id, r.owner_type, r.owner_id, r.slug, r.title, r.description, "
                "r.display, r.published_version, r.created_by, r.created_at, r.updated_at")
# Ce qu'une page montre d'une recette à l'endroit où un processus la lance (ses étapes,
# ce que chacune facture) : sur la FICHE, comme le titre et la description, pas dans le
# corps versionné — la changer ne republie rien. Posée par la révision
# `20261022_2100_recipes_display` ET par le démarrage, sous garde de catalogue — même DDL.
COLONNE_DISPLAY = "display"
DDL_COLONNE_DISPLAY = f"ALTER TABLE recipes ADD COLUMN IF NOT EXISTS {COLONNE_DISPLAY} JSONB"
_LATEST = ("(SELECT MAX(version) FROM recipe_versions v WHERE v.recipe_id = r.id) "
           "AS latest_version")


#: Le propriétaire des recettes de la PLATEFORME : lançables depuis toute org (une
#: procédure de la bibliothèque publique en nomme une), écrites par ses seuls opérateurs.
PLATEFORME = ("platform", "platform")

#: « Pas d'argument » pour `describe_recipe(display=…)`, distinct de None (= effacer).
_INCHANGE: Any = object()


class RecipeExists(Exception):
    """Ce propriétaire a déjà une recette de ce slug."""


class VersionConflict(Exception):
    """La dernière version n'est pas celle que l'appelant a lue."""

    def __init__(self, attendue: int, courante: int):
        super().__init__(f"dernière version {courante}, l'appelant a lu la {attendue}")
        self.attendue, self.courante = attendue, courante


def create_recipe(*, owner_type: str, owner_id: str, slug: str, title: str,
                  description: str, created_by: str, body: dict,
                  note: Optional[str], display: Optional[dict] = None) -> dict:
    """Crée la recette (affichage compris) ET sa version 1, proposée, dans une seule
    transaction."""
    with _connect() as conn:
        with conn.transaction():
            try:
                rec = conn.execute(
                    "INSERT INTO recipes (owner_type, owner_id, slug, title, description, "
                    "display, created_by) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s) "
                    "RETURNING id",
                    (owner_type, str(owner_id), slug, title, description or "",
                     json.dumps(display) if display is not None else None, created_by),
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


def describe_recipe(recipe_id: int, *, title: Optional[str] = None,
                    description: Optional[str] = None, display: Any = _INCHANGE) -> None:
    """Change ce que la fiche DIT d'une recette — titre, description, affichage — sans
    toucher à ses versions. `display=None` efface l'affichage ; omis, il reste."""
    sets, vals = [], []
    if title is not None:
        sets.append("title = %s")
        vals.append(title)
    if description is not None:
        sets.append("description = %s")
        vals.append(description)
    if display is not _INCHANGE:
        sets.append("display = %s::jsonb")
        vals.append(json.dumps(display) if display is not None else None)
    if not sets:
        return
    with _connect() as conn:
        conn.execute(f"UPDATE recipes SET {', '.join(sets)}, updated_at = NOW() "
                     "WHERE id = %s", (*vals, recipe_id))


def get_recipe(owner_type: str, owner_id: str, slug: str) -> Optional[dict]:
    """La fiche d'une recette et le numéro de sa dernière version, ou `None`."""
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {_RECIPE_COLS}, {_LATEST} FROM recipes r "
            "WHERE r.owner_type = %s AND r.owner_id = %s AND r.slug = %s",
            (owner_type, str(owner_id), slug)).fetchone()
    return dict(row) if row else None


def get_recipe_or_platform(owner_type: str, owner_id: str, slug: str) -> Optional[dict]:
    """La fiche de ce slug chez ce propriétaire, sinon celle de la PLATEFORME, ou `None`.
    Celle du propriétaire passe devant. Une requête.

    ⚠️ Pour LIRE seulement (les outils qu'une procédure fera appeler, un avertissement),
    jamais pour choisir ce qui S'EXÉCUTE : une recette d'org ou de soi qui porte le slug
    d'une recette de la plateforme la masquerait, et qui lance ce slug sans `scope`
    exécuterait le code de son auteur. `oto_recipe` résout par
    `get_recipe_and_platform`, qui rend les deux et laisse refuser la collision."""
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {_RECIPE_COLS}, {_LATEST} FROM recipes r WHERE r.slug = %s AND "
            "((r.owner_type = %s AND r.owner_id = %s) "
            "OR (r.owner_type = %s AND r.owner_id = %s)) "
            "ORDER BY (r.owner_type = %s) LIMIT 1",
            (slug, owner_type, str(owner_id), *PLATEFORME, PLATEFORME[0])).fetchone()
    return dict(row) if row else None


def get_recipe_and_platform(owner_type: str, owner_id: str,
                            slug: str) -> tuple[Optional[dict], Optional[dict]]:
    """`(fiche de ce propriétaire, fiche de la plateforme)` pour ce slug, chacune ou
    `None`. Une requête. Les deux rendues : à l'appelant de refuser une collision plutôt
    que de laisser l'une masquer l'autre."""
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {_RECIPE_COLS}, {_LATEST} FROM recipes r WHERE r.slug = %s AND "
            "((r.owner_type = %s AND r.owner_id = %s) "
            "OR (r.owner_type = %s AND r.owner_id = %s))",
            (slug, owner_type, str(owner_id), *PLATEFORME)).fetchall()
    fiches = [dict(r) for r in rows]
    plateforme = next((f for f in fiches
                       if (f["owner_type"], f["owner_id"]) == PLATEFORME), None)
    propre = next((f for f in fiches
                   if (f["owner_type"], f["owner_id"]) == (owner_type, str(owner_id))), None)
    return propre, plateforme


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


def get_bodies(versions: dict[int, int]) -> dict[int, Any]:
    """Les corps de ces versions (`{recipe_id: version}`), par recette, en UNE requête :
    une liste qui lit un corps par recette ne relit pas la base recette par recette."""
    if not versions:
        return {}
    with _connect() as conn:
        rows = conn.execute(
            "SELECT v.recipe_id, v.body FROM recipe_versions v "
            "JOIN unnest(%s::bigint[], %s::int[]) AS w(recipe_id, version) "
            "ON v.recipe_id = w.recipe_id AND v.version = w.version",
            (list(versions), list(versions.values()))).fetchall()
    return {r["recipe_id"]: r["body"] for r in rows}


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
