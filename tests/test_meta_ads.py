"""Le connecteur `meta_ads` — campagnes Meta, en lecture, par la Marketing API.

⚠️ Le cœur est MOQUÉ à sa frontière (`oto.tools.meta_ads` posé dans
`sys.modules`) : le venv porte oto-core au tag épinglé, qui ne contient pas encore
ce paquet (même raison que `test_instagram_meta_natif.py`). Vérifié ici : le
registre, le flux hébergé, le coffre, la traduction des refus, et le rapport
asynchrone borné dans le temps.

⚠️ Aucun appel réel à Meta n'est joué ici.
"""
from __future__ import annotations

import asyncio
import sys
import types
from unittest.mock import MagicMock

import pytest

from oto_mcp import providers

CONNECTEUR = "meta_ads"
ORG = 42
SUB = "user-de-test"
MEMBRE = f"{ORG}:{SUB}"
_COORDONNEES = {"app_id": "app-id-fictif", "app_secret": "secret-fictif",
                "config_id": "config-fictive"}


def _faux_coeur():
    mod = types.ModuleType("oto.tools.meta_ads")

    class MetaAdsError(RuntimeError):
        pass

    class MetaAdsAuthExpired(MetaAdsError):
        pass

    class MetaAdsApiError(MetaAdsError):
        pass

    class MetaAdsThrottled(MetaAdsApiError):
        pass

    mod.MetaAdsError = MetaAdsError
    mod.MetaAdsAuthExpired = MetaAdsAuthExpired
    mod.MetaAdsApiError = MetaAdsApiError
    mod.MetaAdsThrottled = MetaAdsThrottled
    mod.MetaAdsApp = MagicMock(name="MetaAdsApp")
    mod.MetaAdsClient = MagicMock(name="MetaAdsClient")
    mod.authorize_url = MagicMock(
        return_value="https://www.facebook.com/v25.0/dialog/oauth?x=1")
    mod.connect = MagicMock()
    return mod


class _Coffre:
    def __init__(self):
        self.lignes: dict[tuple, dict] = {}
        self.rejets: list[tuple] = []

    def poser(self, secret, meta=None):
        self.lignes[("member", MEMBRE, "")] = {
            "secret": secret, "meta": dict(meta or {}), "set_at": "2026-10-02T00:00:00Z"}

    def get(self, entity_type, entity_id, connector, account=""):
        assert connector == CONNECTEUR
        ligne = self.lignes.get((entity_type, entity_id, account))
        return dict(ligne) if ligne else None

    def set(self, entity_type, entity_id, connector, secret, set_by=None,
            meta=None, conn=None, account="", expected_version=None):
        assert connector == CONNECTEUR
        self.lignes[(entity_type, entity_id, account)] = {
            "secret": secret, "meta": dict(meta or {}), "set_by": set_by}

    def marquer(self, entity_type, entity_id, provider, account, error):
        self.rejets.append((entity_id, account, error))


@pytest.fixture
def env(monkeypatch):
    from oto_mcp import credentials_store
    from oto_mcp.auth import meta_ads as ads_auth
    from oto_mcp.connectors import health as connector_health
    from oto_mcp.db import connector_settings as store

    mod = _faux_coeur()
    monkeypatch.setitem(sys.modules, "oto.tools.meta_ads", mod)
    monkeypatch.setattr(store, "list_connector_settings",
                        lambda key=None, conn=None: [
                            {"scope_type": "platform", "scope_id": "platform",
                             "connector": CONNECTEUR, "key": k, "value": v}
                            for k, v in _COORDONNEES.items()])
    coffre = _Coffre()
    monkeypatch.setattr(credentials_store, "get_credential_with_meta", coffre.get)
    monkeypatch.setattr(credentials_store, "set_credential", coffre.set)
    monkeypatch.setattr(connector_health, "mark_rejected", coffre.marquer)
    monkeypatch.setattr("oto_mcp.access.current_org", lambda sub: ORG)
    monkeypatch.setattr("oto_mcp.access.current_user_sub_or_raise", lambda: SUB)
    monkeypatch.setenv("OTO_MCP_PUBLIC_URL", "https://mcp.exemple.test")
    monkeypatch.setenv("OTO_MCP_OAUTH_STATE_SECRET", "secret-de-signature-de-test")
    return types.SimpleNamespace(coeur=mod, coffre=coffre, auth=ads_auth)


# ── Registre ────────────────────────────────────────────────────────────────

def test_registre():
    c = providers.REGISTRY[CONNECTEUR]
    assert c.secret_kind == "oauth" and c.auth_modes == frozenset({"byo_user"})
    assert c.publisher_name == "Meta"
    assert "Read-only" in c.help
    assert c.doc_sections, "la fiche doit être servie depuis son markdown"


def test_namespace_ne_tombe_pas_ailleurs():
    from oto_mcp import tool_visibility as tv

    for nom in ("meta_ads_accounts", "meta_ads_objects", "meta_ads_insights"):
        assert tv.namespace_of(nom) == CONNECTEUR


def test_la_fiche_dit_l_acces_standard_et_les_trois_reglages():
    corps = " ".join(s.body_md for s in providers.REGISTRY[CONNECTEUR].doc_sections)
    assert "standard access" in corps
    for cle in _COORDONNEES:
        assert f'key="{cle}"' in corps


# ── Flux hébergé ────────────────────────────────────────────────────────────

def test_url_de_retour_derivee_de_l_environnement(env):
    from oto_mcp.connectors import flow as connector_flow

    assert connector_flow.supports(CONNECTEUR)
    assert (connector_flow.callback_url(CONNECTEUR)
            == "https://mcp.exemple.test/api/meta_ads/oauth/callback")


def test_state_ne_vaut_que_pour_ce_flux(env):
    from oto_mcp.auth import flow as oauth_flow

    etat = env.auth.make_state(SUB, ORG, "")
    assert env.auth.verify_state(etat) == (SUB, ORG, "")
    assert env.auth.verify_state(oauth_flow.sign_state(
        "instagram_meta", {"sub": SUB, "org": ORG})) is None


def test_sans_reglages_le_refus_nomme_la_cle(env, monkeypatch):
    from oto_mcp.db import connector_settings as store

    monkeypatch.setattr(store, "list_connector_settings",
                        lambda key=None, conn=None: [])
    assert env.auth.coordonnees_manquantes() == ["app_id", "app_secret", "config_id"]
    assert env.auth.app_disponible(SUB) is False
    with pytest.raises(RuntimeError) as e:
        env.auth.app()
    assert "config_id" in str(e.value) and "oto_admin_connector_setting" in str(e.value)


def test_le_dialogue_part_avec_l_application_de_l_instance(env):
    env.auth.build_auth_url(SUB, "")
    _app, retour, etat = env.coeur.authorize_url.call_args.args
    assert retour == "https://mcp.exemple.test/api/meta_ads/oauth/callback"
    assert env.auth.verify_state(etat) == (SUB, ORG, "")
    env.coeur.MetaAdsApp.assert_called_with(**_COORDONNEES)


def test_seul_app_secret_est_secret_et_suit_la_convention():
    from oto_mcp.auth import meta_ads as ads_auth
    from oto_mcp.capabilities import platform_connectors as pc

    assert [k for k in ads_auth._REGLAGES if "secret" in k] == ["app_secret"]
    lignes = pc._sans_les_secrets([
        {"connector": CONNECTEUR, "key": k, "value": v} for k, v in _COORDONNEES.items()])
    assert "secret-fictif" not in str(lignes)
    assert {"app-id-fictif", "config-fictive"} <= {l["value"] for l in lignes}


# ── Coffre ──────────────────────────────────────────────────────────────────

def test_jeton_bisu_sans_echeance(env):
    grant = types.SimpleNamespace(access_token="jeton", expires_in=None,
                                  user_id="su1", name="Acme",
                                  client_business_id="biz9")
    out = env.auth.persist_grant(SUB, ORG, grant)
    ligne = env.coffre.lignes[("member", MEMBRE, "")]
    assert ligne["secret"] == "jeton"
    assert ligne["meta"]["client_business_id"] == "biz9"
    assert "expires_at" not in ligne["meta"] and out["expires_at"] is None


def test_jeton_a_duree_de_vie_garde_son_echeance(env):
    grant = types.SimpleNamespace(access_token="jeton", expires_in=5_184_000,
                                  user_id="u1", name="Jane", client_business_id="")
    out = env.auth.persist_grant(SUB, ORG, grant)
    assert out["expires_at"] == env.coffre.lignes[("member", MEMBRE, "")]["meta"][
        "expires_at"]


def test_statut_de_la_fiche(env):
    assert env.auth._etape_manquante(SUB, None, None, {}) == "Authorize oto on Facebook"
    env.coffre.poser("jeton", {"health_ko": True})
    assert "reconnect" in env.auth._etape_manquante(SUB, None, None, {})


# ── Outils ──────────────────────────────────────────────────────────────────

def _serveur():
    from fastmcp import FastMCP
    from oto_mcp.tools import meta_ads

    m = FastMCP("t")
    meta_ads.register(m)
    return m


def _outil(nom):
    return asyncio.run(_serveur().get_tool(nom)).fn


def _client(env, **methodes):
    client = MagicMock()
    for nom, valeur in methodes.items():
        setattr(client, nom, valeur)
    env.coeur.MetaAdsClient = MagicMock(return_value=client)
    return client


def test_les_trois_outils_et_leurs_descriptions(env):
    outils = asyncio.run(_serveur().list_tools())
    assert sorted(t.name for t in outils) == [
        "meta_ads_accounts", "meta_ads_insights", "meta_ads_objects"]
    assert all(t.description for t in outils)


def test_sans_compte_connecte_le_refus_dit_le_geste(env):
    with pytest.raises(Exception) as e:
        asyncio.run(_outil("meta_ads_accounts")())
    assert "No Meta Ads account connected" in str(e.value)


def test_accounts_rend_le_brut(env):
    env.coffre.poser("jeton")
    brut = {"data": [{"id": "act_1"}], "next_cursor": None}
    client = _client(env, list_ad_accounts=MagicMock(return_value=brut))
    assert asyncio.run(_outil("meta_ads_accounts")(limit=10)) == brut
    client.list_ad_accounts.assert_called_once_with(10, None, None)
    env.coeur.MetaAdsClient.assert_called_once_with("jeton")


def test_objects_exige_un_seul_des_deux_ids(env):
    env.coffre.poser("jeton")
    _client(env)
    fn = _outil("meta_ads_objects")
    for kwargs in ({}, {"ad_account_id": "1", "object_id": "2"}):
        with pytest.raises(Exception, match="exactly one"):
            asyncio.run(fn(**kwargs))


def test_objects_liste_avec_le_curseur(env):
    env.coffre.poser("jeton")
    client = _client(env, list_objects=MagicMock(return_value={"data": []}))
    asyncio.run(_outil("meta_ads_objects")(level="adset", ad_account_id="act_1",
                                           effective_status=["ACTIVE"], cursor="C"))
    args, kwargs = client.list_objects.call_args
    assert args == ("act_1", "adset")
    assert kwargs["effective_status"] == ["ACTIVE"] and kwargs["after"] == "C"


def test_insights_sync_construit_la_fenetre(env):
    env.coffre.poser("jeton")
    client = _client(env, get_insights=MagicMock(return_value={"data": []}))
    asyncio.run(_outil("meta_ads_insights")(
        object_id="act_1", level="campaign", since="2026-09-01", until="2026-09-30"))
    kwargs = client.get_insights.call_args.kwargs
    assert kwargs["time_range"] == {"since": "2026-09-01", "until": "2026-09-30"}
    assert kwargs["level"] == "campaign"


def test_insights_fenetre_incomplete_refusee(env):
    env.coffre.poser("jeton")
    _client(env)
    with pytest.raises(Exception, match="both since and until"):
        asyncio.run(_outil("meta_ads_insights")(object_id="act_1", since="2026-09-01"))


def test_insights_async_termine_rend_les_lignes(env):
    env.coffre.poser("jeton")
    client = _client(
        env,
        start_insights_report=MagicMock(return_value="777"),
        get_report_status=MagicMock(return_value={"async_status": "Job Completed"}),
        get_report_insights=MagicMock(return_value={"data": [{"spend": "1"}],
                                                    "next_cursor": None}))
    out = asyncio.run(_outil("meta_ads_insights")(object_id="act_1", mode="async"))
    assert out["report_run_id"] == "777" and out["data"] == [{"spend": "1"}]
    client.get_report_insights.assert_called_once_with("777", 100, None)


def test_insights_async_long_rend_de_quoi_reprendre(env, monkeypatch):
    from oto_mcp.tools import meta_ads

    monkeypatch.setattr(meta_ads, "_ATTENTE_RAPPORT_S", 0.0)
    env.coffre.poser("jeton")
    _client(env,
            start_insights_report=MagicMock(return_value="777"),
            get_report_status=MagicMock(return_value={
                "async_status": "Job Running", "async_percent_completion": 40}))
    out = asyncio.run(_outil("meta_ads_insights")(object_id="act_1", mode="async"))
    assert out["report_run_id"] == "777" and out["percent_complete"] == 40
    assert "report_run_id" in out["hint"]


def test_insights_rapport_en_echec(env):
    env.coffre.poser("jeton")
    _client(env, get_report_status=MagicMock(return_value={"async_status": "Job Failed"}))
    with pytest.raises(Exception, match="Job Failed"):
        asyncio.run(_outil("meta_ads_insights")(report_run_id="777"))


def test_jeton_rejete_marque_la_ligne_et_demande_de_reconnecter(env):
    env.coffre.poser("jeton")
    _client(env, list_ad_accounts=MagicMock(
        side_effect=env.coeur.MetaAdsAuthExpired("dead")))
    with pytest.raises(Exception) as e:
        asyncio.run(_outil("meta_ads_accounts")())
    assert "Reconnect" in str(e.value)
    assert env.coffre.rejets and env.coffre.rejets[0][0] == MEMBRE


def test_debit_atteint_ne_marque_rien(env):
    env.coffre.poser("jeton")
    _client(env, list_ad_accounts=MagicMock(
        side_effect=env.coeur.MetaAdsThrottled("Meta rate limit reached")))
    with pytest.raises(Exception, match="rate limit"):
        asyncio.run(_outil("meta_ads_accounts")())
    assert env.coffre.rejets == []


def test_panne_inconnue_ne_laisse_pas_fuir_le_message(env):
    env.coffre.poser("jeton")
    _client(env, list_ad_accounts=MagicMock(
        side_effect=ConnectionError("https://graph.facebook.com/?access_token=jeton")))
    with pytest.raises(Exception) as e:
        asyncio.run(_outil("meta_ads_accounts")())
    assert "access_token" not in str(e.value) and "ConnectionError" in str(e.value)


def test_sans_coeur_installe_refuse_en_le_disant(env, monkeypatch):
    monkeypatch.delitem(sys.modules, "oto.tools.meta_ads")
    monkeypatch.setattr(
        "importlib.import_module",
        lambda nom, *a, **k: (_ for _ in ()).throw(ImportError("absent")))
    with pytest.raises(RuntimeError, match="oto-core"):
        env.auth._coeur()


def test_les_arguments_passent_la_validation_du_schema(env):
    """Par `call_tool`, pas `.fn` : c'est la validation pydantic qu'on teste. Un
    agent qui suit le guide envoie `time_increment=1` (un entier) et des
    `fields`/`breakdowns` en chaîne — refusés avant d'atteindre le code sinon."""
    env.coffre.poser("jeton")
    client = _client(env, get_insights=MagicMock(return_value={"data": []}))
    for args in ({"time_increment": 1}, {"time_increment": "monthly"},
                 {"fields": "spend,clicks", "breakdowns": "age"},
                 {"fields": ["spend"], "breakdowns": ["age", "gender"]}):
        asyncio.run(_serveur().call_tool(
            "meta_ads_insights", {"object_id": "act_1", **args}))
    assert client.get_insights.call_count == 4
    assert client.get_insights.call_args_list[0].kwargs["time_increment"] == 1
    with pytest.raises(Exception):
        asyncio.run(_serveur().call_tool(
            "meta_ads_insights", {"object_id": "act_1", "time_increment": 400}))
