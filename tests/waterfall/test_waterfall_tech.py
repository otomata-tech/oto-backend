"""La table pile technique (`waterfall/tech.py`) — ce que le SITE fait tourner (l'index
DataForSEO, puis un crawl), puis ce que les OFFRES D'EMPLOI nomment (TheirStack) ; avec
`technologies`, une source ne compte que si elle en détecte une."""
import pytest

from oto_mcp.mcp_errors import McpError
from oto_mcp.tools.meta import IssueCible
from oto_mcp.waterfall import fournisseurs

from _waterfall_faux import _made, _run, calls  # noqa: F401 — la fixture des fournisseurs


# --- pile technique (`waterfall_tech`) ---------------------------------------------
# Réponses réelles du 20/10/2026, anonymisées : le même domaine vu par l'index DataForSEO
# (6 noms), un crawl Apify (7) et les offres d'emploi TheirStack (~100 slugs).

# DataForSEO answers its envelope (`dataforseo(op="domain_technologies")`, served from its
# description): the site's record is `tasks[0].result[0]`.
_DATAFORSEO_TECH = {"cost": 0.012, "tasks": [{"status_code": 20000, "result": [{
    "domain": "www.acme.com", "technologies": {
        "web_development": {"javascript_frameworks": ["Next.js", "React"],
                            "web_frameworks": ["Next.js"]},
        "servers": {"paas": ["Vercel"]}, "content": {"cms": ["Storyblok"]}}}]}]}
_APIFY_TECH = [{"url": "https://acme.com/", "statusCode": 200, "technologyCount": 2,
                "technologies": [{"name": "Storyblok", "version": None, "categories": ["CMS"]},
                                 {"name": "Vercel", "version": None, "categories": ["PaaS"]}],
                "byCategory": {"CMS": ["Storyblok"], "PaaS": ["Vercel"]}, "error": None}]
_THEIRSTACK_TECH = {"metadata": {}, "data": [{
    "name": "Acme", "domain": "acme.com", "employee_count": 277,
    "technology_names": ["hubspot", "salesforce", "next-js", "spendesk1", "snowflake"]}]}


def test_tech_l_index_du_site_d_abord_et_par_categorie(calls):
    calls["answers"]["dataforseo"] = IssueCible(ok=True, result=_DATAFORSEO_TECH)
    out = _run("waterfall_tech", domain="https://www.acme.com/")
    # lemlist d'abord (aucun crédit) : rien en base ici, l'index du site prend le relais.
    assert out["provider"] == "dataforseo"
    assert _made(calls) == ["lemlist_database", "dataforseo"]
    assert calls["made"][1][1] == {"op": "domain_technologies", "target": "acme.com"}
    t = out["tech"]
    assert t["source"] == "website" and t["technologies"] == ["Next.js", "React", "Vercel",
                                                               "Storyblok"]
    assert t["by_category"]["cms"] == ["Storyblok"] and t["count"] == 4


def test_tech_un_site_absent_de_l_index_passe_au_crawl(calls):
    calls["answers"]["dataforseo"] = IssueCible(ok=True, result={
        "cost": 0.01, "tasks": [{"result": None}]})
    calls["answers"]["apify_run_sync"] = IssueCible(ok=True, result=_APIFY_TECH)
    out = _run("waterfall_tech", domain="acme.com")
    assert out["provider"] == "apify" and out["tech"]["technologies"] == ["Storyblok", "Vercel"]
    args = dict(calls["made"])["apify_run_sync"]
    # Une URL, un site facturé au plus ; pas de `max_items` (plafond sous le minimum d'Apify).
    assert args["run_input"]["urls"] == ["acme.com"] and "max_items" not in args
    assert args["timeout_secs"] == fournisseurs._APIFY_TIMEOUT_S


def test_tech_un_site_injoignable_n_est_pas_un_trouve(calls):
    calls["answers"]["apify_run_sync"] = IssueCible(ok=True, result=[
        {"url": "https://acme.com/", "technologies": [], "error": "timeout"}])
    out = _run("waterfall_tech", domain="acme.com", providers=["apify"])
    assert out["found"] is False and out["attempts"][0]["status"] == "no_match"


def test_tech_cherchee_ne_stoppe_que_si_detectee(calls):
    """« Utilise-t-elle HubSpot ? » : le site ne le voit pas, la cascade continue ;
    les offres d'emploi le nomment en slug, comparé sans ponctuation."""
    calls["answers"]["dataforseo"] = IssueCible(ok=True, result=_DATAFORSEO_TECH)
    calls["answers"]["apify_run_sync"] = IssueCible(ok=True, result=_APIFY_TECH)
    calls["answers"]["theirstack_companies_search"] = IssueCible(ok=True, result=_THEIRSTACK_TECH)
    out = _run("waterfall_tech", domain="acme.com", technologies="HubSpot, Spendesk")
    assert out["provider"] == "theirstack" and out["tech"]["source"] == "job_postings"
    assert out["tech"]["matched"] == ["HubSpot", "Spendesk"]
    # Pipecorn comes last: TheirStack's find stops the cascade before it.
    assert [a["status"] for a in out["attempts"]] == ["no_match"] * 5 + ["found"]

    calls["made"].clear()
    out = _run("waterfall_tech", domain="acme.com", technologies=["nextjs"])
    assert out["provider"] == "dataforseo" and out["tech"]["matched"] == ["nextjs"]


def test_tech_jamais_detectee_rend_un_non_trouve(calls):
    calls["answers"]["dataforseo"] = IssueCible(ok=True, result=_DATAFORSEO_TECH)
    calls["answers"]["apify_run_sync"] = IssueCible(ok=True, result=_APIFY_TECH)
    calls["answers"]["theirstack_companies_search"] = IssueCible(ok=True, result=_THEIRSTACK_TECH)
    out = _run("waterfall_tech", domain="acme.com", technologies=["Shopify"])
    assert out["found"] is False and len(out["attempts"]) == 7


def test_tech_par_nom_seulement_theirstack_et_l_homonyme_ecarte(calls):
    calls["answers"]["theirstack_companies_search"] = IssueCible(ok=True, result={"data": [
        {"name": "Acmeville Bakery", "domain": "acmeville.fr", "technology_names": ["wix"]},
        *_THEIRSTACK_TECH["data"]]})
    out = _run("waterfall_tech", company_name="acme")
    assert _made(calls) == ["theirstack_companies_search"]
    assert calls["made"][0][1]["extra"] == {"company_name_case_insensitive_or": ["acme"]}
    assert out["tech"]["technologies"][0] == "hubspot"

    calls["made"].clear()
    _run("waterfall_tech", domain="acme.com", providers=["theirstack"])
    assert calls["made"][0][1] == {"extra": {"company_domain_or": ["acme.com"]}, "limit": 1}


def test_tech_sans_domaine_ni_nom_est_refuse(calls):
    with pytest.raises(McpError):
        _run("waterfall_tech", technologies=["Shopify"])


# Fiches société réelles du 20/10/2026 (~100 technologies chacune), anonymisées et réduites.
_APOLLO_TECH = {"organization": {"name": "Acme", "primary_domain": "acme.com",
    "technology_names": ["Hubspot", "Salesforce", "Snowflake", "Next.js"],
    "current_technologies": [
        {"uid": "hubspot", "name": "Hubspot", "category": "Marketing Automation"},
        {"uid": "salesforce", "name": "Salesforce", "category": "Customer Relationship Management"},
        {"uid": "snowflake", "name": "Snowflake", "category": "Database"},
        {"uid": "nextjs", "name": "Next.js", "category": "Frameworks and Programming Languages"}]}}
_AIARK_TECH = {"content": [{"summary": {"name": "Acme"},
    "link": {"website": "https://www.acme.com", "domain": "acme.com"},
    "technologies": [{"name": "google analytics"}, {"name": "Google Analytics",
                      "category": "Analytics and Tracking"},
                     {"name": "Lever", "category": "Recruitment"},
                     {"name": "Salesforce", "category": "Customer Relationship Management"}]}]}


def test_tech_apollo_rend_outils_metier_et_categories(calls):
    calls["answers"]["apollo_enrich_organization"] = IssueCible(ok=True, result=_APOLLO_TECH)
    out = _run("waterfall_tech", domain="acme.com", providers=["apollo"], technologies=["salesforce"])
    t = out["tech"]
    assert out["provider"] == "apollo" and t["source"] == "vendor_database"
    assert t["matched"] == ["salesforce"] and t["by_category"]["Database"] == ["Snowflake"]
    assert calls["made"][0][1] == {"domain": "acme.com"}


def test_tech_apollo_d_un_autre_domaine_ne_compte_pas(calls):
    other = {"organization": {**_APOLLO_TECH["organization"], "primary_domain": "acme.de"}}
    calls["answers"]["apollo_enrich_organization"] = IssueCible(ok=True, result=other)
    out = _run("waterfall_tech", domain="acme.com", providers=["apollo"])
    assert out["found"] is False


def test_tech_aiark_une_fiche_par_domaine_et_doublons_de_casse_fusionnes(calls):
    calls["answers"]["linkedin_aiark_search"] = IssueCible(ok=True, result=_AIARK_TECH)
    out = _run("waterfall_tech", domain="acme.com", providers=["aiark"])
    args = calls["made"][0][1]
    assert args["size"] == 1 and args["full"] is True and "technologies" in args["fields"]
    t = out["tech"]
    assert t["technologies"] == ["google analytics", "Lever", "Salesforce"] and t["count"] == 3
    assert t["by_category"]["Recruitment"] == ["Lever"]
