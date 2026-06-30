"""Ingestion open-data FR : BOAMP (marchés publics) + ACCO (accords d'entreprise).

Extrait de l'ex-monolithe `db.py` (barreau final). Fonctions de domaine — la
plomberie est dans `_conn`. Ré-exporté par `db/__init__`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
from datetime import date, datetime, timezone
from typing import Any, Iterator, Optional

import psycopg

logger = logging.getLogger(__name__)

from ._conn import _connect


_BOAMP_COLS = [
    "idweb", "annee", "objet", "organisme",
    "date_publication", "date_limite_reponse", "date_fin_diffusion",
    "dep_publication", "nature_marche", "type_procedure",
    "type_avis_nature", "type_avis_famille", "statut",
    "descripteurs_libelle", "descripteurs_json", "synthese", "url",
]


def upsert_boamp(rows: list[dict]) -> int:
    """Insère/met à jour des avis BOAMP (clé idweb). Idempotent. Retourne le nb
    de lignes traitées. Conçu pour des batches (ingestion jour-par-jour)."""
    if not rows:
        return 0
    cols = ", ".join(_BOAMP_COLS)
    placeholders = ", ".join(["%s"] * len(_BOAMP_COLS))
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in _BOAMP_COLS if c != "idweb")
    sql = (
        f"INSERT INTO boamp ({cols}, ingested_at) "
        f"VALUES ({placeholders}, NOW()) "
        f"ON CONFLICT (idweb) DO UPDATE SET {updates}, ingested_at = NOW()"
    )
    data = [tuple(r.get(c) for c in _BOAMP_COLS) for r in rows]
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.executemany(sql, data)
    return len(data)


def _boamp_row(r: dict) -> dict:
    """Normalise une ligne BOAMP : descripteurs_json (TEXT) → liste `descripteurs`."""
    out = dict(r)
    raw = out.pop("descripteurs_json", None)
    if raw:
        try:
            out["descripteurs"] = json.loads(raw)
        except (ValueError, TypeError):
            pass
    out.pop("ingested_at", None)
    return out


def search_boamp(
    query: Optional[str] = None,
    descripteur: Optional[str] = None,
    departement: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    type_marche: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Recherche d'avis BOAMP (table PG). Filtres AND. Renvoie {results, total_count}."""
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    clauses, params = ["1=1"], []
    if query:
        clauses.append("objet ILIKE %s"); params.append(f"%{query}%")
    if descripteur:
        clauses.append("descripteurs_libelle ILIKE %s"); params.append(f"%{descripteur}%")
    if departement:
        clauses.append("dep_publication = %s"); params.append(departement)
    if date_from:
        clauses.append("date_publication >= %s"); params.append(date_from)
    if date_to:
        clauses.append("date_publication <= %s"); params.append(date_to)
    if type_marche:
        clauses.append("nature_marche = %s"); params.append(type_marche.upper())
    where = " AND ".join(clauses)
    cols = ", ".join(_BOAMP_COLS)
    with _connect() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM boamp WHERE {where}", tuple(params)
        ).fetchone()["n"]
        rows = conn.execute(
            f"SELECT {cols} FROM boamp WHERE {where} "
            "ORDER BY date_publication DESC NULLS LAST, idweb DESC "
            "LIMIT %s OFFSET %s",
            tuple(params) + (limit, offset),
        ).fetchall()
    return {"results": [_boamp_row(r) for r in rows], "total_count": int(total)}


def get_boamp(idweb: str) -> Optional[dict]:
    """Un avis BOAMP par idweb, ou None."""
    cols = ", ".join(_BOAMP_COLS)
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {cols} FROM boamp WHERE idweb = %s LIMIT 1", (idweb,)
        ).fetchone()
    return _boamp_row(row) if row else None


def boamp_info() -> dict:
    """Métadonnées pour healthcheck : nb de lignes + plage de dates."""
    with _connect() as conn:
        r = conn.execute(
            "SELECT COUNT(*) AS n, MIN(date_publication) AS dmin, "
            "MAX(date_publication) AS dmax FROM boamp"
        ).fetchone()
    return {"total_rows": int(r["n"]), "date_min": r["dmin"], "date_max": r["dmax"]}


def boamp_last_ingested_epoch() -> Optional[float]:
    """Epoch (s) du dernier upsert BOAMP, ou None si table vide. Sert de garde de
    fraîcheur au rafraîchissement in-process (ne pas recrawler si récent)."""
    with _connect() as conn:
        r = conn.execute(
            "SELECT EXTRACT(EPOCH FROM MAX(ingested_at)) AS e FROM boamp"
        ).fetchone()
    return float(r["e"]) if r and r["e"] is not None else None


_ACCO_COLS = [
    "id", "nature", "numero", "siret", "raison_sociale", "code_ape", "code_idcc",
    "secteur", "date_texte", "date_depot", "date_effet", "date_fin", "date_maj",
    "date_diffusion", "conforme_version_integrale", "theme_codes", "themes_libelle",
    "syndicats_libelle", "code_postal", "ville", "titre", "url",
]


# Colonnes triables (whitelist anti-injection : sort_by n'est jamais interpolé brut).
_ACCO_SORT = {
    "date": "date_texte", "date_depot": "date_depot",
    "date_diffusion": "date_diffusion", "date_maj": "date_maj",
}


def upsert_acco(rows: list[dict]) -> int:
    """Insère/met à jour des accords (clé id DILA). Idempotent. Pour batches."""
    if not rows:
        return 0
    cols = ", ".join(_ACCO_COLS)
    placeholders = ", ".join(["%s"] * len(_ACCO_COLS))
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in _ACCO_COLS if c != "id")
    sql = (
        f"INSERT INTO acco ({cols}, ingested_at) "
        f"VALUES ({placeholders}, NOW()) "
        f"ON CONFLICT (id) DO UPDATE SET {updates}, ingested_at = NOW()"
    )
    data = [tuple(r.get(c) for c in _ACCO_COLS) for r in rows]
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.executemany(sql, data)
    return len(data)


def _acco_row(r: dict) -> dict:
    """Normalise une ligne ACCO : theme_codes (TEXT JSON) → liste `theme_codes`."""
    out = dict(r)
    raw = out.get("theme_codes")
    if raw:
        try:
            out["theme_codes"] = json.loads(raw)
        except (ValueError, TypeError):
            out["theme_codes"] = None
    out.pop("ingested_at", None)
    return out


def search_acco(
    query: Optional[str] = None,
    themes: Optional[list[str]] = None,
    nature: Optional[str] = None,
    siren: Optional[str] = None,
    siret: Optional[str] = None,
    idcc: Optional[str] = None,
    departement: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    latest_per_siret: bool = False,
    sort_by: str = "date",
    sort_dir: str = "desc",
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Recherche d'accords d'entreprise (table PG) — primitive neutre, lignes brutes.

    Filtres AND (sauf `themes` : OR interne). `siren` (9 chiffres) matche TOUS les
    établissements de l'entreprise (ACCO indexe l'accord sous le SIRET déposant, pas
    le siège → toujours préférer `siren` à `siret` pour « cette société a-t-elle un
    accord ? »). `latest_per_siret` réduit à 1 ligne par établissement (l'acte le plus
    récent) AVANT d'appliquer date_from/date_to (→ « dernier accord antérieur à X » =
    contrat dormant). Renvoie {results, total_count}."""
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    order_col = _ACCO_SORT.get(sort_by, "date_texte")
    order_dir = "ASC" if str(sort_dir).lower() == "asc" else "DESC"
    order = f"{order_col} {order_dir} NULLS LAST, id {order_dir}"

    # Filtres « population » (avant réduction par SIRET).
    pop, params = ["1=1"], []
    if query:
        pop.append("titre ILIKE %s"); params.append(f"%{query}%")
    if themes:
        ors = []
        for t in themes:
            ors.append("theme_codes LIKE %s"); params.append(f'%"{t}"%')
        pop.append("(" + " OR ".join(ors) + ")")
    if nature:
        pop.append("nature = %s"); params.append(nature.upper())
    if siren:
        pop.append("LEFT(siret, 9) = %s"); params.append(siren)
    if siret:
        pop.append("siret = %s"); params.append(siret)
    if idcc:
        pop.append("code_idcc = %s"); params.append(idcc)
    if departement:
        pop.append("code_postal LIKE %s"); params.append(f"{departement}%")
    pop_clause = " AND ".join(pop)

    # Filtres de date (sur la ligne retenue → après réduction si latest_per_siret).
    date_conds, date_params = [], []
    if date_from:
        date_conds.append("date_texte >= %s"); date_params.append(date_from)
    if date_to:
        date_conds.append("date_texte <= %s"); date_params.append(date_to)
    date_clause = (" AND " + " AND ".join(date_conds)) if date_conds else ""

    cols = ", ".join(_ACCO_COLS)
    if latest_per_siret:
        inner = (
            f"SELECT {cols}, ROW_NUMBER() OVER "
            "(PARTITION BY siret ORDER BY date_texte DESC NULLS LAST, id DESC) AS rn "
            f"FROM acco WHERE {pop_clause} AND siret IS NOT NULL"
        )
        base = f"SELECT {cols} FROM ({inner}) t WHERE rn = 1{date_clause}"
        qparams = params + date_params
    else:
        base = f"SELECT {cols} FROM acco WHERE {pop_clause}{date_clause}"
        qparams = params + date_params

    with _connect() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM ({base}) c", tuple(qparams)
        ).fetchone()["n"]
        rows = conn.execute(
            f"{base} ORDER BY {order} LIMIT %s OFFSET %s",
            tuple(qparams) + (limit, offset),
        ).fetchall()
    return {"results": [_acco_row(r) for r in rows], "total_count": int(total)}


def get_acco(id_or_numero: str) -> Optional[dict]:
    """Un accord par son id DILA (ACCOTEXT…) ou son numero (T…), ou None."""
    cols = ", ".join(_ACCO_COLS)
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {cols} FROM acco WHERE id = %s OR numero = %s LIMIT 1",
            (id_or_numero, id_or_numero),
        ).fetchone()
    return _acco_row(row) if row else None


def acco_themes() -> list[dict]:
    """Catalogue des thèmes présents (code → libellé + nb d'accords). Découverte."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT code, libelle, COUNT(*) AS n FROM acco a, "
            "  UNNEST("
            "    ARRAY(SELECT json_array_elements_text(a.theme_codes::json)), "
            "    string_to_array(a.themes_libelle, ' | ')"
            "  ) AS t(code, libelle) "
            "WHERE a.theme_codes IS NOT NULL "
            "GROUP BY code, libelle ORDER BY n DESC"
        ).fetchall()
    return [{"code": r["code"], "libelle": r["libelle"], "count": int(r["n"])} for r in rows]


def acco_info() -> dict:
    """Métadonnées healthcheck : nb de lignes + plage de dates."""
    with _connect() as conn:
        r = conn.execute(
            "SELECT COUNT(*) AS n, MIN(date_texte) AS dmin, MAX(date_texte) AS dmax FROM acco"
        ).fetchone()
    return {"total_rows": int(r["n"]), "date_min": r["dmin"], "date_max": r["dmax"]}


def acco_last_ingested_epoch() -> Optional[float]:
    """Epoch (s) du dernier upsert ACCO, ou None si table vide."""
    with _connect() as conn:
        r = conn.execute(
            "SELECT EXTRACT(EPOCH FROM MAX(ingested_at)) AS e FROM acco"
        ).fetchone()
    return float(r["e"]) if r and r["e"] is not None else None
