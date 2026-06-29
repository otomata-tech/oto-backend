"""Santé — établissements sanitaires & médico-sociaux (open data France, sans clé).

- **FINESS** : annuaire des établissements (sanitaire + médico-social), data.gouv.
- **HAS ESSMS** : évaluations qualité des ESSMS (référentiel HAS), lues en DuckDB
  sur parquet distant — nécessite l'extra `france-opendata[sante]`.

Connecteur open-data : pas de credential. Exposé seulement si activé en DB
(cran d'activation, ADR 0010) — register_all gate sur `connector_activation`.
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP


def register(mcp: FastMCP) -> None:
    from france_opendata import FinessClient
    from france_opendata.has_essms import HasEssmsClient

    finess = FinessClient()
    has = HasEssmsClient()

    # --- annuaire FINESS -----------------------------------------------------

    @mcp.tool()
    def sante_finess_search(
        q: str,
        departement: Optional[str] = None,
        categorie: Optional[str] = None,
        limit: int = 20,
    ) -> dict:
        """Search FINESS establishments by name or FINESS code (health + medico-social).

        Returns {count, results} with FINESS ET/EJ, raison sociale, address,
        commune, category, SIRET, APE.

        Args:
            q: FINESS code (prefix) or free-text name (multi-word, accent-insensitive).
            departement: INSEE department code (e.g. "75").
            categorie: substring of the category label (e.g. "EHPAD", "Centre Hospitalier").
            limit: max results (default 20).
        """
        return finess.search(q, departement=departement, categorie=categorie, limit=limit)

    @mcp.tool()
    def sante_finess(finess: str) -> Optional[dict]:
        """FINESS establishment by exact code (ET or EJ), or null."""
        return finess.by_code(finess)

    # --- évaluations qualité ESSMS (HAS) -------------------------------------

    @mcp.tool()
    def sante_essms_search(
        region_libelle: Optional[str] = None,
        departement_code: Optional[str] = None,
        secteur: Optional[str] = None,
        type_structure: Optional[str] = None,
        statut_juridique: Optional[str] = None,
        annee_min: Optional[int] = None,
        annee_max: Optional[int] = None,
        limit: int = 50,
    ) -> dict:
        """Search evaluated ESSMS (HAS quality evaluations, référentiel EDS).

        Returns {total, returned, results} (FINESS, raison sociale, region,
        category, sector, evaluation date, quality index). ⚠️ `region_libelle`
        values are UPPERCASE, no accents (e.g. "ILE DE FRANCE") — call
        `sante_essms_dimensions` to get the exact filter values.

        Args:
            region_libelle: region label (uppercase, no accents).
            departement_code: INSEE department code.
            secteur: "Social" | "Médico-social".
            type_structure: "Etablissement" | "Service".
            statut_juridique: legal status label.
            annee_min / annee_max: evaluation year band.
            limit: max results (default 50).
        """
        return has.search(
            region_libelle=region_libelle, departement_code=departement_code,
            secteur=secteur, type_structure=type_structure, statut_juridique=statut_juridique,
            annee_min=annee_min, annee_max=annee_max, limit=limit,
        )

    @mcp.tool()
    def sante_essms_dimensions() -> dict:
        """Distinct filter values for ESSMS evaluations (regions, sectors, types,
        legal statuses, categories, years) with counts — to build a valid
        `sante_essms_search` query."""
        return has.dimensions()
