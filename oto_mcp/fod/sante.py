"""Clients HTTP minces vers le service FOD — capacité « sante » (ADR 0028, B3).

FINESS (annuaire) + HAS ESSMS (évaluations qualité, DuckDB sur parquet distant —
workload lourd) servis par le service FOD. Objets proxy répliquant la surface des
clients `france_opendata`. Pas de fallback (ADR 0028).

Médicaments (BDPM), essais cliniques (ClinicalTrials.gov, CTIS), publications (PubMed,
Europe PMC) et liens d'intérêts (Transparence Santé) : routes `/api/sante/*` du même
service, sous la même clé. Un argument laissé vide n'est PAS envoyé : le service
applique son propre défaut (un `pays=` vide y veut dire « monde entier », un `siren=`
vide y est refusé). Un identifiant inconnu rend `null`.
"""
from __future__ import annotations

from typing import Any, Optional
from urllib.parse import quote

from .http import get as _get, post as _post


def _params(**kw: Any) -> dict[str, Any]:
    """Les seuls arguments donnés : `None` n'est jamais envoyé (httpx l'enverrait vide)."""
    return {k: v for k, v in kw.items() if v is not None}


def _seg(valeur: str) -> str:
    """Un identifiant posé dans le CHEMIN, échappé : il ne peut pas en changer la route."""
    return quote(str(valeur), safe="")


class _Finess:
    def search(self, q: str, departement: Optional[str] = None,
               categorie: Optional[str] = None, limit: int = 20) -> dict[str, Any]:
        return _get("/api/sante/finess/search",
                    {"q": q, "departement": departement, "categorie": categorie, "limit": limit})

    def by_code(self, code: str) -> Optional[dict[str, Any]]:
        return _get(f"/api/sante/finess/{code}")


class _HasEssms:
    def search(self, region_libelle: Optional[str] = None, departement_code: Optional[str] = None,
               secteur: Optional[str] = None, type_structure: Optional[str] = None,
               statut_juridique: Optional[str] = None, annee_min: Optional[int] = None,
               annee_max: Optional[int] = None, limit: int = 50) -> dict[str, Any]:
        return _post("/api/sante/essms/search", {
            "region_libelle": region_libelle, "departement_code": departement_code,
            "secteur": secteur, "type_structure": type_structure,
            "statut_juridique": statut_juridique, "annee_min": annee_min,
            "annee_max": annee_max, "limit": limit,
        })

    def dimensions(self) -> dict[str, Any]:
        return _get("/api/sante/essms/dimensions")


class _Medicaments:
    """Base de données publique des médicaments (ANSM, HAS, Assurance maladie)."""

    def search(self, q: str, commercialises: Optional[bool] = None,
               limit: Optional[int] = None) -> dict[str, Any]:
        return _get("/api/sante/medicaments",
                    _params(q=q, commercialises=commercialises, limit=limit))

    def fiche(self, cis: str) -> Optional[dict[str, Any]]:
        return _get(f"/api/sante/medicaments/{_seg(cis)}")


class _Essais:
    """Essais cliniques : ClinicalTrials.gov (NLM) et CTIS (portail public de l'EMA)."""

    def clinicaltrials(self, *, condition: Optional[str] = None,
                       intervention: Optional[str] = None, promoteur: Optional[str] = None,
                       terme: Optional[str] = None, pays: Optional[str] = None,
                       statut: Optional[list[str]] = None, limit: Optional[int] = None,
                       page_token: Optional[str] = None) -> dict[str, Any]:
        return _get("/api/sante/essais/clinicaltrials", _params(
            condition=condition, intervention=intervention, promoteur=promoteur,
            terme=terme, pays=pays, statut=statut, limit=limit, page_token=page_token))

    def clinicaltrial(self, nct: str, pays: Optional[str] = None) -> Optional[dict[str, Any]]:
        return _get(f"/api/sante/essais/clinicaltrials/{_seg(nct)}", _params(pays=pays))

    def ctis(self, *, texte: Optional[str] = None, condition: Optional[str] = None,
             promoteur: Optional[str] = None, produit: Optional[str] = None,
             pays: Optional[list[str]] = None, statut: Optional[list[int]] = None,
             page: Optional[int] = None, limit: Optional[int] = None) -> dict[str, Any]:
        return _get("/api/sante/essais/ctis", _params(
            texte=texte, condition=condition, promoteur=promoteur, produit=produit,
            pays=pays, statut=statut, page=page, limit=limit))

    def ctis_essai(self, numero: str, pays: Optional[str] = None) -> Optional[dict[str, Any]]:
        return _get(f"/api/sante/essais/ctis/{_seg(numero)}", _params(pays=pays))


class _Publications:
    """Publications biomédicales : PubMed (NCBI) et Europe PMC (EMBL-EBI)."""

    def pubmed(self, query: str, *, limit: Optional[int] = None, offset: Optional[int] = None,
               tri: Optional[str] = None) -> dict[str, Any]:
        return _get("/api/sante/publications/pubmed",
                    _params(query=query, limit=limit, offset=offset, tri=tri))

    def pubmed_article(self, pmid: str) -> Optional[dict[str, Any]]:
        return _get(f"/api/sante/publications/pubmed/{_seg(pmid)}")

    def europepmc(self, query: str, *, limit: Optional[int] = None,
                  curseur: Optional[str] = None) -> dict[str, Any]:
        return _get("/api/sante/publications/europepmc",
                    _params(query=query, limit=limit, curseur=curseur))

    def europepmc_article(self, *, pmid: Optional[str] = None,
                          doi: Optional[str] = None) -> Optional[dict[str, Any]]:
        return _get("/api/sante/publications/europepmc/article", _params(pmid=pmid, doi=doi))


class _Transparence:
    """Liens d'intérêts déclarés (Transparence Santé) — nominatifs, CC BY-NC-SA 4.0."""

    def declarations(self, *, limit: Optional[int] = None, offset: Optional[int] = None,
                     **filtres: Any) -> dict[str, Any]:
        return _get("/api/sante/transparence/declarations",
                    _params(limit=limit, offset=offset, **filtres))

    def par_entreprise(self, *, limit: Optional[int] = None, offset: Optional[int] = None,
                       **filtres: Any) -> dict[str, Any]:
        return _get("/api/sante/transparence/par-entreprise",
                    _params(limit=limit, offset=offset, **filtres))


finess = _Finess()
has = _HasEssms()
medicaments = _Medicaments()
essais = _Essais()
publications = _Publications()
transparence = _Transparence()
