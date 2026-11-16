"""La table société (`waterfall/societe.py`) — le registre par SIREN puis par nom, la garde
d'homonyme (une recherche rend toujours quelque chose), et la page LinkedIn en étape de
COMPLÉTION : elle ne tourne que si la société trouvée n'a pas ce champ."""
from oto_mcp.mcp_errors import McpError
from oto_mcp.tools.meta import IssueCible
from oto_mcp.waterfall import fournisseurs

import pytest

from _waterfall_faux import _made, _run, calls  # noqa: F401 — la fixture des fournisseurs


# --- société ----------------------------------------------------------------------

def test_societe_registre_par_siren(calls):
    calls["answers"]["fr_get"] = IssueCible(ok=True, result={"identity": {
        "siren": "123456789", "nom_complet": "ACME", "activite_principale": "62.01Z",
        "siege": {"libelle_commune": "PARIS"}, "site_internet": "https://acme.com"}})
    out = _run("waterfall_company", siren="123 456 789")
    assert out["provider"] == "fr" and out["attempts"][0]["via"] == "fr_siren"
    assert out["company"]["siren"] == "123456789" and out["company"]["domain"] == "acme.com"
    assert calls["made"] == [("fr_get", {"siren": "123456789"})]


def test_societe_homonyme_ne_stoppe_pas_la_cascade(calls):
    calls["answers"]["fr_search"] = IssueCible(ok=True, result={"results": [
        {"siren": "111111111", "nom_complet": "ACMEX HOLDING"}]})
    calls["answers"]["linkedin_aiark_search"] = IssueCible(ok=True, result={"content": [
        {"summary": {"name": "Acme", "industry": "Software", "staff": {"total": 42}},
         "link": {"linkedin": "https://linkedin.com/company/acme"},
         "location": {"headquarter": {"city": "Paris", "country": "France"}}}]})
    out = _run("waterfall_company", company_name="Acme", domain="acme.com")
    # Le registre (une ligne) : pas de SIREN, la recherche par nom rend un homonyme ;
    # lemlist n'a rien en base.
    assert [a["status"] for a in out["attempts"][:3]] == ["no_match", "no_match", "found"]
    assert [t["via"] for t in out["attempts"][0]["tried"]] == ["fr_name"]
    assert out["provider"] == "aiark"
    assert out["company"]["employees"] == 42 and out["company"]["domain"] == "acme.com"
    assert calls["made"][-1][1]["account"] == {"domain": {"any": {"include": ["acme.com"]}}}


def test_societe_nom_contenu_dans_la_raison_sociale(calls):
    calls["answers"]["fr_search"] = IssueCible(ok=True, result={"results": [
        {"siren": "222222222", "nom_complet": "Société Acmé Industries"}]})
    out = _run("waterfall_company", company_name="acme")
    assert out["provider"] == "fr" and out["company"]["siren"] == "222222222"
    assert out["attempts"][0]["via"] == "fr_name"


def test_societe_theirstack_exige_le_meme_domaine(calls):
    calls["answers"]["theirstack_companies_search"] = IssueCible(ok=True, result={"data": [
        {"name": "Other", "domain": "other.com"}, {"name": "Acme", "domain": "acme.com",
                                                   "employee_count": 7}]})
    out = _run("waterfall_company", domain="acme.com", providers=["theirstack"])
    assert out["company"]["name"] == "Acme" and out["company"]["employees"] == 7


def test_societe_hors_france_saute_le_registre_en_le_disant(calls):
    out = _run("waterfall_company", company_name="Acme", domain="acme.de", country="DE",
               dry_run=True)
    plan = {a["provider"]: a for a in out["attempts"]}
    assert plan["fr"]["status"] == "skipped"
    assert "French company" in plan["fr"]["message"]
    assert plan["apollo"]["status"] == "planned"


def test_siren_malforme_refuse(calls):
    with pytest.raises(McpError, match="9 digits"):
        _run("waterfall_company", siren="12345")


# --- la page LinkedIn d'une société : étapes de COMPLÉTION ------------------------

def test_la_page_linkedin_complete_une_societe_identifiee_sans_elle(calls):
    calls["answers"]["apollo_enrich_organization"] = IssueCible(ok=True, result={
        "organization": {"name": "Acme", "primary_domain": "acme.com"}})
    calls["answers"]["apify_run_sync"] = IssueCible(ok=True, result=[
        {"domain": "acme.com", "linkedIns": ["https://www.linkedin.com/in/jane-doe",
                                             "https://fr.linkedin.com/company/Acme-SAS/about"]}])
    out = _run("waterfall_company", domain="acme.com", providers=["apollo", "linkedin"])
    assert out["provider"] == "apollo"
    assert out["company"]["name"] == "Acme"
    assert out["company"]["linkedin_url"] == "https://www.linkedin.com/company/acme-sas/"
    assert out["company"]["linkedin_source"] == "website"
    assert calls["made"][1][1]["actor_id"] == "vdrmota/contact-info-scraper"
    assert calls["made"][1][1]["timeout_secs"] == fournisseurs._APIFY_TIMEOUT_S


def test_la_completion_ne_tourne_pas_quand_la_page_est_deja_la(calls):
    calls["answers"]["apollo_enrich_organization"] = IssueCible(ok=True, result={
        "organization": {"name": "Acme", "primary_domain": "acme.com",
                         "linkedin_url": "https://www.linkedin.com/company/acme"}})
    out = _run("waterfall_company", domain="acme.com", providers=["apollo", "linkedin"])
    assert _made(calls) == ["apollo_enrich_organization"]
    assert [a["provider"] for a in out["attempts"]] == ["apollo"]


def test_la_recherche_par_nom_ecarte_l_homonyme(calls):
    calls["answers"]["apify_run_sync"] = IssueCible(ok=True, result=[
        {"name": "Acmebird Restaurant", "linkedinUrl": "https://www.linkedin.com/company/acmebird-food",
         "website": "https://acmebird-food.example"},
        {"name": "Acmebird", "linkedinUrl": "https://www.linkedin.com/company/acmebird3d",
         "website": "https://www.acmebird3d.example", "employeeCount": 42}])
    out = _run("waterfall_company", company_name="Acmebird", domain="acmebird3d.example",
               providers=["linkedin"])
    # Le site n'a rien donné (même réponse servie : un homonyme écarté), la recherche par
    # nom a trouvé — et, rien d'autre n'ayant identifié la société, la page suffit.
    assert out["found"] is True and out["provider"] == "linkedin"
    assert out["attempts"][0]["via"] == "linkedin_search"
    assert [t["via"] for t in out["attempts"][0]["tried"]] == ["linkedin_site", "linkedin_search"]
    assert out["company"]["linkedin_url"] == "https://www.linkedin.com/company/acmebird3d/"
    assert out["company"]["employees"] == 42


def test_une_execution_apify_coupee_rend_encore_ses_elements(calls):
    calls["answers"]["apify_run_sync"] = IssueCible(ok=True, result={
        "error": "TIMED-OUT", "run_id": "r1", "status": "TIMED-OUT",
        "items": [{"linkedIns": ["https://linkedin.com/company/acme"]}]})
    out = _run("waterfall_company", domain="acme.com", providers=["linkedin"])
    assert out["company"]["linkedin_url"] == "https://www.linkedin.com/company/acme/"


def test_reel_aiark_societe_par_domaine_avec_url_et_chiffre_d_affaires(calls):
    calls["answers"]["linkedin_aiark_search"] = IssueCible(ok=True, result={"content": [{
        "summary": {"name": "ACME GmbH", "founded_year": 2025, "industry": "it services",
                    "staff": {"total": 4, "range": {"start": 2, "end": 10}}},
        "link": {"website": "http://www.acme.de", "domain": "http://www.acme.de",
                 "linkedin": "https://www.linkedin.com/company/acme-gmbh"},
        "contact": {"phone": {"raw": "+49 1556 0000000", "sanitized": "+4915560000000"}},
        "financial": {"revenue": {"annual": {"amount": "0-100000"}}}}]})
    out = _run("waterfall_company", domain="acme.de", providers=["aiark"])
    c = out["company"]
    assert c["domain"] == "acme.de" and c["employees"] == 4 and c["revenue_range"] == "0-100000"
    assert c["phone"] == "+4915560000000" and c["linkedin_url"].endswith("/acme-gmbh")


@pytest.mark.parametrize("mode", ["forbidden", "over_quota"])
def test_le_registre_ne_depend_pas_de_la_cle_insee(calls, mode):
    """`fr_get` et `fr_search` lisent l'open data et ne résolvent aucune clé, alors que
    leur connecteur en porte une (l'INSEE, pour `fr_siret`) : le registre n'est JAMAIS
    sauté `no_key` sur le mode de cette clé (sauté dans une org où il
    répondait en direct, la cascade tombait sur un homonyme d'un autre pays)."""
    calls["modes"] = {"fr_get": mode, "fr_search": mode}
    calls["answers"]["fr_get"] = IssueCible(ok=True, result={"identity": {
        "siren": "123456789", "nom_complet": "ACME", "site_internet": "https://acme.com"}})
    out = _run("waterfall_company", siren="123456789", domain="acme.com")
    assert out["provider"] == "fr" and out["attempts"][0]["key"] == "open"
    assert calls["made"][0] == ("fr_get", {"siren": "123456789"})


def test_le_plan_dit_le_registre_utilisable_sans_cle(monkeypatch):
    from oto_mcp.waterfall import ordre, plan
    monkeypatch.setattr(ordre, "_configured_steps", lambda ns: None)
    monkeypatch.setattr(ordre, "_key_modes", lambda sub, tools: {t: "forbidden" for t in tools})
    fr = next(s for s in plan.plan("waterfall_company", "sub-test")["steps"]
              if s["key"] == "fr")
    assert (fr["key_mode"], fr["usable"]) == ("open", True)
