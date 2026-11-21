"""`sante(op=…)` : chaque op appelle SA route FOD avec SES paramètres, sans réseau.

Médicaments (BDPM), essais (ClinicalTrials.gov, CTIS), publications (PubMed, Europe PMC)
et liens d'intérêts (Transparence Santé) passent par `fod/sante`, qui pose la route et
n'envoie jamais un argument laissé vide (le service a ses propres défauts : un `pays=`
vide y veut dire « monde entier », un `siren=` vide y est refusé). La doublure remplace
`_get` : ce qui est fixé ici, c'est le chemin et les paramètres que FOD reçoit.
"""
import asyncio

import pytest

import oto_mcp.fod.sante  # noqa: F401 — cible du monkeypatch (import à l'appel)
from oto_mcp.mcp_errors import McpError
from oto_mcp.tools.lecture import LECTURE


def _outil():
    from fastmcp import FastMCP
    from oto_mcp.tools import sante as S

    m = FastMCP("t")
    S.register(m)
    return asyncio.run(m.get_tool("sante"))


class _Appels(list):
    """Les appels vus, et ce que FOD rend par chemin (`{}` par défaut)."""

    def __init__(self):
        super().__init__()
        self.reponses: dict[str, object] = {}


@pytest.fixture
def appels(monkeypatch):
    vus = _Appels()
    reponses = vus.reponses

    def faux_get(path, params=None, headers=None, timeout=None):
        vus.append((path, params))
        return reponses.get(path, {})

    monkeypatch.setattr("oto_mcp.fod.sante._get", faux_get)
    return vus


def _appel(**kw):
    return _outil().fn(**kw)


def test_l_outil_lit_et_rien_d_autre():
    t = _outil()
    assert t.annotations == LECTURE
    from oto_mcp.tools.sante import OPS
    assert set(t.parameters["properties"]["op"]["enum"]) == set(OPS)


@pytest.mark.parametrize("kw, path, params", [
    ({"op": "search_medicines", "q": "ibuprofène"},
     "/api/sante/medicaments", {"q": "ibuprofène"}),
    ({"op": "search_medicines", "q": "doliprane", "marketed_only": True, "limit": 5},
     "/api/sante/medicaments", {"q": "doliprane", "commercialises": True, "limit": 5}),
    ({"op": "get_medicine", "cis": "60234100"}, "/api/sante/medicaments/60234100", None),
    ({"op": "search_clinicaltrials", "condition": "glioblastoma", "status": ["RECRUITING"],
      "page_token": "abc"},
     "/api/sante/essais/clinicaltrials",
     {"condition": "glioblastoma", "statut": ["RECRUITING"], "page_token": "abc"}),
    ({"op": "search_clinicaltrials", "sponsor": "Inserm", "text": "x", "intervention": "y",
      "country": ""},
     "/api/sante/essais/clinicaltrials",
     {"promoteur": "Inserm", "terme": "x", "intervention": "y", "pays": ""}),
    ({"op": "get_clinicaltrial", "nct": "NCT03958240"},
     "/api/sante/essais/clinicaltrials/NCT03958240", {}),
    ({"op": "get_clinicaltrial", "nct": "NCT03958240", "country": "Spain"},
     "/api/sante/essais/clinicaltrials/NCT03958240", {"pays": "Spain"}),
    ({"op": "search_ctis_trials", "text": "lymphoma", "product": "p", "sponsor": "s",
      "countries": ["FR", "BE"], "status": ["3", "4"], "page": 2, "limit": 10},
     "/api/sante/essais/ctis",
     {"texte": "lymphoma", "produit": "p", "promoteur": "s", "pays": ["FR", "BE"],
      "statut": [3, 4], "page": 2, "limit": 10}),
    ({"op": "get_ctis_trial", "eu_ct_number": "2024-520051-24-00", "country": "DE"},
     "/api/sante/essais/ctis/2024-520051-24-00", {"pays": "DE"}),
    ({"op": "search_pubmed", "query": "glioblastoma AND france[ad]", "sort": "relevance",
      "offset": 20},
     "/api/sante/publications/pubmed",
     {"query": "glioblastoma AND france[ad]", "tri": "pertinence", "offset": 20}),
    ({"op": "search_pubmed", "query": "x", "sort": "date"},
     "/api/sante/publications/pubmed", {"query": "x", "tri": "date"}),
    ({"op": "get_pubmed_article", "pmid": "31234567"},
     "/api/sante/publications/pubmed/31234567", None),
    ({"op": "search_europepmc", "query": "AUTH:\"Doe J\"", "cursor": "c2"},
     "/api/sante/publications/europepmc", {"query": "AUTH:\"Doe J\"", "curseur": "c2"}),
    ({"op": "get_europepmc_article", "doi": "10.1000/xyz"},
     "/api/sante/publications/europepmc/article", {"doi": "10.1000/xyz"}),
    ({"op": "search_interest_declarations", "siren": "123456789", "types": ["convention"],
      "date_min": "2024-01-01", "limit": 5, "offset": 10},
     "/api/sante/transparence/declarations",
     {"siren": "123456789", "types": ["convention"], "date_min": "2024-01-01",
      "limit": 5, "offset": 10}),
    ({"op": "interest_totals_by_company", "rpps": "10001234567", "company": "acme",
      "beneficiary": "Martin", "city": "Lyon", "department": "69", "date_max": "2025-12-31"},
     "/api/sante/transparence/par-entreprise",
     {"rpps": "10001234567", "entreprise": "acme", "beneficiaire": "Martin", "ville": "Lyon",
      "departement": "69", "date_max": "2025-12-31"}),
])
def test_la_route_et_les_parametres(appels, kw, path, params):
    _appel(**kw)
    assert appels == [(path, params)]


def test_un_identifiant_ne_change_pas_de_route(appels):
    _appel(op="get_medicine", cis="../finess/1")
    assert appels[0][0] == "/api/sante/medicaments/..%2Ffiness%2F1"


def test_un_identifiant_inconnu_le_dit(appels):
    appels.reponses["/api/sante/medicaments/00000000"] = None
    assert _appel(op="get_medicine", cis="00000000") == {"found": False, "cis": "00000000"}


@pytest.mark.parametrize("kw, manque", [
    ({"op": "search_medicines"}, "q"),
    ({"op": "get_medicine"}, "cis"),
    ({"op": "get_clinicaltrial"}, "nct"),
    ({"op": "get_ctis_trial"}, "eu_ct_number"),
    ({"op": "search_pubmed"}, "query"),
    ({"op": "search_europepmc"}, "query"),
    ({"op": "get_pubmed_article"}, "pmid"),
    ({"op": "get_europepmc_article"}, "pmid or doi"),
    ({"op": "search_ctis_trials", "status": ["recruiting"]}, "'1' to '12'"),
])
def test_un_argument_manquant_est_refuse_sans_appel(appels, kw, manque):
    with pytest.raises(McpError, match=manque):
        _appel(**kw)
    assert appels == []


def test_la_vue_par_defaut_retire_ce_que_la_fiche_repete(appels):
    essai = {"nct": "NCT1", "titre": "t", "titre_officiel": "long", "collaborateurs": ["c"],
             "pays_sites": "France", "statut": "RECRUITING"}
    appels.reponses["/api/sante/essais/clinicaltrials"] = {
        "total": 1, "essais": [essai], "page_suivante": "n", "source": {"producteur": "NLM"}}
    r = _appel(op="search_clinicaltrials", condition="x")
    assert r == {"total": 1, "essais": [{"nct": "NCT1", "titre": "t", "statut": "RECRUITING"}],
                 "page_suivante": "n", "source": {"producteur": "NLM"}}
    assert _appel(op="search_clinicaltrials", condition="x", full=True)["essais"] == [essai]


def test_les_montants_et_la_licence_passent_tels_quels(appels):
    """Le lien `convention_liee` (montants non additionnables) et `source` (licence,
    attribution) ne sont jamais retirés de la vue par défaut."""
    d = {"type": "remuneration", "montant": 500.0, "convention_liee": "C1", "periode": {},
         "publiee_le": "2025-01-01", "statut": "x", "entreprise": {}, "beneficiaire": {}}
    source = {"licence": "CC BY-NC-SA 4.0", "attribution": "a"}
    appels.reponses["/api/sante/transparence/declarations"] = {
        "total": 1, "declarations": [d], "source": source}
    r = _appel(op="search_interest_declarations", siren="123456789")
    assert r["source"] == source
    assert r["declarations"][0]["convention_liee"] == "C1"
    assert r["declarations"][0]["montant"] == 500.0


def test_la_description_dit_ce_que_l_agent_doit_savoir():
    d = _outil().description
    for attendu in ("NEVER add amounts", "CC BY-NC-SA 4.0", "undocumented API", "source",
                    "Direction Générale de la Santé", "base Transparence - Santé"):
        assert attendu in d, attendu
