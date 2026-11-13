"""Connecteur Pipecorn — enrichissement de contact en cascade, et le solde.

Les tripwires génériques couvrent déjà le registre, l'éditeur, le logo, la prose
servie, la paire lire/agir et la jointure au client oto-core. Ce fichier verrouille
ce qui est PROPRE à ce module :

- un seul outil `pipecorn` (LECTURE), le verbe dans `op` ;
- l'enrichissement est ASYNCHRONE : `enrich` soumet, `result` relève ;
- la trace : un zéro à l'envoi (et une recherche, `units`, sur la clé partagée), un
  zéro à une relève en cours ou échouée, et à la relève finie `quantity` 1 ou 0, les
  crédits Pipecorn dépensés (`provider_credits`) et les trois `found_*` ensemble ;
- le budget d'unités : la soumission est au périmètre, son pire cas est d'une
  trouvaille par sorte demandée ; sur une clé refacturée, la relève finie règle chaque
  sorte à ce qu'elle a trouvé, une relève échouée libère ; sur la clé du client, rien ;
- l'email personnel d'un tiers est refusé sur toute clé, refus nommé, AVANT toute
  résolution de clé ; les recherches synchrones (profil, société, technologies, offres)
  rendent leur réponse SANS aucun champ d'email ni de téléphone ;
- la clé partagée sert le téléphone et l'email pro (au budget d'unités) ; le solde, et
  les recherches synchrones (hors budget) sur toute clé qui n'est pas celle de
  l'appelant, sont refusés AVANT tout envoi ;
- une recherche synchrone trace `quantity` 1 ou 0 (jamais absent) et ses crédits
  Pipecorn (`provider_credits`) ; un 404 est un non-trouvé, rien de facturé ;
- une réponse illisible est un refus nommé, jamais une clé refusée ;
- la traduction des refus Pipecorn (402 sur la clé partagée ≠ sur la sienne) et la sonde.

Porte `exige_pin_oto_core` : `oto.tools.pipecorn` et la description
`pipecorn.submit_enrichment` (`oto.connectors.declared`) viennent de la bibliothèque des
connecteurs. Toutes les valeurs sont factices.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from oto_mcp import providers
from oto_mcp.mcp_errors import McpError

pytestmark = pytest.mark.exige_pin_oto_core

KEY = "pc_test_fake"
URL = "https://www.linkedin.com/in/jane-doe/"
ID = "9d8c7b6a-5f4e-4d3c-8b2a-1f0e9d8c7b6a"
FINI = {"enrichment_id": ID, "status": "finished", "phone": ["+33612345678"],
        "phone_details": [{"number": "+33612345678", "type": "mobile"}]}
RIEN = {"found_work_emails": 0, "found_personal_emails": 0, "found_phones": 0}


@pytest.fixture()
def key(monkeypatch):
    """La clé que rend la cascade : `mode` = `org` (la sienne) ou `platform`."""
    state = {"mode": "org", "resolved": 0}

    def resolve(provider, account=None, check_usage=True):
        assert provider == "pipecorn"
        state["resolved"] += 1
        state["check_usage"] = check_usage
        return SimpleNamespace(key=KEY, is_platform=state["mode"] == "platform",
                               mode=state["mode"], entity_id="7")
    monkeypatch.setattr("oto_mcp.access.resolve_credential", resolve)
    monkeypatch.setattr("oto_mcp.access.current_org", lambda sub: None)
    monkeypatch.setattr("oto_mcp.auth.hooks.current_user_sub_from_token", lambda: "sub")
    return state


@pytest.fixture()
def trace(monkeypatch):
    noted = {}
    monkeypatch.setattr("oto_mcp.session_org.note_call_trace",
                        lambda **values: noted.update(values))
    return noted


@pytest.fixture()
def releve(monkeypatch):
    """Ce que la relève règle ou libère sur le budget d'unités."""
    from oto_mcp.access import credits_releve
    vus = []
    monkeypatch.setattr(credits_releve, "regler_releve",
                        lambda fn, args, trouves: vus.append(("regler", fn, args, trouves)))
    monkeypatch.setattr(credits_releve, "liberer_releve",
                        lambda fn, args: vus.append(("liberer", fn, args)))
    return vus


def _tools():
    from fastmcp import FastMCP
    from oto_mcp.tools import pipecorn

    m = FastMCP("t")
    pipecorn.register(m)
    return {t.name: t for t in asyncio.run(m._list_tools())}


def _fn():
    return _tools()["pipecorn"].fn


@pytest.fixture()
def client():
    with patch("oto.tools.pipecorn.PipecornClient") as cls:
        cls.return_value.submit_enrichment.return_value = {
            "enrichment_id": ID, "status": "pending"}
        cls.return_value.get_enrichment.return_value = FINI
        cls.return_value.get_credits.return_value = {"credits": {"enrichment": 2500}}
        cls.return_value.get_account_info.return_value = {"account": {"plan": "growth"}}
        cls.return_value.enrich_person.return_value = {"full_name": "Jane Doe", "positions": []}
        cls.return_value.enrich_account.return_value = {"company_name": "Acme",
                                                        "domain": "acme.com"}
        cls.return_value.company_stack.return_value = {"technologies": ["HubSpot"]}
        cls.return_value.company_hiring.return_value = {"total_jobs": 0, "jobs": []}
        yield cls


# --- registre ---------------------------------------------------------------

def test_un_seul_outil_en_lecture():
    tools = _tools()
    assert set(tools) == {"pipecorn"}
    assert tools["pipecorn"].annotations.readOnlyHint is True


def test_registre_ferme_par_defaut_cle_partagee_sur_octroi_seulement():
    c = providers.REGISTRY["pipecorn"]
    assert c.auth_modes == {"byo_user", "byo_org", "platform"}
    assert c.platform_key_open is False and c.default_quota == 0
    assert c.grant_quota_unit == ""


def test_la_soumission_est_au_perimetre_par_sorte_trouvee():
    from oto_mcp.access import credits as budget
    from oto_mcp.tools import pipecorn
    _tools()
    assert "pipecorn" in budget._PERIMETRE
    assert pipecorn._pire_par_nature({"find": ["phone", "email"]}) == {
        "phones": 1, "work_emails": 1}
    assert pipecorn._pire_par_nature({}) == {"phones": 1}


# --- enrich : l'envoi asynchrone -----------------------------------------------

def test_enrich_soumet_et_rend_l_identifiant_a_relever(key, trace, client):
    out = _fn()(linkedin_url=URL, phone_type="mobile")
    client.assert_called_once_with(api_key=KEY)
    assert key["check_usage"] is True
    sent = client.return_value.submit_enrichment.call_args
    assert sent.args == (["phone"],)
    assert sent.kwargs["linkedin_url"] == URL and sent.kwargs["phone_type"] == "mobile"
    client.return_value.enrich_contact.assert_not_called()
    assert out["enrichment_id"] == ID and out["status"] == "pending"
    assert out["find"] == ["phone"] and "op='result'" in out["next_step"]
    # Rien n'est dépensé avant la fin : un zéro TRACÉ.
    assert trace == {"quantity": 0}


def test_enrich_sur_la_cle_partagee_compte_une_recherche(key, trace, client):
    key["mode"] = "platform"
    _fn()(linkedin_url=URL)
    assert trace == {"quantity": 0, "units": 1}


def test_un_envoi_sans_identifiant_est_un_refus(key, trace, client):
    client.return_value.submit_enrichment.return_value = {"status": "pending"}
    with pytest.raises(McpError, match="no enrichment_id"):
        _fn()(linkedin_url=URL)


@pytest.mark.parametrize("mode", ["org", "user", "platform"])
@pytest.mark.parametrize("find", [["personal_email"], ["phone", "personal_email"]])
def test_l_email_personnel_d_un_tiers_est_refuse_sur_toute_cle(key, trace, client, mode,
                                                               find):
    key["mode"] = mode
    with pytest.raises(McpError, match="pipecorn_personal_email_refused") as e:
        _fn()(find=find, linkedin_url=URL)
    assert e.value.error.data["code"] == "pipecorn_personal_email_refused"
    assert key["resolved"] == 0
    client.return_value.submit_enrichment.assert_not_called()
    assert trace == {}


@pytest.mark.parametrize("find", [["email"], ["phone", "email"]])
def test_cle_partagee_sert_le_telephone_et_l_email_pro(key, trace, client, find):
    key["mode"] = "platform"
    out = _fn()(find=find, linkedin_url=URL)
    assert key["check_usage"] is True
    assert client.return_value.submit_enrichment.call_args.args == (find,)
    assert out["find"] == find and trace == {"quantity": 0, "units": 1}


@pytest.mark.parametrize("kwargs", [{"op": "credits"}, {"op": "account"}])
def test_cle_partagee_refuse_le_solde(key, trace, client, kwargs):
    key["mode"] = "platform"
    with pytest.raises(McpError, match="needs your own Pipecorn key"):
        _fn()(**kwargs)
    client.return_value.get_credits.assert_not_called()
    client.return_value.get_account_info.assert_not_called()
    assert trace == {}


def test_une_entree_manquante_est_un_refus_nomme(key, trace):
    # Le vrai client (pas de faux) : son refus local devient un INVALID_PARAMS.
    with pytest.raises(McpError, match="linkedin_url"):
        _fn()(find=["phone"])
    assert trace == {}


def test_une_reponse_illisible_est_nommee_pas_une_cle_refusee(key, trace, client):
    from oto.tools.pipecorn import PipecornUnreadable
    client.return_value.submit_enrichment.side_effect = PipecornUnreadable("not JSON")
    with pytest.raises(McpError, match="pipecorn_unreadable_answer") as e:
        _fn()(linkedin_url=URL)
    assert e.value.error.data["code"] == "pipecorn_unreadable_answer"
    assert "API key" not in str(e.value)


# --- result : la relève, qui règle ------------------------------------------------

def test_en_cours_rien_n_est_facture(key, trace, client, releve):
    client.return_value.get_enrichment.return_value = {"status": "pending"}
    out = _fn()(op="result", enrichment_id=ID, find=["phone"])
    client.return_value.get_enrichment.assert_called_once_with(ID)
    assert key["check_usage"] is False
    assert out["done"] is False and trace == {"quantity": 0} and releve == []


def test_fini_trace_ce_qui_a_ete_trouve_et_demande(key, trace, client, releve):
    # Le bac à sable rend aussi un email non demandé : il ne compte pas.
    client.return_value.get_enrichment.return_value = {**FINI, "email": "jane@acme.com"}
    out = _fn()(op="result", enrichment_id=ID, find=["phone"])
    assert out["done"] is True and out["pipecorn_credits"] == 30
    assert out["phone_details"] == FINI["phone_details"]
    assert trace == {"quantity": 1, "provider_credits": 30, **RIEN, "found_phones": 1}
    # Sur la clé du client, rien n'était réservé : rien à régler.
    assert releve == []


def test_un_email_perso_rendu_sans_etre_demande_ne_sort_jamais(key, trace, client):
    client.return_value.get_enrichment.return_value = {
        **FINI, "personal_email": "jane@gmail.com", "personal_email_status": "valid"}
    out = _fn()(op="result", enrichment_id=ID, find=["phone"])
    assert not any(k.startswith("personal_email") for k in out)
    assert trace["found_personal_emails"] == 0


def test_un_mobile_sans_indicatif_est_compte_trouve(key, trace, client):
    client.return_value.get_enrichment.return_value = {
        "status": "finished", "phone": ["0612345678"],
        "phone_details": [{"number": "0612345678", "type": "mobile"}]}
    out = _fn()(op="result", enrichment_id=ID, find=["phone"])
    assert out["pipecorn_credits"] == 30 and out["phone"] == ["0612345678"]
    assert trace["found_phones"] == 1 and trace["quantity"] == 1


def test_fini_sans_mobile_trace_un_zero(key, trace, client):
    client.return_value.get_enrichment.return_value = {
        "status": "finished", "phone": [], "phone_details": []}
    out = _fn()(op="result", enrichment_id=ID)
    assert out["pipecorn_credits"] == 0
    assert trace == {"quantity": 0, "provider_credits": 0, **RIEN}


def test_fini_email_et_telephone(key, trace, client):
    client.return_value.get_enrichment.return_value = {
        **FINI, "email": "jane@acme.com", "email_status": "deliverable"}
    out = _fn()(op="result", enrichment_id=ID, find=["email", "phone"])
    assert out["pipecorn_credits"] == 33
    assert trace == {"quantity": 1, "provider_credits": 33, "found_work_emails": 1,
                     "found_personal_emails": 0, "found_phones": 1}


def test_la_releve_sur_la_cle_partagee_regle_par_sorte(key, trace, client, releve):
    key["mode"] = "platform"
    _fn()(op="result", enrichment_id=ID, find=["phone"])
    assert trace == {"quantity": 1, "provider_credits": 30, **RIEN, "found_phones": 1,
                     "units": 0}
    assert releve == [("regler", "pipecorn.get_enrichment", {"enrichment_id": ID},
                       {"phones": 1})]


def test_la_releve_sans_trouvaille_regle_a_zero(key, trace, client, releve):
    key["mode"] = "platform"
    client.return_value.get_enrichment.return_value = {
        "status": "finished", "phone": [], "phone_details": []}
    _fn()(op="result", enrichment_id=ID, find=["phone"])
    assert releve == [("regler", "pipecorn.get_enrichment", {"enrichment_id": ID},
                       {"phones": 0})]


@pytest.mark.parametrize("code, match", [
    ("insufficient_credits", "balance ran out"),
    ("workspace_limit_exceeded", "weekly credit limit"),
    ("", "no reason given"),
])
def test_un_echec_dit_pourquoi_et_ne_facture_rien(key, trace, client, releve, code, match):
    client.return_value.get_enrichment.return_value = {"status": "failed", "code": code}
    with pytest.raises(McpError, match=match):
        _fn()(op="result", enrichment_id=ID)
    assert trace == {"quantity": 0} and releve == []


def test_un_echec_sur_la_cle_partagee_libere_la_reservation(key, trace, client, releve):
    key["mode"] = "platform"
    client.return_value.get_enrichment.return_value = {
        "status": "failed", "code": "insufficient_credits"}
    with pytest.raises(McpError, match="balance ran out"):
        _fn()(op="result", enrichment_id=ID)
    assert trace == {"quantity": 0, "units": 0}
    assert releve == [("liberer", "pipecorn.get_enrichment", {"enrichment_id": ID})]


def test_la_releve_exige_l_identifiant(key, client):
    with pytest.raises(McpError, match="enrichment_id"):
        _fn()(op="result")


# --- credits / account ------------------------------------------------------

def test_solde_et_compte(key, client):
    assert _fn()(op="credits") == {"credits": {"enrichment": 2500}}
    assert _fn()(op="account") == {"account": {"plan": "growth"}}
    assert key["check_usage"] is False


@pytest.mark.parametrize("kwargs, name", [
    ({"op": "credits", "linkedin_url": URL}, "linkedin_url"),
    ({"op": "result", "enrichment_id": ID, "linkedin_url": URL}, "linkedin_url"),
    ({"enrichment_id": ID, "linkedin_url": URL}, "enrichment_id"),
])
def test_aucun_argument_ignore_en_silence(key, client, kwargs, name):
    with pytest.raises(McpError, match=f"does not take `{name}`"):
        _fn()(**kwargs)


# --- refus de Pipecorn ------------------------------------------------------

def _upstream(status, body):
    from oto.tools.common.errors import UpstreamHTTPError
    return UpstreamHTTPError(status_code=status, body=body, service="pipecorn")


@pytest.mark.parametrize("mode, status, body, match", [
    ("org", 402, {"code": "insufficient_credits"}, "Top up in Pipecorn"),
    ("platform", 402, {"code": "insufficient_credits"}, "shared key's credit balance is empty"),
    ("org", 403, {"code": "workspace_limit_exceeded"}, "weekly credit limit"),
    ("org", 401, {"error": "Invalid API key"}, "rejected the API key"),
    ("org", 422, {"error": "Linkedin URL is required"}, "Linkedin URL is required"),
    ("org", 500, {"error": "An error occurred during enrichment"}, "do not submit it again"),
])
def test_traduction_des_refus(key, trace, client, mode, status, body, match):
    key["mode"] = mode
    client.return_value.submit_enrichment.side_effect = _upstream(status, body)
    with pytest.raises(McpError, match=match):
        _fn()(linkedin_url=URL)
    assert trace == {}


def test_une_releve_inconnue_le_dit(key, trace, client):
    client.return_value.get_enrichment.side_effect = _upstream(404, {"error": "Resource not found"})
    with pytest.raises(McpError, match="no such lookup"):
        _fn()(op="result", enrichment_id=ID)


# --- sonde ------------------------------------------------------------------

def test_sonde(client):
    from oto_mcp.connectors import verify
    from oto_mcp.tools import pipecorn

    client.return_value.probe.return_value = {"credits": {"enrichment": 0}}
    with pytest.raises(verify.QuotaEpuise):
        pipecorn._verify({"key": KEY})
    client.return_value.probe.return_value = {"credits": {"enrichment": 12}}
    assert pipecorn._verify({"key": KEY}) == {"quota": {"restant": 12, "unite": "credits"}}
    client.return_value.probe.side_effect = _upstream(401, {"error": "Invalid API key"})
    with pytest.raises(verify.NonAutorise):
        pipecorn._verify({"key": KEY})


@pytest.mark.parametrize("answer", [{"credits": {}}, {"credits": {"enrichment": "?"}}, []])
def test_sonde_un_solde_illisible_est_une_panne(client, answer):
    from oto_mcp.connectors import verify
    from oto_mcp.tools import pipecorn

    client.return_value.probe.return_value = answer
    with pytest.raises(RuntimeError, match="readable enrichment balance") as e:
        pipecorn._verify({"key": KEY})
    assert not isinstance(e.value, (verify.NonAutorise, verify.QuotaEpuise))


def test_sonde_une_reponse_illisible_est_une_panne(client):
    from oto.tools.pipecorn import PipecornUnreadable
    from oto_mcp.connectors import verify
    from oto_mcp.tools import pipecorn

    client.return_value.probe.side_effect = PipecornUnreadable("not JSON")
    with pytest.raises(RuntimeError, match="unreadable answer") as e:
        pipecorn._verify({"key": KEY})
    assert not isinstance(e.value, verify.NonAutorise)


# --- person / company / stack / jobs : synchrones, sur la clé de l'appelant ------------

def _lookup_args(op):
    return {"linkedin_url": URL} if op == "person" else {"domain": "acme.com"}


@pytest.mark.parametrize("op, method, answer, credits", [
    ("person", "enrich_person", {"full_name": "Jane Doe", "positions": []}, 1),
    ("company", "enrich_account", {"company_name": "Acme", "domain": "acme.com"}, 1),
    ("stack", "company_stack", {"technologies": ["HubSpot"]}, 3),
    ("jobs", "company_hiring", {"total_jobs": 2, "jobs": [{"job_title": "AE"}] * 2}, 2),
])
def test_une_recherche_synchrone_trace_un_trouve_et_ses_credits(key, trace, client, op,
                                                                method, answer, credits):
    getattr(client.return_value, method).return_value = answer
    out = _fn()(op=op, **_lookup_args(op))
    assert key["check_usage"] is False
    assert trace == {"quantity": 1, "provider_credits": credits}
    assert out["found"] is True and out["pipecorn_credits"] == credits


@pytest.mark.parametrize("op, method, answer, credits", [
    ("person", "enrich_person", {"positions": []}, 0),
    ("company", "enrich_account", {}, 0),
    # Facturée à la requête, trouvée ou non.
    ("stack", "company_stack", {"technologies": []}, 3),
    ("jobs", "company_hiring", {"total_jobs": 0, "jobs": []}, 0),
])
def test_une_recherche_synchrone_vide_trace_un_zero(key, trace, client, op, method, answer,
                                                    credits):
    getattr(client.return_value, method).return_value = answer
    out = _fn()(op=op, **_lookup_args(op))
    assert trace == {"quantity": 0, "provider_credits": credits}
    assert out["found"] is False


@pytest.mark.parametrize("op, method", [
    ("person", "enrich_person"), ("company", "enrich_account"),
    ("stack", "company_stack"), ("jobs", "company_hiring")])
def test_un_404_est_un_non_trouve_qui_ne_coute_rien(key, trace, client, op, method):
    getattr(client.return_value, method).side_effect = _upstream(404, {"error": "not found"})
    out = _fn()(op=op, **_lookup_args(op))
    assert out == {"found": False, "pipecorn_credits": 0}
    assert trace == {"quantity": 0, "provider_credits": 0}


@pytest.mark.parametrize("mode", ["platform", "tenant"])
@pytest.mark.parametrize("op, method", [
    ("person", "enrich_person"), ("company", "enrich_account"),
    ("stack", "company_stack"), ("jobs", "company_hiring")])
def test_une_recherche_synchrone_hors_de_la_cle_de_l_appelant_est_refusee(key, trace, client,
                                                                          mode, op, method):
    """Hors du budget d'unités : refusée AVANT tout envoi sur une clé partagée."""
    key["mode"] = mode
    with pytest.raises(McpError, match="needs your own Pipecorn key") as e:
        _fn()(op=op, **_lookup_args(op))
    assert "credit budget" in str(e.value)
    getattr(client.return_value, method).assert_not_called()
    assert trace == {}


def test_aucun_contact_ne_sort_d_une_recherche_synchrone(key, trace, client):
    """Un profil qui porterait un email personnel, un téléphone, ou une équipe d'offre qui
    en porterait : retirés de la sortie, l'email personnel d'un tiers d'abord."""
    client.return_value.enrich_person.return_value = {
        "full_name": "Jane Doe", "personal_email": "jane@example.test",
        "personal_emails": ["jane@example.test"], "email": "jane@acme.example",
        "mobile_phone": "+33600000000",
        "positions": [{"title": "VP Sales", "current": True, "work_email": "j@acme.example"}]}
    out = _fn()(op="person", linkedin_url=URL)
    assert out == {"full_name": "Jane Doe", "positions": [{"title": "VP Sales",
                                                           "current": True}],
                   "found": True, "pipecorn_credits": 1}
    client.return_value.company_hiring.return_value = {"jobs": [{
        "job_title": "AE", "hiring_team": [{"name": "John Roe", "email": "john@acme.example",
                                            "phone": "+15550000000"}]}]}
    out = _fn()(op="jobs", domain="acme.com")
    assert out["jobs"] == [{"job_title": "AE", "hiring_team": [{"name": "John Roe"}]}]
    client.return_value.enrich_account.return_value = {"company_name": "Acme",
                                                       "phone": "+33100000000",
                                                       "emails": ["contact@acme.example"]}
    assert _fn()(op="company", domain="acme.com") == {
        "company_name": "Acme", "found": True, "pipecorn_credits": 1}


def test_les_offres_passent_leurs_filtres(key, trace, client):
    _fn()(op="jobs", company_name="Acme", posted_within_days=90, limit=10,
          job_title_keywords=["sales"], excluded_job_title_keywords=["intern"],
          job_country_codes=["FR"])
    kw = client.return_value.company_hiring.call_args.kwargs
    assert kw["name"] == "Acme" and kw["posted_within_days"] == 90 and kw["limit"] == 10
    assert kw["job_title_keywords"] == ["sales"] and kw["job_country_codes"] == ["FR"]
    assert kw["excluded_job_title_keywords"] == ["intern"]


def test_la_societe_par_sa_page_linkedin_et_son_pays(key, trace, client):
    _fn()(op="company", company_name="Acme", country_code="FR",
          company_linkedin_url="https://www.linkedin.com/company/acme/")
    kw = client.return_value.enrich_account.call_args.kwargs
    assert kw == {"domain": None, "name": "Acme",
                  "linkedin_url": "https://www.linkedin.com/company/acme/",
                  "country_code": "FR", "timeout": (10, 60)}


@pytest.mark.parametrize("kwargs, name", [
    ({"op": "company", "domain": "acme.com", "limit": 5}, "limit"),
    ({"op": "person", "linkedin_url": URL, "domain": "acme.com"}, "domain"),
    ({"op": "stack", "domain": "acme.com", "country_code": "FR"}, "country_code"),
    ({"find": ["phone"], "linkedin_url": URL, "company_linkedin_url": URL},
     "company_linkedin_url"),
    ({"find": ["phone"], "linkedin_url": URL, "timeout_s": 20}, "timeout_s"),
    ({"op": "result", "enrichment_id": ID, "timeout_s": 20}, "timeout_s"),
    ({"op": "credits", "timeout_s": 20}, "timeout_s"),
])
def test_un_argument_hors_de_son_op_est_refuse(key, client, kwargs, name):
    with pytest.raises(McpError, match=f"does not take `{name}`"):
        _fn()(**kwargs)


def test_les_offres_sans_description_par_defaut(key, trace, client):
    client.return_value.company_hiring.return_value = {
        "total_jobs": 1, "jobs": [{"job_title": "AE", "description": "long text"}]}
    out = _fn()(op="jobs", domain="acme.com")
    assert out["jobs"] == [{"job_title": "AE"}]
    out = _fn()(op="jobs", domain="acme.com", full_description=True)
    assert out["jobs"][0]["description"] == "long text"


def test_une_entree_de_recherche_manquante_est_un_refus_nomme(key, trace):
    # Le vrai client : son refus local devient un INVALID_PARAMS, rien n'est envoyé.
    with pytest.raises(McpError, match="company_name"):
        _fn()(op="person", firstname="Jane", lastname="Doe")
    assert trace == {}


# --- le délai d'une recherche synchrone ------------------------------------------

@pytest.mark.parametrize("op, method", [
    ("person", "enrich_person"), ("company", "enrich_account"),
    ("stack", "company_stack"), ("jobs", "company_hiring")])
def test_timeout_s_borne_l_attente_du_client(key, trace, client, op, method):
    """`timeout_s` (le temps qui reste à une cascade) devient `(connexion, lecture)` ;
    sans lui, le délai propre du client."""
    from oto.tools.pipecorn.client import HTTP_TIMEOUT
    _fn()(op=op, **_lookup_args(op), timeout_s=23)
    assert getattr(client.return_value, method).call_args.kwargs["timeout"] == (10, 23)
    _fn()(op=op, **_lookup_args(op), timeout_s=4)
    assert getattr(client.return_value, method).call_args.kwargs["timeout"] == (4, 4)
    _fn()(op=op, **_lookup_args(op))
    assert getattr(client.return_value, method).call_args.kwargs["timeout"] == HTTP_TIMEOUT


@pytest.mark.parametrize("error", ["ReadTimeout", "ConnectTimeout"])
@pytest.mark.parametrize("op, method", [
    ("person", "enrich_person"), ("company", "enrich_account"),
    ("stack", "company_stack"), ("jobs", "company_hiring")])
def test_un_depassement_est_un_refus_nomme_jamais_un_succes(key, trace, client, op, method,
                                                           error):
    """Envoyée, sans réponse à temps : ni trouvé ni non-trouvé, un refus nommé qui dit
    que Pipecorn a pu facturer ; rien n'est tracé comme trouvé."""
    import requests
    getattr(client.return_value, method).side_effect = getattr(requests.exceptions, error)()
    with pytest.raises(McpError) as e:
        _fn()(op=op, **_lookup_args(op), timeout_s=12)
    assert e.value.error.data["code"] == "pipecorn_timeout"
    assert "no answer within 12 s" in e.value.error.message
    assert "may have billed the lookup" in e.value.error.message
    assert trace == {}


@pytest.mark.parametrize("bad", [0, -3, True])
def test_un_timeout_s_qui_n_est_pas_un_delai_est_refuse(key, client, bad):
    with pytest.raises(McpError, match="`timeout_s` must be a positive number"):
        _fn()(op="company", domain="acme.com", timeout_s=bad)
    client.return_value.enrich_account.assert_not_called()
