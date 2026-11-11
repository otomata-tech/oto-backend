"""Les waterfalls sur un TABLEAU (`waterfall_<x>_act(op="update_rows")`) : une ligne
payée une fois, une recherche par entrées distinctes, rien de lu ni de trouvé au reçu ;
un échec n'est pas un verdict (`no_match` seulement quand une étape a RÉPONDU sans trouver
et qu'aucune n'a échoué) ; `qualify` sans Jev utilisable refusé avant de lire une ligne."""
from __future__ import annotations

import asyncio

import pytest
from fastmcp import FastMCP
from mcp.types import ErrorData, INVALID_PARAMS

from oto_mcp.datastore import par_reference as pr
from oto_mcp.mcp_errors import McpError
from oto_mcp.tools import waterfall_lignes
from oto_mcp.tools.meta import IssueCible

from _waterfall_faux import _FIT, _verdicts, calls  # noqa: F401 — fournisseurs simulés


class _Store:
    def __init__(self, schema):
        self.schema, self.patches, self.ecrites = schema, [], {}
        self.dernier_tableau = {"ns_id": 42, "namespace": "t"}

    def get_schema(self, adresse):
        return self.schema

    def patch_schema(self, adresse, fields):
        self.patches.append(fields)
        self.schema = {"fields": [*self.schema["fields"], *fields]}

    def count_rows(self, *a, **k):
        return 0


@pytest.fixture()
def table(monkeypatch):
    etat = {"lignes": [], "store": _Store({"fields": [
        {"key": "first_name"}, {"key": "last_name"}, {"key": "website"},
        {"key": "linkedin"}, {"key": "employees", "type": "number"}]})}

    def ouvrir(datastore, *, row_ids, filter, colonne_etat, limite):
        lignes = [r for r in etat["lignes"] if not r.get(colonne_etat)]
        return pr.Lot(etat["store"], "t", lignes[:limite], [], filter or {},
                      [{"field": colonne_etat, "op": "empty", "value": True}])

    def ecrire(lot, rid, patch):
        etat["store"].ecrites.setdefault(rid, {}).update(patch)
        return None

    monkeypatch.setattr(pr, "ouvrir", ouvrir)
    monkeypatch.setattr(pr, "colonnes_inconnues", lambda lot, cols: [])
    monkeypatch.setattr(pr, "ecrire", ecrire)
    monkeypatch.setattr(pr, "tenue_ailleurs", lambda ligne: bool(ligne.get("_held")))
    monkeypatch.setattr(pr.identite, "numero", lambda t: {"datastore": 42})
    return etat


def _act(ns, **kw):
    m = FastMCP("t")
    waterfall_lignes.register(m)
    return asyncio.run(asyncio.run(m.get_tool(f"{ns}_act")).fn(op="update_rows", **kw))


_EMAIL_ARGS = dict(datastore="42", inputs={"first_name": "first_name",
                                           "last_name": "last_name", "domain": "website"},
                   output={"email": "email", "provider": "email_source"},
                   status_column="email_status", providers=["hunter"])


def test_une_ligne_trouvee_est_ecrite_et_le_recu_ne_porte_aucune_valeur(calls, table):
    table["lignes"] = [{"_id": "r1", "first_name": "Jane", "last_name": "Doe",
                        "website": "https://acme.com"}]
    calls["answers"]["hunter_email_finder"] = IssueCible(
        ok=True, result={"data": {"email": "jane@acme.com", "verification": {"status": "valid"}}})
    out = _act("waterfall_email", **_EMAIL_ARGS)
    w = table["store"].ecrites["r1"]
    assert w["email"] == "jane@acme.com" and w["email_source"] == "hunter"
    assert w["email_status"] == "found" and w["email_status.comment"] == "hunter"
    assert out["found"] == 1 and "jane" not in str(out)
    # Les colonnes manquantes sont DÉCLARÉES avant d'être écrites.
    assert {f["key"] for f in table["store"].patches[0]} == {"email_status", "email", "email_source"}


def test_memes_entrees_une_seule_recherche_ecrite_sur_chaque_ligne(calls, table):
    row = {"first_name": "Jane", "last_name": "Doe", "website": "acme.com"}
    table["lignes"] = [{"_id": "r1", **row}, {"_id": "r2", **row}]
    calls["answers"]["hunter_email_finder"] = IssueCible(
        ok=True, result={"data": {"email": "jane@acme.com"}})
    out = _act("waterfall_email", **_EMAIL_ARGS)
    assert [n for n, _ in calls["made"]].count("hunter_email_finder") == 1
    assert out["found"] == 2 and out["unique_lookups"] == 1 and out["rows_sharing_a_lookup"] == 1


def test_sans_trouvaille_no_match_est_ecrit_pour_ne_pas_repayer(calls, table):
    table["lignes"] = [{"_id": "r1", "first_name": "Jane", "last_name": "Doe",
                        "website": "acme.com"}]
    calls["answers"]["hunter_email_finder"] = IssueCible(ok=True, result={"data": {"email": None}})
    out = _act("waterfall_email", **_EMAIL_ARGS)
    assert table["store"].ecrites["r1"] == {"email_status": "no_match"} and out["no_match"] == 1
    # Relancé : la ligne a son état, rien n'est rappelé.
    table["lignes"][0]["email_status"] = "no_match"
    calls["made"].clear()
    out = _act("waterfall_email", **_EMAIL_ARGS)
    assert out["selected"] == 0 and calls["made"] == []


def test_ligne_sans_entree_attend_et_ligne_tenue_est_ecartee(calls, table):
    table["lignes"] = [{"_id": "r1", "first_name": "Jane"},
                       {"_id": "r2", "_held": True, "first_name": "A", "last_name": "B",
                        "website": "b.com"}]
    out = _act("waterfall_email", **_EMAIL_ARGS)
    assert out["skipped_missing_input"] == 1 and out["skipped_row_locked"] == 1
    assert table["store"].ecrites == {} and calls["made"] == []


def test_les_etapes_asynchrones_sont_laissees_de_cote(calls, table):
    table["lignes"] = [{"_id": "r1", "linkedin": "https://www.linkedin.com/in/jane"}]
    out = _act("waterfall_email", datastore="42", inputs={"linkedin_url": "linkedin"},
               output={"email": "email"}, status_column="email_status",
               providers=["lemlist", "aiark"])
    made = [n for n, _ in calls["made"]]
    assert "lemlist_enrich" not in made and "linkedin_aiark_person" in made
    assert "lemlist" in out["left_out_async_steps"]


def test_un_nombre_va_dans_une_colonne_number_et_une_liste_dans_du_texte(calls, table):
    table["lignes"] = [{"_id": "r1", "website": "acme.de"}]
    calls["answers"]["linkedin_aiark_search"] = IssueCible(ok=True, result={"content": [{
        "summary": {"name": "ACME", "staff": {"total": 12}},
        "link": {"domain": "acme.de"}}]})
    _act("waterfall_company", datastore="42", inputs={"domain": "website"},
         output={"name": "company", "employees": "employees"},
         status_column="company_status", providers=["aiark"])
    w = table["store"].ecrites["r1"]
    assert w["employees"] == 12 and w["company"] == "ACME"


def test_dry_run_ne_declare_ni_n_ecrit_rien(calls, table):
    table["lignes"] = [{"_id": "r1", "first_name": "Jane", "last_name": "Doe",
                        "website": "acme.com"}]
    out = _act("waterfall_email", dry_run=True, **_EMAIL_ARGS)
    assert out["would_look_up"] == 1 and out["first_lookup_plan"]["dry_run"] is True
    assert "Jane" not in str(out) and "acme" not in str(out)   # rien de la ligne
    assert table["store"].patches == [] and table["store"].ecrites == {}
    assert "hunter_email_finder" not in [n for n, _ in calls["made"]]


@pytest.mark.parametrize("kw", [
    {"inputs": {"siren": "x"}},                                   # pas une entrée d'email
    {"output": {}},
    {"status_column": "email"},                                    # aussi une sortie
    {"qualify": {"fit": {"type": "noul"}}},                        # rien à qualifier
    {"roles": ["CEO"]},                                            # propre à people
])
def test_un_appel_mal_forme_est_refuse_avant_tout(calls, table, kw):
    with pytest.raises(McpError):
        _act("waterfall_email", **{**_EMAIL_ARGS, **kw})
    assert calls["made"] == []


def test_people_ecrit_la_liste_compacte_et_marque_unqualified(calls, table):
    table["store"].schema["fields"].append({"key": "company"})
    table["lignes"] = [{"_id": "c1", "company": "Acme"}, {"_id": "c2", "company": "Beta"}]
    calls["answers"]["serper_search"] = IssueCible(ok=True, result={"organic": [
        {"title": "Ann Boss – Acme", "subtitle": "Lyon · CEO · Acme",
         "link": "https://fr.linkedin.com/in/ann-boss"},
        {"title": "Bo Beta – Beta", "subtitle": "Lyon · Intern · Beta",
         "link": "https://fr.linkedin.com/in/bo-beta"}]})
    calls["answers"]["jev_items"] = [_verdicts(0.9), _verdicts(0.1)]
    out = _act("waterfall_people", datastore="42", inputs={"company_name": "company"},
               output={"people": "people", "count": "people_count"},
               status_column="people_status", providers=["google"],
               qualify=_FIT, qualify_min=0.7)
    w = table["store"].ecrites
    assert out["found"] == 1 and out["unqualified"] == 1
    statuses = sorted(v["people_status"] for v in w.values())
    assert statuses == ["found", "unqualified"]
    found = next(v for v in w.values() if v["people_status"] == "found")
    assert found["people"][0]["fit"] == {"fit": 0.9} and "location" not in found["people"][0]
    assert out["qualification"]["calls"] == 2


def test_que_des_etapes_asynchrones_est_refuse_sans_marquer_les_lignes(calls, table):
    table["lignes"] = [{"_id": "r1", "linkedin": "https://www.linkedin.com/in/jane"}]
    with pytest.raises(McpError):
        _act("waterfall_email", datastore="42", inputs={"linkedin_url": "linkedin"},
             output={"email": "email"}, status_column="email_status", providers=["lemlist"])
    assert table["store"].ecrites == {}


def test_une_recherche_pas_partie_a_temps_arrete_le_recu(calls, table, monkeypatch):
    monkeypatch.setattr(waterfall_lignes, "DEPART_S", -1)
    table["lignes"] = [{"_id": "r1", "first_name": "Jane", "last_name": "Doe",
                        "website": "acme.com"},
                       {"_id": "r2", "first_name": "Bad", "last_name": "Siren",
                        "website": "b.com"}]
    out = _act("waterfall_email", **_EMAIL_ARGS)
    assert out["stopped"] == "time_budget" and table["store"].ecrites == {}


def test_une_cascade_coupee_par_le_temps_laisse_la_ligne_en_attente(calls, table, monkeypatch):
    monkeypatch.setattr(waterfall_lignes, "CASCADE_S", -100)
    table["lignes"] = [{"_id": "r1", "first_name": "Jane", "last_name": "Doe",
                        "website": "acme.com"}]
    out = _act("waterfall_email", **_EMAIL_ARGS)
    assert table["store"].ecrites == {} and out["skipped_time_budget"] == 1
    assert out["stopped"] == "time_budget"


def test_tech_sur_un_tableau_la_liste_en_json_et_la_techno_cherchee_pour_chaque_ligne(calls, table):
    table["lignes"] = [{"_id": "r1", "website": "acme.com"}, {"_id": "r2", "website": "b.com"}]
    calls["answers"]["dataforseo_technologies"] = [
        IssueCible(ok=True, result={"technologies": {"content": {"cms": ["Shopify"]}}}),
        IssueCible(ok=True, result={"technologies": {"content": {"cms": ["WordPress"]}}})]
    out = _act("waterfall_tech", datastore="42", inputs={"domain": "website"},
               output={"technologies": "stack", "matched": "uses"},
               status_column="tech_status", providers=["dataforseo"],
               technologies=["shopify"])
    e = table["store"].ecrites
    assert out["found"] == 1 and out["no_match"] == 1
    statuts = sorted(w["tech_status"] for w in e.values())
    assert statuts == ["found", "no_match"]
    trouvee = next(w for w in e.values() if w["tech_status"] == "found")
    assert trouvee["stack"] == ["Shopify"] and trouvee["uses"] == ["shopify"]
    assert {f["key"]: f["type"] for f in table["store"].patches[0]}["stack"] == "json"


def test_technologies_refuse_hors_du_waterfall_tech(calls, table):
    with pytest.raises(McpError):
        _act("waterfall_email", technologies=["Shopify"], **_EMAIL_ARGS)


# --- un échec n'est pas un verdict ----------------------------------------------------

def _une_ligne(table):
    table["lignes"] = [{"_id": "r1", "first_name": "Jane", "last_name": "Doe",
                        "website": "acme.com"}]


@pytest.mark.parametrize("reponse,code", [
    (IssueCible(ok=False, code="quota_exhausted", message="no credits"), "step_failed"),
    (IssueCible(ok=True, result="withheld", retenu=True), "step_failed"),
])
def test_une_etape_en_echec_laisse_la_ligne_vide_et_le_recu_dit_pourquoi(calls, table,
                                                                         reponse, code):
    _une_ligne(table)
    calls["answers"]["hunter_email_finder"] = reponse
    out = _act("waterfall_email", **_EMAIL_ARGS)
    assert table["store"].ecrites == {} and out.get("no_match") is None
    assert out[f"skipped_{code}"] == 1 and out["errors"] == [{"row_id": "r1", "code": code}]


def test_un_echec_suivi_d_un_non_trouve_n_est_toujours_pas_un_verdict(calls, table):
    _une_ligne(table)
    calls["answers"]["hunter_email_finder"] = IssueCible(ok=False, code="upstream", message="x")
    calls["answers"]["apollo_match_person"] = IssueCible(ok=True, result={"person": {}})
    out = _act("waterfall_email", **{**_EMAIL_ARGS, "providers": ["hunter", "apollo"]})
    assert table["store"].ecrites == {} and out["skipped_step_failed"] == 1


def test_aucune_etape_qui_tourne_n_est_pas_un_verdict(calls, table):
    _une_ligne(table)
    calls["modes"] = {"hunter_email_finder": "forbidden"}
    out = _act("waterfall_email", **_EMAIL_ARGS)
    assert table["store"].ecrites == {} and out["skipped_no_step_answered"] == 1
    assert calls["made"] == []


def test_un_fournisseur_inconnu_est_refuse_une_fois_sans_marquer_les_lignes(calls, table):
    _une_ligne(table)
    with pytest.raises(McpError, match="waterfall_rows_providers"):
        _act("waterfall_email", **{**_EMAIL_ARGS, "providers": ["hunter", "nope"]})
    assert table["store"].ecrites == {} and calls["made"] == []


def test_qualify_sans_jev_utilisable_est_refuse_avant_de_lire_une_ligne(calls, table,
                                                                        monkeypatch):
    lues = []
    monkeypatch.setattr(pr, "ouvrir", lambda *a, **k: lues.append(1))
    calls["modes"] = {"jev_items": "forbidden"}
    with pytest.raises(McpError) as e:
        _act("waterfall_people", datastore="42", inputs={"company_name": "company"},
             output={"people": "people"}, status_column="people_status",
             providers=["google"], qualify=_FIT)
    assert e.value.error.data["code"] == "qualification_unavailable"
    assert lues == [] and calls["made"] == []


def test_une_trouvaille_que_jev_n_a_pas_jugee_n_est_pas_ecrite(calls, table):
    table["store"].schema["fields"].append({"key": "company"})
    table["lignes"] = [{"_id": "c1", "company": "Acme"}]
    calls["answers"]["serper_search"] = IssueCible(ok=True, result={"organic": [
        {"title": "Ann Boss – Acme", "subtitle": "Lyon · CEO · Acme",
         "link": "https://fr.linkedin.com/in/ann-boss"}]})
    calls["answers"]["jev_items"] = McpError(ErrorData(code=INVALID_PARAMS, message="refused"))
    out = _act("waterfall_people", datastore="42", inputs={"company_name": "company"},
               output={"people": "people"}, status_column="people_status",
               providers=["google"], qualify=_FIT)
    assert table["store"].ecrites == {} and out["skipped_qualification_skipped"] == 1


def _hors_liste(name):
    """Le refus que la garde du travail hébergé lève sur l'outil d'une étape."""
    return McpError(ErrorData(code=INVALID_PARAMS,
                              message=f"Refusal `tool_not_in_job`: `{name}` is not in this "
                                      "hosted agent's tool list (job 41).",
                              data={"code": "tool_not_in_job", "tool": name}))


@pytest.fixture
def _travail_41(monkeypatch):
    """Le travail 41 de l'agent 7, qui a la cascade mais pas l'outil de Hunter."""
    from oto_mcp import outils_du_travail
    import types
    monkeypatch.setattr(outils_du_travail.verrou_org, "travail_de_la_requete",
                        lambda: types.SimpleNamespace(job_id=41))
    monkeypatch.setattr(outils_du_travail.db, "charge_du_travail", lambda job: {
        "tools": ["waterfall_email_act", "apollo_match_person"], "trigger_id": 7})


def test_une_etape_hors_de_la_liste_du_travail_n_est_ni_un_verdict_ni_un_quota(
        calls, table, _travail_41):
    """Hunter refusé au travail, Apollo répond sans trouver : écrire `no_match` fermerait
    la ligne pour de bon, alors que Hunter n'a jamais tourné. Le reçu nomme l'outil et le
    geste qui le donne — pas un quota."""
    _une_ligne(table)
    calls["answers"]["hunter_email_finder"] = _hors_liste("hunter_email_finder")
    calls["answers"]["apollo_match_person"] = IssueCible(ok=True, result={"person": {}})
    out = _act("waterfall_email", **{**_EMAIL_ARGS, "providers": ["hunter", "apollo"]})
    assert table["store"].ecrites == {} and out.get("no_match") is None
    assert out["skipped_tool_not_in_job"] == 1
    assert out["errors"] == [{"row_id": "r1", "code": "tool_not_in_job"}]
    assert out["missing_tools"] == ["hunter_email_finder"]
    assert out["fix"] == {"tool": "oto_trigger", "op": "update", "trigger_id": 7, "tools": [
        "waterfall_email_act", "apollo_match_person", "hunter_email_finder"]}
    assert "not a quota" in out["next_step"] and "agent 7" in out["next_step"]


def test_sans_etape_refusee_le_recu_ne_parle_pas_d_outils(calls, table, _travail_41):
    _une_ligne(table)
    calls["answers"]["hunter_email_finder"] = IssueCible(ok=True, result={"data": {}})
    out = _act("waterfall_email", **_EMAIL_ARGS)
    assert "missing_tools" not in out and "fix" not in out


def test_le_plan_d_un_dry_run_dit_l_etape_que_le_travail_refusera(calls, table, _travail_41):
    """Le plan disait `planned` sans un mot : l'agent l'a cru, puis a conclu à un quota."""
    _une_ligne(table)
    out = _act("waterfall_email", **{**_EMAIL_ARGS, "providers": ["hunter", "apollo"],
                                     "dry_run": True})
    assert calls["made"] == [] and table["store"].ecrites == {}
    assert out["missing_tools"] == ["hunter_email_finder"]
    assert out["fix"]["tools"][-1] == "hunter_email_finder"
    assert "will be skipped" in out["next_step"]


def test_chaque_ligne_trouvee_porte_l_outil_de_l_etape_qui_a_trouve(calls, table, monkeypatch):
    """Le journal des révisions nomme, par valeur, l'OUTIL de l'étape gagnante (ce qu'un
    client résout en logo, « trouvé par Hunter ») ; un verdict sans trouvaille n'en
    porte aucun."""
    from oto_mcp import geste
    vus: dict = {}

    def ecrire(lot, rid, patch):
        vus[rid] = geste.estampille()["fournisseur"]
        table["store"].ecrites.setdefault(rid, {}).update(patch)
        return None

    monkeypatch.setattr(pr, "ecrire", ecrire)
    table["lignes"] = [{"_id": "r1", "first_name": "Jane", "last_name": "Doe",
                        "website": "acme.com"}]
    calls["answers"]["hunter_email_finder"] = IssueCible(
        ok=True, result={"data": {"email": "jane@acme.com"}})
    _act("waterfall_email", **_EMAIL_ARGS)
    table["lignes"] = [{"_id": "r2", "first_name": "Max", "last_name": "Roe",
                        "website": "nope.com"}]
    calls["answers"]["hunter_email_finder"] = IssueCible(
        ok=True, result={"data": {"email": None}})
    _act("waterfall_email", **_EMAIL_ARGS)
    assert table["store"].ecrites["r2"]["email_status"] == "no_match"
    assert vus == {"r1": "hunter_email_finder", "r2": None}
    assert geste.estampille()["fournisseur"] is None, "rien ne reste posé après l'écriture"
