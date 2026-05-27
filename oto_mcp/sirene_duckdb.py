"""DuckDB read-only over StockEtablissement.parquet (~35M rows, INSEE SIRENE).

Le parquet vit sur le filesystem du serveur (tuls.me typiquement) — path résolu
via `SIRENE_STOCK_PARQUET_PATH` env, défaut `/opt/oto-mcp/data/sirene/StockEtablissement.parquet`.

DuckDB est utilisé en mode lecture seule. On ouvre une connexion à la demande,
on enregistre une view sur le parquet, on query. Pas d'index — DuckDB lit
les row groups + columnar pruning. Lookups par `siren`/`siret` sur 35M = 50-200ms
à froid, ~10-50ms à chaud (page cache OS).

Tous les retours sont des dicts avec snake_case (transformés depuis les camelCase
INSEE). NULLs explicites au lieu de strings vides.
"""
from __future__ import annotations

import os
from typing import Any, Optional

import duckdb


DEFAULT_PATH = "/opt/oto-mcp/data/sirene/StockEtablissement.parquet"


def parquet_path() -> str:
    return os.environ.get("SIRENE_STOCK_PARQUET_PATH", DEFAULT_PATH)


# Colonnes parquet INSEE → snake_case stable côté API.
_COLUMN_MAP = {
    "siren": "siren",
    "siret": "siret",
    "nic": "nic",
    "etablissementSiege": "is_siege",
    "etatAdministratifEtablissement": "etat",
    "dateCreationEtablissement": "date_creation",
    "dateDebut": "date_debut",
    "denominationUsuelleEtablissement": "denomination",
    "enseigne1Etablissement": "enseigne_1",
    "enseigne2Etablissement": "enseigne_2",
    "enseigne3Etablissement": "enseigne_3",
    "activitePrincipaleEtablissement": "naf",
    "nomenclatureActivitePrincipaleEtablissement": "naf_nomenclature",
    "trancheEffectifsEtablissement": "tranche_effectifs",
    "anneeEffectifsEtablissement": "annee_effectifs",
    "complementAdresseEtablissement": "complement_adresse",
    "numeroVoieEtablissement": "numero_voie",
    "indiceRepetitionEtablissement": "indice_repetition",
    "typeVoieEtablissement": "type_voie",
    "libelleVoieEtablissement": "libelle_voie",
    "codePostalEtablissement": "code_postal",
    "libelleCommuneEtablissement": "libelle_commune",
    "codeCommuneEtablissement": "code_commune",
    "codeCedexEtablissement": "code_cedex",
    "libelleCedexEtablissement": "libelle_cedex",
    "distributionSpecialeEtablissement": "distribution_speciale",
    "coordonneeLambertAbscisseEtablissement": "lambert_x",
    "coordonneeLambertOrdonneeEtablissement": "lambert_y",
    "libelleCommuneEtrangerEtablissement": "libelle_commune_etranger",
    "codePaysEtrangerEtablissement": "code_pays_etranger",
    "libellePaysEtrangerEtablissement": "libelle_pays_etranger",
    "dateDernierTraitementEtablissement": "date_dernier_traitement",
}

_SELECT_CLAUSE = ", ".join(f'"{src}" AS {dst}' for src, dst in _COLUMN_MAP.items())


def _connect() -> duckdb.DuckDBPyConnection:
    """Une nouvelle connexion read-only par appel. DuckDB est rapide à ouvrir
    (~ms) et les connections ne sont pas thread-safe pour des queries concurrentes,
    donc on évite de partager. La page cache OS fait le travail de mise en cache."""
    conn = duckdb.connect(database=":memory:", read_only=False)
    # `read_only=False` sur le :memory: store, mais le parquet est en lecture seule de facto.
    return conn


def _from_parquet() -> str:
    """Clause FROM pointant le parquet — quoté/échappé via DuckDB."""
    return f"read_parquet('{parquet_path()}')"


def _row_to_dict(row: tuple, columns: list[str]) -> dict[str, Any]:
    """Normalise les bools/strings vides issus du parquet."""
    out: dict[str, Any] = {}
    for col, val in zip(columns, row):
        if val == "" or val is None:
            out[col] = None
        elif col == "is_siege":
            out[col] = bool(val) if val is not None else None
        else:
            out[col] = val
    return out


def _output_columns() -> list[str]:
    return list(_COLUMN_MAP.values())


def lookup_siege(siren: str) -> Optional[dict[str, Any]]:
    """Renvoie le siège (etablissementSiege=True) pour un SIREN, ou None.

    Si plusieurs sièges existent dans l'historique (changement d'adresse rare),
    on prend celui dont la période est encore ouverte (date_debut max).
    """
    sql = (
        f"SELECT {_SELECT_CLAUSE} FROM {_from_parquet()} "
        "WHERE siren = ? AND etablissementSiege = TRUE "
        "ORDER BY dateDebut DESC NULLS LAST "
        "LIMIT 1"
    )
    with _connect() as conn:
        row = conn.execute(sql, [siren]).fetchone()
    return _row_to_dict(row, _output_columns()) if row else None


def list_establishments(siren: str, active_only: bool = True) -> list[dict[str, Any]]:
    """Liste tous les établissements d'un SIREN (sièges + secondaires).

    Args:
        siren: 9 chiffres
        active_only: filtre etatAdministratif = 'A'
    """
    where = ["siren = ?"]
    params: list[Any] = [siren]
    if active_only:
        where.append("etatAdministratifEtablissement = 'A'")
    sql = (
        f"SELECT {_SELECT_CLAUSE} FROM {_from_parquet()} "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY etablissementSiege DESC, dateDebut DESC NULLS LAST"
    )
    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    cols = _output_columns()
    return [_row_to_dict(r, cols) for r in rows]


def lookup_siret(siret: str) -> Optional[dict[str, Any]]:
    """Renvoie un établissement précis par SIRET (14 chiffres)."""
    sql = (
        f"SELECT {_SELECT_CLAUSE} FROM {_from_parquet()} "
        "WHERE siret = ? LIMIT 1"
    )
    with _connect() as conn:
        row = conn.execute(sql, [siret]).fetchone()
    return _row_to_dict(row, _output_columns()) if row else None


def search(
    naf: Optional[str] = None,
    code_commune: Optional[str] = None,
    code_postal: Optional[str] = None,
    denomination: Optional[str] = None,
    enseigne: Optional[str] = None,
    active_only: bool = True,
    sieges_only: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Recherche multi-critères. Tous les filtres sont AND.

    Args:
        naf: code APE (ex. "4711F", "10.71C") — match exact sur activitePrincipale
        code_commune: code INSEE COG (ex. "13201" pour Marseille 1er)
        code_postal: ex. "13001"
        denomination: substring case-insensitive sur denomination ou enseigne
        enseigne: substring case-insensitive sur enseigne1/2/3
        active_only: filtre etat='A'
        sieges_only: ne renvoie que les sièges
        limit: max 1000
        offset: pagination
    """
    limit = max(1, min(limit, 1000))
    offset = max(0, offset)
    where = ["1=1"]
    params: list[Any] = []

    if active_only:
        where.append("etatAdministratifEtablissement = 'A'")
    if sieges_only:
        where.append("etablissementSiege = TRUE")
    if naf:
        where.append("activitePrincipaleEtablissement = ?")
        params.append(naf)
    if code_commune:
        where.append("codeCommuneEtablissement = ?")
        params.append(code_commune)
    if code_postal:
        where.append("codePostalEtablissement = ?")
        params.append(code_postal)
    if denomination:
        where.append("LOWER(denominationUsuelleEtablissement) LIKE ?")
        params.append(f"%{denomination.lower()}%")
    if enseigne:
        where.append(
            "(LOWER(enseigne1Etablissement) LIKE ? OR "
            " LOWER(enseigne2Etablissement) LIKE ? OR "
            " LOWER(enseigne3Etablissement) LIKE ?)"
        )
        like = f"%{enseigne.lower()}%"
        params.extend([like, like, like])

    sql = (
        f"SELECT {_SELECT_CLAUSE} FROM {_from_parquet()} "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY siret "
        "LIMIT ? OFFSET ?"
    )
    params.extend([limit, offset])

    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    cols = _output_columns()
    return [_row_to_dict(r, cols) for r in rows]


def count_active() -> int:
    """Comptage rapide pour sanity check / monitoring."""
    sql = f"SELECT COUNT(*) FROM {_from_parquet()} WHERE etatAdministratifEtablissement = 'A'"
    with _connect() as conn:
        return int(conn.execute(sql).fetchone()[0])


def parquet_info() -> dict[str, Any]:
    """Métadonnées pour healthcheck : taille fichier, dernière modif, count."""
    path = parquet_path()
    info: dict[str, Any] = {"path": path}
    try:
        st = os.stat(path)
        info["size_bytes"] = st.st_size
        info["mtime"] = st.st_mtime
    except FileNotFoundError:
        info["error"] = "not_found"
        return info
    try:
        info["total_rows"] = int(
            _connect().execute(f"SELECT COUNT(*) FROM {_from_parquet()}").fetchone()[0]
        )
    except Exception as e:
        info["query_error"] = str(e)
    return info
