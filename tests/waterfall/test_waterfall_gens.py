"""Les gens d'une société (`waterfall/gens.py`, `vivier.py`, `qualification.py`) — des
noms masqués ne stoppent pas la cascade ; une personne n'est gardée que si sa société
ACTUELLE est celle demandée ; Jev juge ce qui a été trouvé (jamais un nom ni une URL), et
un `qualify` que Jev ne peut pas servir est refusé AVANT toute dépense ; la recherche
gratuite est lue page après page, une liste payée achète ses pages suivantes jusqu'à
`limit` retenus."""
import pytest
from mcp.types import ErrorData, INVALID_PARAMS

from oto_mcp.mcp_errors import McpError
from oto_mcp.tools.meta import IssueCible
from oto_mcp.waterfall import vivier

from _waterfall_faux import (_FIT, _gens, _made, _run, _verdicts,  # noqa: F401
                             calls)


# --- les gens d'une société (`waterfall_people`) ----------------------------------

def test_people_apollo_garde_les_roles_et_l_id_de_revelation(calls):
    calls["answers"]["apollo_search_people"] = IssueCible(ok=True, result={"people": [
        {"id": "a1", "first_name": "Jane", "last_name_obfuscated": "Do***e", "title": "VP Sales"},
        {"id": "a2", "first_name": "Bob", "last_name_obfuscated": "Sm***h", "title": "Engineer"}]})
    out = _run("waterfall_people", domain="acme.com", roles="sales, CEO", country="France")
    assert out["provider"] == "apollo" and out["people"]["count"] == 1
    assert out["people"]["people"][0] == {"full_name": "Jane Do***e", "title": "VP Sales",
                                          "apollo_id": "a1", "name_obfuscated": True}
    assert calls["made"][0][1] == {"domains": ["acme.com"], "titles": ["sales", "CEO"],
                                   "person_locations": ["France"], "per_page": 20}


def test_people_employes_linkedin_ecarte_ceux_d_ailleurs(calls):
    calls["answers"]["apify_run_sync"] = IssueCible(ok=True, result=[
        {"firstName": "Li", "lastName": "Wei", "linkedinUrl": "https://linkedin.com/in/liwei",
         "currentPosition": [{"companyUniversalName": "acme-printers", "position": "VP of B2B BU"}]},
        {"firstName": "Fake", "lastName": "Profile", "linkedinUrl": "https://linkedin.com/in/fake",
         "currentPosition": [{"companyUniversalName": "someone-else", "position": "CEO"}]}])
    out = _run("waterfall_people", company_linkedin_url="https://www.linkedin.com/company/acme-printers/",
               providers=["linkedin_employees"])
    assert [p["full_name"] for p in out["people"]["people"]] == ["Li Wei"]
    assert out["people"]["people"][0]["title"] == "VP of B2B BU"
    assert calls["made"][0][1]["run_input"]["companies"] == ["https://www.linkedin.com/company/acme-printers/"]


def test_people_google_lit_le_titre_et_exige_la_societe(calls):
    calls["answers"]["serper_search"] = IssueCible(ok=True, result={"organic": [
        {"title": "Zhang San - VP of Overseas Business - Acme Additive | LinkedIn",
         "link": "https://cn.linkedin.com/in/zhangsan?trk=x", "snippet": ""},
        {"title": "John Roe - Director - Other Corp | LinkedIn",
         "link": "https://linkedin.com/in/johnroe", "snippet": ""}]})
    out = _run("waterfall_people", company_name="Acme Additive", providers=["google"])
    assert out["people"]["people"] == [{"full_name": "Zhang San", "title": "VP of Overseas Business",
                                        "linkedin_url": "https://cn.linkedin.com/in/zhangsan"}]
    assert calls["made"][0][1]["site_filter"] == "linkedin.com/in"


def test_people_page_linkedin_malformee_refusee(calls):
    with pytest.raises(McpError, match="LinkedIn company page"):
        _run("waterfall_people", company_linkedin_url="https://example.com/acme")


def test_des_noms_masques_ne_stoppent_pas_la_cascade_mais_restent_le_repli(calls):
    calls["answers"]["apollo_search_people"] = IssueCible(ok=True, result={"people": [
        {"id": "a1", "first_name": "Jane", "last_name_obfuscated": "Do***e", "title": "VP Sales"}]})
    calls["answers"]["serper_search"] = IssueCible(ok=True, result={"organic": [
        {"title": "Jane Doe - VP Sales - Acme | LinkedIn",
         "link": "https://linkedin.com/in/janedoe", "snippet": ""}]})
    out = _run("waterfall_people", domain="acme.com", company_name="Acme",
               providers=["apollo", "google"])
    assert out["provider"] == "google"
    assert out["people"]["people"][0]["full_name"] == "Jane Doe"
    assert [a["status"] for a in out["attempts"]] == ["weak", "found"]

    calls["made"].clear()
    calls["answers"]["serper_search"] = IssueCible(ok=True, result={"organic": []})
    out = _run("waterfall_people", domain="acme.com", company_name="Acme",
               providers=["apollo", "google"])
    # Rien de mieux : les noms masqués d'Apollo sont la réponse.
    assert out["found"] is True and out["provider"] == "apollo"
    assert out["people"]["people"][0]["apollo_id"] == "a1"


def test_on_ne_demande_pas_plus_que_ce_que_l_etape_garde(calls):
    _run("waterfall_people", domain="acme.com", providers=["aiark"], limit=7)
    assert calls["made"][0][1]["size"] == 7
    calls["made"].clear()
    _run("waterfall_company", domain="acme.com", providers=["theirstack", "aiark"])
    args = dict(calls["made"])
    assert args["theirstack_companies_search"]["limit"] == 1
    assert args["linkedin_aiark_search"]["size"] == 1


def test_reel_google_lit_le_sous_titre_et_ecarte_la_simple_mention(calls):
    calls["answers"]["serper_search"] = IssueCible(ok=True, result={"organic": [
        {"title": "Dr. Ulla Jay – Acme Marking GmbH",
         "subtitle": "Paderborn, Deutschland · Acme Marking GmbH",
         "link": "https://de.linkedin.com/in/ulla-jay-1"},
        {"title": "Phil Bee – TRACEABLE AND ...",
         "subtitle": "Paderborn, Deutschland · CTO - Acme Marking · Acme Marking GmbH",
         "link": "https://de.linkedin.com/in/phil-bee-2"},
        {"title": "Dr Paul Roe - Senior Project Manager", "link": "https://uk.linkedin.com/in/paul-roe",
         "snippet": "... Acme Marking Techniques Deposition PECVD ..."}]})
    out = _run("waterfall_people", company_name="Acme Marking", providers=["google"])
    people = out["people"]["people"]
    # Le CTO d'abord (séniorité), la simple mention dans un extrait écartée.
    assert [p["full_name"] for p in people] == ["Phil Bee", "Dr. Ulla Jay"]
    assert people[0]["title"] == "CTO - Acme Marking" and people[1].get("title") is None


def test_reel_apollo_people_dit_qui_a_un_email(calls):
    calls["answers"]["apollo_search_people"] = IssueCible(ok=True, result={"people": [
        {"id": "a2", "first_name": "Dev", "last_name_obfuscated": "Pi***k", "title": "Software Developer",
         "has_email": True, "has_direct_phone": "Maybe: please request direct dial via people/bulk_match"},
        {"id": "a1", "first_name": "Ulla", "last_name_obfuscated": "Ja***y", "title": "CEO, Co-Founder",
         "has_email": True, "has_direct_phone": "Yes"}]})
    out = _run("waterfall_people", domain="acme.de", providers=["apollo"])
    first = out["people"]["people"][0]
    assert first["apollo_id"] == "a1" and first["has_email"] is True and first["has_direct_phone"] is True


# --- qualification par Jev (optionnelle) ----------------------------------------------

def test_qualifier_annote_chaque_personne_sans_rien_ecarter_par_defaut(calls):
    _gens(calls)
    calls["answers"]["jev_items"] = _verdicts(0.95, 0.10)
    out = _run("waterfall_people", company_name="Acme", providers=["google"], qualify=_FIT)
    people = out["people"]["people"]
    assert [p["fit"]["fit"] for p in people] == [0.95, 0.10]   # vue resserrée par défaut
    assert out["qualification"]["status"] == "done" and out["qualification"]["model"] == "jev-2026-09"
    # Seuls les champs d'un jugement partent chez Jev : jamais un nom ni une URL.
    sent = dict(calls["made"])["jev_items"]["items"]
    assert sent[0]["state"] == {"title": "CEO", "location": "Lyon", "company": "Acme"}


def test_qualify_min_ecarte_les_personnes_sous_le_seuil(calls):
    _gens(calls)
    calls["answers"]["jev_items"] = _verdicts(0.95, 0.10)
    out = _run("waterfall_people", company_name="Acme", providers=["google"],
               qualify=_FIT, qualify_min=0.7)
    assert [p["full_name"] for p in out["people"]["people"]] == ["Ann Boss"]
    assert out["people"]["count"] == 1 and out["qualification"]["not_qualified"] == 1
    assert out["attempts"][0]["qualified"] == 1 and out["attempts"][0]["not_qualified"] == 1
    # Les écartés reviennent toujours, en compact : vérifier la rubrique ne repaie rien.
    assert out["unqualified"] == [{"full_name": "Bob Dev", "title": "Developer",
                                   "linkedin_url": "https://fr.linkedin.com/in/bob-dev",
                                   "answers": {"fit": 0.1}}]


@pytest.mark.parametrize("mode", ["forbidden", "over_quota", "org", "user"])
@pytest.mark.parametrize("ns,kw", [("waterfall_people", {"company_name": "Acme"}),
                                   ("waterfall_company", {"domain": "acme.com"})])
def test_sans_jev_utilisable_qualify_est_refuse_avant_toute_depense(calls, mode, ns, kw):
    """Des résultats non jugés se liraient comme jugés (et le mode tableau les écrirait
    `found`) : `qualify` demandé alors que Jev ne peut pas tourner — pas de clé partagée,
    ou une clé plus proche qui la masque — est refusé nommément, rien n'est appelé."""
    _gens(calls)
    calls["modes"] = {"jev_items": mode}
    with pytest.raises(McpError) as e:
        _run(ns, qualify=_FIT, **kw)
    assert e.value.error.data["code"] == "qualification_unavailable"
    assert calls["made"] == []


def test_jev_qui_refuse_a_l_appel_rend_les_resultats_non_qualifies(calls):
    _gens(calls)
    calls["answers"]["jev_items"] = McpError(ErrorData(code=INVALID_PARAMS,
                                                       message="No `jev` key for your org"))
    out = _run("waterfall_people", company_name="Acme", providers=["google"],
               qualify=_FIT, qualify_min=0.7)
    assert out["found"] is True and out["people"]["count"] == 2
    assert out["qualification"]["status"] == "skipped"


def test_une_societe_qualifiee_reste_trouvee_et_dit_si_elle_passe(calls):
    calls["answers"]["linkedin_aiark_search"] = IssueCible(ok=True, result={"content": [{
        "summary": {"name": "Acme", "industry": "machinery"}, "link": {"domain": "acme.com"}}]})
    calls["answers"]["jev_items"] = _verdicts(0.2)
    out = _run("waterfall_company", domain="acme.com", providers=["aiark"],
               qualify=_FIT, qualify_min=0.7)
    assert out["found"] is True and out["company"]["qualification"]["passed"] is False
    assert dict(calls["made"])["jev_items"]["items"][0]["state"]["industry"] == "machinery"


def test_pas_de_qualification_sans_resultat_ni_en_dry_run(calls):
    out = _run("waterfall_people", company_name="Acme", providers=["google"], qualify=_FIT)
    assert out["found"] is False and "jev_items" not in _made(calls)
    out = _run("waterfall_people", company_name="Acme", providers=["google"], qualify=_FIT,
               dry_run=True)
    assert out["qualification"]["status"] == "planned"


@pytest.mark.parametrize("kwargs", [{"qualify_min": 0.7}, {"qualify": {}},
                                    {"qualify": _FIT, "qualify_min": 1.5},
                                    {"qualify": {"x": {"type": "score"}}, "qualify_min": 0.5}])
def test_une_rubrique_mal_formee_est_refusee(calls, kwargs):
    with pytest.raises(McpError):
        _run("waterfall_people", company_name="Acme", **kwargs)


def test_si_personne_ne_passe_le_fournisseur_suivant_est_essaye(calls):
    _gens(calls)
    calls["answers"]["linkedin_aiark_search"] = IssueCible(ok=True, result={"content": [
        {"profile": {"full_name": "Cleo Head", "title": "Head of Marketing"},
         "link": {"linkedin": "https://www.linkedin.com/in/cleo-head"}}]})
    calls["answers"]["jev_items"] = [_verdicts(0.2, 0.1), _verdicts(0.9)]
    out = _run("waterfall_people", company_name="Acme", domain="acme.com",
               providers=["google", "aiark"], qualify=_FIT, qualify_min=0.7)
    assert out["provider"] == "aiark"
    assert [p["full_name"] for p in out["people"]["people"]] == ["Cleo Head"]
    assert out["attempts"][0]["status"] == "unqualified"
    assert out["attempts"][0]["not_qualified"] == 2
    q = out["qualification"]
    assert q["calls"] == 2 and q["not_qualified"] == 2


def test_show_unqualified_rend_les_ecartes_avec_leurs_reponses(calls):
    _gens(calls)
    calls["answers"]["jev_items"] = _verdicts(0.2, 0.1)
    out = _run("waterfall_people", company_name="Acme", providers=["google"],
               qualify=_FIT, qualify_min=0.7, show_unqualified=True)
    assert out["found"] is False
    assert [p["full_name"] for p in out["unqualified"]] == ["Ann Boss", "Bob Dev"]
    assert out["unqualified"][0]["qualification"]["answers"]["fit"]["noul"] == 0.2
    assert out["unqualified"][0]["location"] == "Lyon"     # the full record


def test_un_ecarte_retrouve_par_le_fournisseur_suivant_n_est_pas_rejuge(calls):
    _gens(calls)
    calls["answers"]["linkedin_aiark_search"] = IssueCible(ok=True, result={"content": [
        {"profile": {"full_name": "Bob Dev", "title": "Developer"},
         "link": {"linkedin": "https://fr.linkedin.com/in/bob-dev/"}}]})
    calls["answers"]["jev_items"] = _verdicts(0.2, 0.1)
    out = _run("waterfall_people", company_name="Acme", domain="acme.com",
               providers=["google", "aiark"], qualify=_FIT, qualify_min=0.7)
    assert _made(calls).count("jev_items") == 1
    assert out["found"] is False and out["qualification"]["not_qualified"] == 2
    assert out["attempts"][1]["status"] == "unqualified"


def test_moins_de_qualifies_que_demande_s_arrete_et_propose_la_suite(calls):
    _gens(calls)
    calls["answers"]["jev_items"] = _verdicts(0.95, 0.10)
    out = _run("waterfall_people", company_name="Acme", domain="acme.com", limit=5,
               providers=["google", "aiark"], qualify=_FIT, qualify_min=0.7)
    assert out["provider"] == "google" and "linkedin_aiark_search" not in _made(calls)
    assert out["continue_with"]["providers"] == ["aiark"]
    assert out["continue_with"]["short_of_limit"] == 4


def test_reel_recherche_apollo_gratuite_jugee_puis_seuls_les_retenus_reveles(calls):
    """Le cas d'une grande enseigne (19/10/2026) : 239 personnes à la recherche gratuite,
    titres visibles, noms masqués — Jev juge les titres, on ne révèle que les meilleurs
    qui passent ; AI Ark (payé au profil) n'est jamais appelé."""
    calls["answers"]["apollo_search_people"] = IssueCible(ok=True, result={"people": [
        {"id": "p1", "first_name": "Bo", "last_name_obfuscated": "D***c",
         "title": "Directeur Marketing, Communication, Omnicommerce"},
        {"id": "p2", "first_name": "Ba", "last_name_obfuscated": "V***n",
         "title": "Customer Experience Leader"},
        {"id": "p3", "first_name": "Ev", "last_name_obfuscated": "E***t",
         "title": "Responsable des Événements de Communication"}]})
    calls["answers"]["jev_items"] = _verdicts(0.82, 0.78, 0.22)
    calls["answers"]["apollo_match_person"] = [
        IssueCible(ok=True, result={"person": {"name": "Bo Dee", "title": "Directeur Marketing",
                                               "linkedin_url": "http://www.linkedin.com/in/bo-dee",
                                               "email": "bo@acme.fr", "email_status": "verified"}}),
        IssueCible(ok=True, result={"person": {"name": "Ba Vee", "title": "Customer Experience Leader",
                                               "linkedin_url": "http://www.linkedin.com/in/ba-vee",
                                               "email": "ba@acme.fr", "email_status": "verified"}})]
    out = _run("waterfall_people", domain="acme.fr", limit=2, qualify=_FIT, qualify_min=0.7,
               providers=["apollo", "aiark"])
    made = _made(calls)
    assert made.count("apollo_match_person") == 2 and "linkedin_aiark_search" not in made
    assert made.count("jev_items") == 1
    people = out["people"]["people"]
    assert [p["full_name"] for p in people] == ["Bo Dee", "Ba Vee"]
    assert people[0]["email"] == "bo@acme.fr" and people[0]["fit"] == {"fit": 0.82}
    a = out["attempts"][0]
    assert a["status"] == "found" and a["revealed"] == 2 and a["reveals_paid"] == 2
    assert out["qualification"]["not_qualified"] == 1
    # Les révélations sont payées en série, par id — jamais par nom.
    assert [args for n, args in calls["made"] if n == "apollo_match_person"] == [
        {"person_id": "p1"}, {"person_id": "p2"}]


def test_sans_rubrique_les_noms_masques_restent_un_repli(calls):
    calls["answers"]["apollo_search_people"] = IssueCible(ok=True, result={"people": [
        {"id": "p1", "first_name": "Bo", "last_name_obfuscated": "D***c", "title": "CMO"}]})
    out = _run("waterfall_people", domain="acme.fr", providers=["apollo"])
    assert "apollo_match_person" not in _made(calls)
    assert out["attempts"][0]["status"] == "weak" and out["found"] is True


def test_max_paid_steps_borne_aussi_les_revelations(calls):
    calls["answers"]["apollo_search_people"] = IssueCible(ok=True, result={"people": [
        {"id": f"p{i}", "first_name": f"X{i}", "last_name_obfuscated": "Y***", "title": "CMO"}
        for i in range(3)]})
    calls["answers"]["jev_items"] = _verdicts(0.9, 0.9, 0.9)
    calls["answers"]["apollo_match_person"] = IssueCible(ok=True, result={"person": {
        "name": "X Y", "linkedin_url": "http://www.linkedin.com/in/x"}})
    out = _run("waterfall_people", domain="acme.fr", limit=3, qualify=_FIT,
               providers=["apollo"], max_paid_steps=2)
    assert _made(calls).count("apollo_match_person") == 2
    assert out["attempts"][0]["stopped"] == "max_paid_steps"


def _page(start, n, total):
    return IssueCible(ok=True, result={"total_entries": total, "people": [
        {"id": f"p{i}", "first_name": f"P{i}", "last_name_obfuscated": "X***",
         "title": "CMO" if i == start else "Developer"} for i in range(start, start + n)]})


def test_la_recherche_gratuite_est_lue_page_apres_page_jusqu_au_bout(calls):
    calls["answers"]["apollo_search_people"] = [_page(0, 100, 230), _page(100, 100, 230),
                                                _page(200, 30, 230)]
    calls["answers"]["jev_items"] = _verdicts(*([0.9] + [0.1] * 199))
    calls["answers"]["apollo_match_person"] = IssueCible(ok=True, result={"person": {
        "name": "P0 Y", "linkedin_url": "http://www.linkedin.com/in/p0"}})
    out = _run("waterfall_people", domain="acme.fr", limit=1, qualify=_FIT, qualify_min=0.7,
               providers=["apollo"])
    pages = [a.get("page", 1) for n, a in calls["made"] if n == "apollo_search_people"]
    assert pages == [1, 2, 3]
    a = out["attempts"][0]
    assert a["pages"] == 3 and a["total"] == 230 and "pool_truncated" not in a
    # 230 titres = deux appels Jev (200 + 30), jugés ensemble.
    jev = [args for n, args in calls["made"] if n == "jev_items"]
    assert [len(j["items"]) for j in jev] == [200, 30]
    assert out["qualification"]["not_qualified"] >= 199


def test_sans_temps_pour_juger_les_pages_s_arretent_et_le_recu_le_dit(calls, monkeypatch):
    monkeypatch.setattr(vivier, "_PAGES_RESERVE_S", 10_000)
    calls["answers"]["apollo_search_people"] = [_page(0, 100, 500)]
    out = _run("waterfall_people", domain="acme.fr", qualify=_FIT, providers=["apollo"])
    a = out["attempts"][0]
    assert a["pages"] == 1 and a["pool_truncated"] == "time_budget"


def test_sans_rubrique_une_seule_page(calls):
    calls["answers"]["apollo_search_people"] = [_page(0, 20, 500)]
    _run("waterfall_people", domain="acme.fr", providers=["apollo"])
    assert _made(calls).count("apollo_search_people") == 1


def _aiark_page(names_titles):
    return IssueCible(ok=True, result={"content": [
        {"profile": {"full_name": n, "title": t},
         "link": {"linkedin": f"https://www.linkedin.com/in/{n.lower().replace(' ', '-')}"}}
        for n, t in names_titles]})


def test_une_liste_payee_achete_ses_pages_suivantes_jusqu_a_limit_qualifies(calls):
    calls["answers"]["linkedin_aiark_search"] = [
        _aiark_page([("Ann A", "CMO"), ("Bob B", "Developer")]),
        _aiark_page([("Cy C", "Head of Marketing"), ("Di D", "Intern")]),
        _aiark_page([("Ed E", "Never reached")])]
    calls["answers"]["jev_items"] = [_verdicts(0.9, 0.1), _verdicts(0.8, 0.1)]
    out = _run("waterfall_people", domain="acme.fr", limit=2, qualify=_FIT, qualify_min=0.7,
               providers=["aiark"])
    pages = [a.get("page", 0) for n, a in calls["made"] if n == "linkedin_aiark_search"]
    assert pages == [0, 1]                       # page 2 never bought: limit reached
    assert [p["full_name"] for p in out["people"]["people"]] == ["Ann A", "Cy C"]
    a = out["attempts"][0]
    assert a["pages"] == 2 and a["qualified"] == 2 and a["not_qualified"] == 2
    assert "continue_with" not in out


def test_les_pages_payees_comptent_dans_max_paid_steps(calls):
    calls["answers"]["linkedin_aiark_search"] = [
        _aiark_page([("Ann A", "Intern")]), _aiark_page([("Bob B", "Intern")]),
        _aiark_page([("Cy C", "Intern")])]
    calls["answers"]["jev_items"] = _verdicts(0.1)
    out = _run("waterfall_people", domain="acme.fr", limit=3, qualify=_FIT, qualify_min=0.7,
               providers=["aiark"], max_paid_steps=2)
    assert _made(calls).count("linkedin_aiark_search") == 2
    assert out["attempts"][0]["stopped"] == "max_paid_steps"


def test_la_fin_des_resultats_arrete_les_pages(calls):
    calls["answers"]["linkedin_aiark_search"] = [_aiark_page([("Ann A", "Intern")]),
                                                 IssueCible(ok=True, result={"content": []})]
    calls["answers"]["jev_items"] = _verdicts(0.1)
    out = _run("waterfall_people", domain="acme.fr", limit=3, qualify=_FIT, qualify_min=0.7,
               providers=["aiark"])
    assert _made(calls).count("linkedin_aiark_search") == 2
    assert out["attempts"][0]["pages"] == 2 and "stopped" not in out["attempts"][0]


def test_sans_seuil_une_liste_payee_ne_lit_qu_une_page(calls):
    calls["answers"]["linkedin_aiark_search"] = _aiark_page([("Ann A", "CMO")])
    calls["answers"]["jev_items"] = _verdicts(0.1)
    _run("waterfall_people", domain="acme.fr", limit=3, qualify=_FIT, providers=["aiark"])
    assert _made(calls).count("linkedin_aiark_search") == 1



def test_full_rend_chaque_personne_entiere(calls):
    _gens(calls)
    calls["answers"]["jev_items"] = _verdicts(0.95, 0.10)
    out = _run("waterfall_people", company_name="Acme", providers=["google"], qualify=_FIT,
               full=True)
    p = out["people"]["people"][0]
    assert p["qualification"]["answers"]["fit"]["noul"] == 0.95 and "fit" not in p


# --- la base de personnes de lemlist ----------------------------------------------

def _lemlist_rec(i, title, *, domain="acme.fr", ended=False, headline=None, email=True):
    exp = {"company_domain": domain, "title": title, "order_in_profile": 1,
           "company_linkedin_url": f"https://www.linkedin.com/company/{domain.split('.')[0]}"}
    if ended:
        exp["date_to"] = "July 2023"
    return {"_id": str(i), "full_name": f"Person {i}", "headline": headline or title,
            "lead_linkedin_url": f"https://www.linkedin.com/in/person-{i}",
            "location": "Paris, France", "seniority": "Department Leadership",
            "potential_email": f"p{i}@acme.fr" if email else None, "experiences": [exp]}


def _lemlist_page(recs, total=None, page=1):
    return IssueCible(ok=True, result={"results": recs, "total": total or len(recs),
                                       "page": page, "size": len(recs), "limitation": 1999})


def test_lemlist_lit_le_poste_actuel_dans_la_societe_demandee(calls):
    calls["answers"]["lemlist_database"] = _lemlist_page([
        # Toujours en poste ici, malgré un headline qui nomme ailleurs : gardé, titre du poste.
        _lemlist_rec(1, "Head of Sales", headline="Senior Director @ Other Corp"),
        # Les rôles sont le filtre de lemlist (mots dans le désordre, titre normalisé) :
        # pas re-filtrés ici — « Head of Enterprise Sales » reste pour « Head of Sales ».
        _lemlist_rec(2, "Head of Enterprise Sales - Spain"),
        # Parti : écarté.
        _lemlist_rec(3, "Head of Sales", ended=True),
        # En poste ailleurs : écarté.
        _lemlist_rec(4, "Head of Sales", domain="other.com")])
    out = _run("waterfall_people", domain="acme.fr", roles="Head of Sales, director",
               providers=["lemlist"])
    assert out["provider"] == "lemlist"
    people = out["people"]["people"]
    assert [p["full_name"] for p in people] == ["Person 1", "Person 2"]
    assert people[0]["title"] == "Head of Sales"
    assert people[0]["email"] == "p1@acme.fr" and people[0]["email_status"] == "guessed"
    args = calls["made"][0][1]
    assert args["op"] == "people" and args["page"] == 1 and args["size"] == 20
    assert args["filters"] == [
        {"filterId": "currentCompanyWebsiteUrl", "in": ["acme.fr"], "out": []},
        {"filterId": "currentTitle", "in": ["Head of Sales", "director"], "out": []}]
    assert "experiences" not in args["excludes"]


def test_lemlist_par_la_page_linkedin_sans_domaine(calls):
    calls["answers"]["lemlist_database"] = _lemlist_page([_lemlist_rec(1, "CEO")])
    out = _run("waterfall_people",
               company_linkedin_url="https://www.linkedin.com/company/acme/about",
               providers=["lemlist"])
    assert out["people"]["count"] == 1
    assert calls["made"][0][1]["filters"] == [
        {"filterId": "currentCompanyLinkedInUrl",
         "in": ["https://www.linkedin.com/company/acme"], "out": []}]


def test_lemlist_garde_sa_place_apres_apollo(calls):
    out = _run("waterfall_people", domain="acme.fr", dry_run=True)
    keys = [a["provider"] for a in out["attempts"]]
    assert keys[:3] == ["apollo", "lemlist", "google"]


@pytest.mark.parametrize("mode", ["org", "user", "group"])
def test_lemlist_passe_en_tete_sur_la_cle_de_l_appelant(calls, mode):
    """Sa propre clé lemlist (org, perso, équipe) : la base de lemlist passe devant tout,
    Apollo sur sa propre clé compris — des noms complets qui ne facturent rien."""
    calls["modes"] = {"lemlist_database": mode, "apollo_search_people": "org"}
    out = _run("waterfall_people", domain="acme.fr", dry_run=True)
    assert [a["provider"] for a in out["attempts"]][:3] == ["lemlist", "apollo", "google"]


def test_un_ordre_ecrit_garde_lemlist_ou_il_est(calls):
    calls["modes"] = {"lemlist_database": "org"}
    out = _run("waterfall_people", domain="acme.fr", dry_run=True,
               providers=["apollo", "lemlist"])
    assert [a["provider"] for a in out["attempts"]] == ["apollo", "lemlist"]


def test_lemlist_toutes_les_pages_sous_qualify_sans_compter_d_etape_payee(calls):
    """Sous `qualify`, la base de lemlist est lue page après page (à partir de 1, `total`
    lu au premier appel) dans le vivier que Jev juge — sans rien compter dans
    `max_paid_steps` : elle ne facture aucun crédit."""
    recs = [_lemlist_rec(i, "CMO" if i == 0 else "Developer") for i in range(230)]
    calls["answers"]["lemlist_database"] = [
        _lemlist_page(recs[:100], 230, 1), _lemlist_page(recs[100:200], 230, 2),
        _lemlist_page(recs[200:], 230, 3)]
    calls["answers"]["jev_items"] = _verdicts(*([0.9] + [0.1] * 199))
    out = _run("waterfall_people", domain="acme.fr", limit=1, qualify=_FIT,
               qualify_min=0.7, providers=["lemlist", "aiark"], max_paid_steps=1)
    pages = [a["page"] for n, a in calls["made"] if n == "lemlist_database"]
    sizes = {a["size"] for n, a in calls["made"] if n == "lemlist_database"}
    assert pages == [1, 2, 3] and sizes == {100}
    a = out["attempts"][0]
    assert a["pages"] == 3 and a["total"] == 230 and "pool_truncated" not in a
    assert out["provider"] == "lemlist"
    assert [p["full_name"] for p in out["people"]["people"]] == ["Person 0"]
    assert "linkedin_aiark_search" not in _made(calls)


def test_lemlist_une_seule_page_sans_rubrique(calls):
    calls["answers"]["lemlist_database"] = _lemlist_page(
        [_lemlist_rec(i, "CEO") for i in range(20)], total=358)
    _run("waterfall_people", domain="acme.fr", providers=["lemlist"])
    assert _made(calls).count("lemlist_database") == 1


def test_lemlist_ne_compte_pas_dans_max_paid_steps(calls):
    """Une étape `quota_only` qui ne trouve rien laisse le budget d'étapes payées intact :
    la suivante tourne encore sous `max_paid_steps=1`."""
    calls["answers"]["lemlist_database"] = _lemlist_page([])
    _gens(calls)
    out = _run("waterfall_people", company_name="Acme", domain="acme.fr",
               providers=["lemlist", "google"], max_paid_steps=1)
    assert out["provider"] == "google"


def test_lemlist_page_linkedin_sans_slug_refusee_par_son_nom():
    """Une page sans slug n'est jamais cherchée comme `…/company/None` : refus nommé."""
    from oto_mcp.waterfall import gens
    with pytest.raises(McpError) as e:
        gens._lemlist_args({"company_linkedin_url": "https://www.linkedin.com/company/",
                            "limit": 10})
    assert e.value.error.data["code"] == "linkedin_page_without_slug"


@pytest.mark.parametrize("kind", ["school", "showcase"])
def test_lemlist_garde_la_sorte_de_page_linkedin(calls, kind):
    """Une page `school` ou `showcase` reste ce qu'elle est : jamais réécrite en `/company/`."""
    calls["answers"]["lemlist_database"] = _lemlist_page([])
    _run("waterfall_people", company_linkedin_url=f"https://www.linkedin.com/{kind}/hec-paris/",
         providers=["lemlist"])
    assert calls["made"][0][1]["filters"] == [
        {"filterId": "currentCompanyLinkedInUrl",
         "in": [f"https://www.linkedin.com/{kind}/hec-paris"], "out": []}]


def test_lemlist_le_poste_d_ordre_zero_passe_en_premier():
    """`order_in_profile` 0 est le premier poste du profil, pas un « sans ordre »."""
    from oto_mcp.waterfall import gens
    rec = _lemlist_rec(1, "Board Member")
    rec["experiences"].insert(0, {**rec["experiences"][0], "title": "CEO",
                                  "order_in_profile": 0})
    found = gens._lemlist_people({"results": [rec]}, {"domain": "acme.fr", "limit": 10})
    assert found["people"][0]["title"] == "CEO"


def test_lemlist_chaque_page_est_reduite_avant_de_rejoindre_le_vivier(calls, monkeypatch):
    """Sous `qualify`, une fiche lemlist (~60 Ko : toute sa carrière) n'entre pas entière
    dans le vivier : chaque page est réduite aux personnes en poste ici, et à ce qu'on en
    lit, AVANT d'être cumulée — et la réduction ne change pas ce qui est trouvé."""
    from oto_mcp.waterfall import gens

    def lourde(i, title, **kw):
        rec = _lemlist_rec(i, title, **kw)
        rec["experiences"][0]["description"] = "x" * 5000
        rec["experiences"].append({"company_domain": "past.com", "title": "Intern",
                                   "date_to": "2015", "description": "y" * 5000})
        rec["certifications"] = ["z" * 5000]
        return rec

    page1 = [lourde(0, "CMO"), lourde(1, "Developer"), lourde(2, "CEO", domain="other.com")]
    page2 = [lourde(3, "Developer"), lourde(4, "CTO", ended=True)]
    calls["answers"]["lemlist_database"] = [_lemlist_page(page1, 150, 1),
                                            _lemlist_page(page2, 150, 2)]
    calls["answers"]["jev_items"] = _verdicts(0.9, 0.1, 0.1)
    pools = []
    real = vivier._every_free_page

    async def spy(*a, **kw):
        out = await real(*a, **kw)
        pools.append(out)
        return out

    monkeypatch.setattr(vivier, "_every_free_page", spy)
    out = _run("waterfall_people", domain="acme.fr", limit=1, qualify=_FIT,
               qualify_min=0.7, providers=["lemlist"])
    [pool] = pools
    assert [r["full_name"] for r in pool["results"]] == ["Person 0", "Person 1", "Person 3"]
    for r in pool["results"]:
        assert set(r) == set(gens._LEMLIST_READS) | {"experiences"}
        assert [set(e) for e in r["experiences"]] == [set(gens._LEMLIST_POSITION_READS)]
    assert [p["full_name"] for p in out["people"]["people"]] == ["Person 0"]
    # Réduire puis extraire = extraire : rien de ce qui est lu n'est perdu.
    inputs = {"domain": "acme.fr", "limit": 10}
    whole = page1 + page2
    assert (gens._lemlist_people({"results": gens._lemlist_slim(whole, inputs)}, inputs)
            == gens._lemlist_people({"results": whole}, inputs))


# --- Google : une recherche par rôle -------------------------------

def _profil(nom, titre, slug, hote="fr"):
    return {"title": f"{nom} – Acme", "subtitle": f"Lyon · {titre} · Acme",
            "link": f"https://{hote}.linkedin.com/in/{slug}"}


def _serper(*organic):
    return IssueCible(ok=True, result={"organic": list(organic)})


def _requetes(calls):
    return [a["query"] for n, a in calls["made"] if n == "serper_search"]


def test_google_une_recherche_par_role_fusionnee_et_dedoublonnee(calls):
    """Serper rend une liste VIDE pour `"Acme" (a OR "b c" OR d)` là où `"Acme" "a"`
    trouve (mesuré) : une recherche par rôle, jamais un OU entre parenthèses ; les
    résultats sont fusionnés, le même profil (un autre hôte, une barre finale) compté
    une fois, chaque recherche envoyée facturée et comptée."""
    calls["answers"]["serper_search"] = [
        _serper(_profil("Ann Boss", "Responsable handicap", "ann-boss")),
        _serper(_profil("Ann Boss", "Responsable handicap", "ann-boss/", hote="www"),
                _profil("Cy Dee", "Chargée mission handicap", "cy-dee")),
        _serper(_profil("Di Eve", "Directrice diversité", "di-eve"))]
    out = _run("waterfall_people", company_name="Acme",
               roles=["handicap", "mission handicap", "diversité", "handicap"],
               providers=["google"])
    assert _requetes(calls) == ['"Acme" "handicap"', '"Acme" "mission handicap"',
                                '"Acme" "diversité"']
    assert not any("(" in q or " OR " in q for q in _requetes(calls))
    assert all(a["site_filter"] == "linkedin.com/in" for _, a in calls["made"])
    assert out["provider"] == "google"
    assert sorted(p["full_name"] for p in out["people"]["people"]) == [
        "Ann Boss", "Cy Dee", "Di Eve"]
    a = out["attempts"][0]
    assert a["status"] == "found" and a["calls"] == 3 and "stopped" not in a


def test_google_sans_role_une_seule_recherche_sur_la_societe(calls):
    _gens(calls)
    out = _run("waterfall_people", company_name="Acme", providers=["google"])
    assert _requetes(calls) == ['"Acme"']
    assert out["people"]["count"] == 2 and out["attempts"][0]["calls"] == 1


def test_google_rien_nulle_part_reste_no_match(calls):
    calls["answers"]["serper_search"] = _serper()
    out = _run("waterfall_people", company_name="Acme", roles=["sales", "CEO"],
               providers=["google"])
    assert out["found"] is False and out["attempts"][0]["status"] == "no_match"
    assert out["attempts"][0]["calls"] == 2 and "code" not in out["attempts"][0]


def test_google_borne_le_nombre_de_recherches_et_nomme_les_roles_laisses(calls):
    from oto_mcp.waterfall import gens
    calls["answers"]["serper_search"] = _serper()
    roles = [f"role{i}" for i in range(gens.GOOGLE_MAX_QUERIES + 2)]
    out = _run("waterfall_people", company_name="Acme", roles=roles, providers=["google"])
    assert len(_requetes(calls)) == gens.GOOGLE_MAX_QUERIES
    assert out["attempts"][0]["not_sent"] == ['"Acme" "role5"', '"Acme" "role6"']


def test_google_s_arrete_quand_la_limite_est_atteinte(calls):
    calls["answers"]["serper_search"] = [
        _serper(_profil("Ann Boss", "CEO", "ann-boss")),
        _serper(_profil("Never Sent", "CFO", "never"))]
    out = _run("waterfall_people", company_name="Acme", roles=["CEO", "CFO", "CTO"],
               limit=1, providers=["google"])
    assert _requetes(calls) == ['"Acme" "CEO"']
    a = out["attempts"][0]
    assert a["calls"] == 1 and a["stopped"] == "limit_reached"
    assert a["not_sent"] == ['"Acme" "CFO"', '"Acme" "CTO"']


def test_google_chaque_recherche_compte_dans_max_paid_steps(calls):
    """Chaque recherche envoyée est une étape payée : sous `max_paid_steps=2`, la
    troisième ne part pas, et un échec ainsi coupé n'est pas un verdict (`code`)."""
    calls["answers"]["serper_search"] = _serper()
    out = _run("waterfall_people", company_name="Acme", domain="acme.com",
               roles=["CEO", "CFO", "CTO"], providers=["google", "aiark"],
               max_paid_steps=2)
    assert len(_requetes(calls)) == 2 and "linkedin_aiark_search" not in _made(calls)
    a = out["attempts"][0]
    assert a["calls"] == 2 and a["stopped"] == "max_paid_steps"
    assert a["status"] == "no_match" and a["code"] == "max_paid_steps"
    assert out["attempts"][1]["code"] == "max_paid_steps"


def test_google_un_echec_tardif_garde_ce_que_les_premieres_ont_trouve(calls):
    calls["answers"]["serper_search"] = [
        _serper(_profil("Ann Boss", "CEO", "ann-boss")),
        IssueCible(ok=False, code="upstream_error", message="Serper 500")]
    out = _run("waterfall_people", company_name="Acme", roles=["CEO", "CFO", "CTO"],
               providers=["google"])
    assert [p["full_name"] for p in out["people"]["people"]] == ["Ann Boss"]
    a = out["attempts"][0]
    assert a["status"] == "found" and a["calls"] == 2 and a["stopped"] == "upstream_error"
    assert a["not_sent"] == ['"Acme" "CTO"']


def test_google_un_premier_echec_reste_un_echec(calls):
    calls["answers"]["serper_search"] = IssueCible(ok=False, code="upstream_error",
                                                   message="Serper 500")
    out = _run("waterfall_people", company_name="Acme", roles=["CEO", "CFO"],
               providers=["google"])
    a = out["attempts"][0]
    assert out["found"] is False and a["status"] == "failed" and a["calls"] == 1


def test_google_dry_run_montre_chaque_recherche(calls):
    out = _run("waterfall_people", company_name="Acme", roles=["CEO", "head of sales"],
               providers=["google"], dry_run=True)
    a = out["attempts"][0]
    assert a["arguments"]["query"] == '"Acme" "CEO"'
    assert [x["query"] for x in a["fan_out"]] == ['"Acme" "CEO"', '"Acme" "head of sales"']
    assert calls["made"] == []


def test_google_les_pages_suivantes_relancent_chaque_recherche(calls):
    """Sous `qualify_min`, la page suivante d'une liste payée se lit pour CHAQUE
    recherche par rôle — jamais la page 2 d'un OU entre parenthèses."""
    calls["answers"]["serper_search"] = [
        _serper(_profil("Ann Boss", "CEO", "ann-boss")),
        _serper(_profil("Bob Dev", "CFO", "bob-dev")),
        _serper(_profil("Cy Dee", "CEO", "cy-dee")),
        _serper()]
    calls["answers"]["jev_items"] = [_verdicts(0.9, 0.1), _verdicts(0.9)]
    out = _run("waterfall_people", company_name="Acme", roles=["CEO", "CFO"], limit=2,
               qualify=_FIT, qualify_min=0.7, providers=["google"])
    made = [(a["query"], a.get("page", 1)) for n, a in calls["made"] if n == "serper_search"]
    assert made == [('"Acme" "CEO"', 1), ('"Acme" "CFO"', 1),
                    ('"Acme" "CEO"', 2), ('"Acme" "CFO"', 2)]
    assert sorted(p["full_name"] for p in out["people"]["people"]) == ["Ann Boss", "Cy Dee"]


def test_google_rien_trouve_puis_un_echec_reste_un_echec(calls):
    """Une première recherche vide puis une qui échoue : l'issue est l'ÉCHEC, pas un
    `no_match` — le mode tableau l'écrirait sur la ligne au lieu de la reprendre."""
    calls["answers"]["serper_search"] = [
        _serper(), IssueCible(ok=False, code="upstream_error", message="Serper 500")]
    out = _run("waterfall_people", company_name="Acme", roles=["CEO", "CFO", "CTO"],
               providers=["google"])
    a = out["attempts"][0]
    assert out["found"] is False and a["status"] == "failed"
    assert a["code"] == "upstream_error" and a["calls"] == 2


def test_google_une_page_suivante_ne_rend_pas_deux_fois_le_meme_profil(calls):
    """Le même profil, revenu sous un autre hôte à la page 2, n'est ni rejugé ni rendu
    deux fois : une seule clé de personne pour toute la cascade (son slug LinkedIn)."""
    calls["answers"]["serper_search"] = [
        _serper(_profil("Ann Boss", "CEO", "ann-boss")),
        _serper(_profil("Ann Boss", "CEO", "ann-boss/", hote="www")),
        _serper()]
    calls["answers"]["jev_items"] = [_verdicts(0.9)]
    out = _run("waterfall_people", company_name="Acme", roles=["CEO"], limit=2,
               qualify=_FIT, qualify_min=0.7, providers=["google"])
    assert [p["full_name"] for p in out["people"]["people"]] == ["Ann Boss"]
    assert _made(calls).count("jev_items") == 1
