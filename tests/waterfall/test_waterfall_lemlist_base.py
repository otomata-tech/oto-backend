"""La base partagée de lemlist (`lemlist_database`) dans les cascades société, pile
technique et personne : aucun crédit, un appel du quota de la clé (`quota_only`) — devant
les fiches payantes, et en tête des clés de l'appelant derrière les seules étapes gratuites.
Les fiches sont des réponses réelles réduites (21/10/2026), noms et domaines remplacés."""
import pytest

from oto_mcp.tools.meta import IssueCible

from _waterfall_faux import _made, _run, calls  # noqa: F401 — la fixture des fournisseurs


def _societe(domain="acme.fr", **extra):
    rec = {"company_name": "Acme", "company_domain": domain,
           "company_website_url": f"https://www.{domain}",
           "company_linkedin_url": "https://www.linkedin.com/company/acme",
           "company_employee_count": "454", "company_size": "201-500",
           "company_industry": "Technology, Information and Media",
           "company_subindustry": "Technology, Information and Internet",
           "company_subsubindustry": "Software Development", "company_founded": "2014",
           "company_headquarters_city": "Paris", "company_headquarters_country": "France",
           "company_type": "Privately Held", "business_business_customer": "B2B",
           "technologies": [
               {"name": "HubSpot", "scoring": "CONFIRMED"},
               {"name": "Salesforce", "scoring": "LIKELY"},
               {"name": "Shopify", "scoring": "POTENTIAL"}],
           # Ce qui alourdit une fiche (~0,5 Mo mesuré) : jamais lu.
           "job_post_last_6m_array": [{"job_title": "Head of Finance"}]}
    rec.update(extra)
    return IssueCible(ok=True, result={"results": [rec], "total": 1, "page": 1,
                                       "size": 1, "limitation": 1995})


_VIDE = IssueCible(ok=True, result={"results": [], "total": 0, "page": 1, "size": 1})


# --- société ----------------------------------------------------------------------

def test_societe_la_fiche_lemlist_par_le_site(calls):
    calls["answers"]["lemlist_database"] = _societe()
    out = _run("waterfall_company", domain="https://www.acme.fr/", providers=["lemlist"])
    assert out["provider"] == "lemlist"
    assert out["company"] == {
        "name": "Acme", "domain": "acme.fr", "country": "France", "city": "Paris",
        "employees": 454, "employees_band": "201-500", "industry": "Software Development",
        "founded": 2014, "linkedin_url": "https://www.linkedin.com/company/acme",
        "type": "Privately Held", "market": "B2B"}
    assert calls["made"] == [("lemlist_database", {
        "op": "companies", "page": 1, "size": 1,
        "filters": [{"filterId": "currentCompanyWebsiteUrl", "in": ["acme.fr"], "out": []}]})]


def test_societe_lemlist_d_un_autre_domaine_ne_compte_pas(calls):
    calls["answers"]["lemlist_database"] = _societe(domain="acme-group.com")
    out = _run("waterfall_company", domain="acme.fr", providers=["lemlist"])
    assert out["found"] is False


def test_societe_lemlist_passe_devant_les_fiches_payantes(calls):
    calls["answers"]["lemlist_database"] = _societe()
    out = _run("waterfall_company", company_name="Acme", domain="acme.fr", country="DE")
    assert out["provider"] == "lemlist"
    assert "linkedin_aiark_search" not in _made(calls)


@pytest.mark.parametrize("mode", ["org", "user", "group"])
def test_societe_sur_sa_cle_lemlist_suit_le_seul_registre_gratuit(calls, mode):
    calls["modes"] = {"lemlist_database": mode, "apollo_enrich_organization": "org"}
    out = _run("waterfall_company", company_name="Acme", domain="acme.fr", dry_run=True)
    assert [a["provider"] for a in out["attempts"]][:3] == ["fr", "lemlist", "apollo"]


def test_la_page_linkedin_par_lemlist_avant_apify(calls):
    """Le registre trouve la société sans page LinkedIn : la base de lemlist la complète,
    aucune exécution Apify."""
    calls["answers"]["fr_get"] = IssueCible(ok=True, result={"identity": {
        "siren": "123456789", "nom_complet": "ACME", "site_internet": "https://acme.fr"}})
    calls["answers"]["lemlist_database"] = _societe()
    out = _run("waterfall_company", siren="123456789", domain="acme.fr")
    assert out["provider"] == "fr"
    assert out["company"]["siren"] == "123456789"
    assert out["company"]["linkedin_url"] == "https://www.linkedin.com/company/acme"
    assert _made(calls) == ["fr_get", "lemlist_database"]
    assert out["attempts"][-1]["provider"] == "lemlist_linkedin"


def test_la_completion_lemlist_ne_repose_pas_la_question_deja_posee(calls):
    """lemlist a déjà répondu (rien en base) : sa complétion ne le rappelle pas, Apify
    cherche la page."""
    calls["answers"]["lemlist_database"] = _VIDE
    calls["answers"]["apollo_enrich_organization"] = IssueCible(ok=True, result={
        "organization": {"name": "Acme", "primary_domain": "acme.fr"}})
    calls["answers"]["apify_run_sync"] = IssueCible(ok=True, result=[
        {"domain": "acme.fr", "linkedIns": ["https://www.linkedin.com/company/acme"]}])
    out = _run("waterfall_company", domain="acme.fr",
               providers=["lemlist", "apollo", "lemlist_linkedin", "linkedin"])
    assert _made(calls) == ["lemlist_database", "apollo_enrich_organization",
                            "apify_run_sync"]
    assert out["company"]["linkedin_source"] == "website"


# --- pile technique ----------------------------------------------------------------

def test_tech_lemlist_d_abord_sans_les_detections_potentielles(calls):
    calls["answers"]["lemlist_database"] = _societe()
    out = _run("waterfall_tech", domain="acme.fr")
    assert out["provider"] == "lemlist" and _made(calls) == ["lemlist_database"]
    assert out["tech"]["technologies"] == ["HubSpot", "Salesforce"]
    assert out["tech"]["source"] == "vendor_database"


def test_tech_une_detection_potentielle_ne_repond_pas_a_la_question(calls):
    calls["answers"]["lemlist_database"] = _societe()
    out = _run("waterfall_tech", domain="acme.fr", technologies="Shopify",
               providers=["lemlist", "dataforseo"])
    assert out["found"] is False
    assert _made(calls) == ["lemlist_database", "dataforseo"]

    calls["made"].clear()
    out = _run("waterfall_tech", domain="acme.fr", technologies="salesforce")
    assert out["provider"] == "lemlist" and out["tech"]["matched"] == ["salesforce"]


# --- personne ---------------------------------------------------------------------

def _personne(slug="jane-doe-acme", name="Jane Doe", employer="Acme", experiences=None,
              headline="CEO @Acme"):
    return {"full_name": name, "linkedin_short": slug,
            "lead_linkedin_url": f"https://www.linkedin.com/in/{slug}",
            "headline": headline, "seniority": "Executive Leadership",
            "department": "Operations", "location": "Paris, Ile-de-France, France",
            "country": "France", "potential_email": "jane.doe@acme.fr",
            "current_exp_company_name": employer,
            "experiences": experiences if experiences is not None else [
                {"company_name": "Acme", "title": "Chief Executive Officer",
                 "company_domain": "acme.fr", "order_in_profile": 1,
                 "company_linkedin_url": "https://www.linkedin.com/company/acme"},
                {"company_name": "Acme", "title": "COO", "date_to": "February 2019",
                 "order_in_profile": 2},
                {"company_name": "Other Fund", "title": "Investor", "order_in_profile": 3}]}


def _personnes(*recs):
    return IssueCible(ok=True, result={"results": list(recs), "total": len(recs),
                                       "page": 1, "size": 1})


def test_personne_par_le_slug_linkedin_poste_actuel_chez_l_employeur(calls):
    calls["answers"]["lemlist_database"] = _personnes(_personne(slug="jane-doç"))
    out = _run("waterfall_person", linkedin_url="https://www.linkedin.com/in/jane-do%C3%A7/",
               providers=["lemlist_db"])
    assert out["provider"] == "lemlist_db"
    p = out["person"]
    # Le poste actuel chez l'employeur actuel — pas l'investissement en cours ailleurs.
    assert p["title"] == "Chief Executive Officer" and p["company_name"] == "Acme"
    assert p["company_domain"] == "acme.fr" and p["seniority"] == "Executive Leadership"
    assert p["email"] == "jane.doe@acme.fr" and p["email_status"] == "guessed"
    args = calls["made"][0][1]
    assert args["op"] == "people" and args["size"] == 1
    assert args["filters"] == [{"filterId": "leadLinkedInSlug", "in": ["jane-doç"],
                                "out": []}]


def test_personne_par_nom_et_site_l_homonyme_ecarte(calls):
    calls["answers"]["lemlist_database"] = _personnes(
        _personne(name="Janet Doe", slug="janet"), _personne())
    out = _run("waterfall_person", full_name="Jane Doe", domain="acme.fr",
               providers=["lemlist_db"])
    assert out["person"]["linkedin_url"] == "https://www.linkedin.com/in/jane-doe-acme"
    assert calls["made"][0][1]["filters"] == [
        {"filterId": "username", "in": ["Jane Doe"], "out": []},
        {"filterId": "currentCompanyWebsiteUrl", "in": ["acme.fr"], "out": []}]


def test_personne_poste_actuel_absent_de_la_liste_le_headline_titre(calls):
    """Sans poste actuel chez l'employeur (un profil pas à jour), le headline titre la
    personne."""
    rec = _personne(experiences=[{"company_name": "Acme", "title": "COO",
                                  "date_to": "February 2019"}])
    calls["answers"]["lemlist_database"] = _personnes(rec)
    out = _run("waterfall_person", linkedin_url="https://www.linkedin.com/in/jane-doe-acme",
               providers=["lemlist_db"])
    assert out["person"]["title"] == "CEO @Acme" and out["person"]["company_name"] == "Acme"


def test_personne_un_autre_profil_ne_compte_pas(calls):
    calls["answers"]["lemlist_database"] = _personnes(_personne(slug="someone-else"))
    out = _run("waterfall_person", linkedin_url="https://www.linkedin.com/in/jane-doe-acme",
               providers=["lemlist_db"])
    assert out["found"] is False


@pytest.mark.parametrize("mode", ["org", "user", "group"])
def test_personne_sur_sa_cle_lemlist_suit_son_propre_linkedin(calls, mode):
    calls["modes"] = {"lemlist_database": mode, "apollo_match_person": "org"}
    out = _run("waterfall_person", linkedin_url="https://www.linkedin.com/in/jane",
               dry_run=True)
    assert [a["provider"] for a in out["attempts"]][:3] == ["linkedin", "lemlist_db",
                                                            "apollo"]


def test_personne_lemlist_avant_les_profils_payants_sur_la_cle_partagee(calls):
    out = _run("waterfall_person", linkedin_url="https://www.linkedin.com/in/jane",
               dry_run=True)
    # Pipecorn next in the table — skipped there, the caller's own key only.
    assert [a["provider"] for a in out["attempts"]][:4] == ["linkedin", "lemlist_db",
                                                            "pipecorn", "aiark"]


def test_personne_le_poste_d_ordre_zero_passe_en_premier(calls):
    """`order_in_profile` 0 est le premier poste du profil, pas un « sans ordre » : le
    même choix que les gens d'une société (`fournisseurs._lemlist_lead`)."""
    rec = _personne(experiences=[
        {"company_name": "Acme", "title": "Board Member", "order_in_profile": 1},
        {"company_name": "Acme", "title": "Chief Executive Officer", "order_in_profile": 0}])
    calls["answers"]["lemlist_database"] = _personnes(rec)
    out = _run("waterfall_person", linkedin_url="https://www.linkedin.com/in/jane-doe-acme",
               providers=["lemlist_db"])
    assert out["person"]["title"] == "Chief Executive Officer"
