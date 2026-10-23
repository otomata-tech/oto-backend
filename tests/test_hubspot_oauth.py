"""« Connect HubSpot » — the OAuth flow next to the pasted key (`auth/hubspot.py`).

Checked here: each level connects with ITS app (personal → user-level, org/team →
account-level) and the consent asks for that app's scopes (and each app config
declares the same), the state is bound to its flow, its app and single-use, the return writes
the refresh token with `meta.auth = "oauth"`, `bearer()` hands the tools a pasted key
as is and mints (then caches) an access token for an OAuth row, a rotated refresh
token is written back, a dead grant marks the row, and the probe of an OAuth row
introspects instead of calling the pasted-key endpoints.

⚠️ No real call to HubSpot: `_post` and `oauth_flow.exchange_code` are faked.
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import types
from urllib.parse import parse_qs, urlencode, urlparse

import pytest

ORG = 42
SUB = "user-de-test"
MEMBRE = f"{ORG}:{SUB}"
ROOT = pathlib.Path(__file__).resolve().parents[1]


class _Reponse:
    def __init__(self, status: int, body: dict):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


class _Coffre:
    def __init__(self):
        self.lignes: dict[tuple, dict] = {}
        self.rejets: list[tuple] = []

    def get(self, entity_type, entity_id, connector, account=""):
        assert connector == "hubspot"
        ligne = self.lignes.get((entity_type, entity_id, account))
        return dict(ligne) if ligne else None

    def set(self, entity_type, entity_id, connector, secret, set_by=None,
            meta=None, conn=None, account="", expected_version=None):
        assert connector == "hubspot"
        self.lignes[(entity_type, entity_id, account)] = {
            "secret": secret, "meta": dict(meta or {}), "set_by": set_by}

    def marquer(self, entity_type, entity_id, provider, account, error, verdict=None):
        self.rejets.append((entity_type, entity_id, account, verdict))


@pytest.fixture
def env(monkeypatch):
    from oto_mcp import credentials_store
    from oto_mcp.auth import hubspot as hs
    from oto_mcp.connectors import health as connector_health
    from oto_mcp.db import connector_settings as store

    monkeypatch.setattr(store, "list_connector_settings",
                        lambda key=None, conn=None: [
                            {"scope_type": "platform", "scope_id": "platform",
                             "connector": "hubspot", "key": k, "value": v}
                            for k, v in (("client_id", "cid"), ("client_secret", "csec"),
                                         ("user_client_id", "ucid"),
                                         ("user_client_secret", "ucsec"))])
    coffre = _Coffre()
    monkeypatch.setattr(credentials_store, "get_credential_with_meta", coffre.get)
    monkeypatch.setattr(credentials_store, "set_credential", coffre.set)
    monkeypatch.setattr(connector_health, "mark_rejected", coffre.marquer)
    monkeypatch.setattr("oto_mcp.access.current_org", lambda sub: ORG)
    monkeypatch.setattr("oto_mcp.access.current_group", lambda sub: None)
    monkeypatch.setattr("oto_mcp.roles.is_org_admin", lambda sub, org: False)
    monkeypatch.setattr("oto_mcp.auth.flow.consume_state", lambda aud, data: True)
    monkeypatch.setenv("OTO_MCP_PUBLIC_URL", "https://mcp.exemple.test")
    monkeypatch.setenv("OTO_MCP_OAUTH_STATE_SECRET", "secret-de-signature-de-test")
    appels: list[tuple[str, dict]] = []
    reponses: dict[str, list] = {}

    def faux_post(url, data):
        appels.append((url, dict(data)))
        file = reponses.get(url) or []
        assert file, f"no fake answer for {url}"
        return file.pop(0)

    monkeypatch.setattr(hs, "_post", faux_post)
    hs._TOKENS.clear()
    # `bearer()` records which shape served the call in a ContextVar: in production a
    # call's context ends with it; a test run shares one, so it is put back here.
    jeton = hs._SERVED_BY.set("")
    jeton_owner = hs._OWNER.set("")
    owners: list[tuple] = []

    def faux_owners(email, token):
        owners.append((email, token))
        return "owner-7"

    monkeypatch.setattr(hs, "owner_id_of", lambda token, email: faux_owners(email, token))
    yield types.SimpleNamespace(hs=hs, coffre=coffre, appels=appels, reponses=reponses,
                                owners=owners)
    hs._SERVED_BY.reset(jeton)
    hs._OWNER.reset(jeton_owner)


def _introspection(**extra):
    return _Reponse(200, {"active": True, "hub_id": 123, "hub_domain": "portal.example",
                          "user": "someone@example.test", "token_use": "refresh_token",
                          "scopes": ["crm.objects.contacts.read", "crm.lists.read"],
                          **extra})


# ── Scopes ──────────────────────────────────────────────────────────────────

def test_les_scopes_viennent_de_la_reference():
    from oto_mcp.auth import hubspot as hs
    from oto_mcp.tools.hubspot_scopes import SCOPES, known_scope

    org_req, org_opt = hs.SCOPES_BY_APP[hs.APP_ACCOUNT]
    perso_req, perso_opt = hs.SCOPES_BY_APP[hs.APP_USER]
    for fam in ("contacts", "companies", "deals"):
        read, write = SCOPES[fam]
        for req in (org_req, perso_req):
            assert read[0] in req and write[0] in req
    for req in (org_req, perso_req):
        assert "crm.objects.owners.read" in req   # the personal owner lookup needs it
    for req, opt in ((org_req, org_opt), (perso_req, perso_opt)):
        assert not set(req) & set(opt)
        assert all(known_scope(s) for s in (*req, *opt))
        # Events and tickets optional: a portal without them still installs.
        assert "business-intelligence" in opt and "crm.objects.tickets.read" in opt
        assert "crm.objects.custom.read" in opt
    # Lists: required for the org, optional for a person (reps often lack the right).
    assert "crm.lists.write" in org_req and "crm.lists.write" in perso_opt
    # Writing fields: an admin's gesture, the org app only.
    assert "crm.schemas.contacts.write" in org_opt
    assert not any(".write" in s and "schemas" in s for s in (*perso_req, *perso_opt))


@pytest.mark.parametrize("dossier,kind,user_level", [
    ("org", "account", False), ("personal", "user", True)])
def test_la_config_de_chaque_app_declare_ses_scopes(dossier, kind, user_level):
    """HubSpot refuses an authorize URL whose required scopes the app does not
    declare: each uploaded config and the code must be the same lists."""
    from oto_mcp.auth import hubspot as hs

    cfg = json.loads((ROOT / f"docs/hubspot-app/{dossier}/src/app/app-hsmeta.json")
                     .read_text())
    assert cfg["config"]["isUserLevel"] is user_level
    auth = cfg["config"]["auth"]
    required, optional = hs.SCOPES_BY_APP[kind]
    assert auth["type"] == "oauth"
    assert auth["requiredScopes"] == list(required)
    assert auth["optionalScopes"] == list(optional)
    assert all(u.endswith(hs.CALLBACK_PATH) for u in auth["redirectUrls"])


# ── Start ───────────────────────────────────────────────────────────────────

def _consent(env, scope):
    url = urlparse(env.hs.build_auth_url(SUB, scope))
    return url, {k: v[0] for k, v in parse_qs(url.query).items()}


def test_le_consentement_perso_part_avec_l_app_user_level(env):
    url, q = _consent(env, "member")
    assert f"{url.scheme}://{url.netloc}{url.path}" == env.hs.AUTHORIZE_URL
    assert q["client_id"] == "ucid"
    assert q["redirect_uri"] == "https://mcp.exemple.test/api/hubspot/oauth/callback"
    required, optional = env.hs.SCOPES_BY_APP["user"]
    assert q["scope"].split() == list(required)
    assert q["optional_scope"].split() == list(optional)
    data = env.hs.read_state(q["state"])
    assert data["sub"] == SUB and data["org"] == ORG and data["scope"] == "member"
    assert data["kind"] == "user" and data["jti"]
    assert "csec" not in url.query and "ucsec" not in url.query   # never in a URL


def test_le_consentement_org_part_avec_l_app_account_level(env, monkeypatch):
    monkeypatch.setattr("oto_mcp.roles.is_org_admin", lambda sub, org: True)
    _, q = _consent(env, "org")
    assert q["client_id"] == "cid"
    required, optional = env.hs.SCOPES_BY_APP["account"]
    assert q["scope"].split() == list(required)
    assert q["optional_scope"].split() == list(optional)
    assert env.hs.read_state(q["state"])["kind"] == "account"


def test_un_state_dont_l_app_ne_suit_pas_le_niveau_est_refuse(env):
    """A personal level signed with the org app (or the reverse) would store a grant
    at the wrong level: refused, whatever signed it."""
    from oto_mcp.auth import flow as oauth_flow

    for scope, kind in (("member", "account"), ("org", "user"), ("member", None)):
        payload = {"sub": SUB, "org": ORG, "scope": scope, "app": "", "jti": "j"}
        if kind:
            payload["kind"] = kind
        assert env.hs.read_state(oauth_flow.sign_state("hubspot", payload)) is None


def test_le_state_ne_vaut_que_pour_ce_flux(env):
    from oto_mcp.auth import flow as oauth_flow

    autre = oauth_flow.sign_state("meta_ads", {"sub": SUB, "org": ORG, "scope": "member"})
    assert env.hs.read_state(autre) is None
    assert env.hs.read_state("faux") is None


def test_pour_l_org_il_faut_etre_admin(env):
    with pytest.raises(PermissionError):
        env.hs.build_auth_url(SUB, "org")


def _reglages(monkeypatch, **valeurs):
    from oto_mcp.db import connector_settings as store

    monkeypatch.setattr(store, "list_connector_settings", lambda key=None, conn=None: [
        {"scope_type": "platform", "scope_id": "platform", "connector": "hubspot",
         "key": k, "value": v} for k, v in valeurs.items()])


def test_sans_app_le_refus_nomme_le_reglage(env, monkeypatch):
    _reglages(monkeypatch)
    with pytest.raises(env.hs.HubSpotOAuthError,
                       match="personal HubSpot app.*user_client_id, user_client_secret"):
        env.hs.build_auth_url(SUB, "member")
    assert env.hs.app_ready(SUB) is False
    assert env.hs.app_ready_scopes(SUB) == []


def test_chaque_onglet_suit_son_app(env, monkeypatch):
    """Only the org app set: "Connect" on the org and team tabs, the form on personal
    — and the personal start names the missing settings."""
    from oto_mcp.connectors import flow as connector_flow

    _reglages(monkeypatch, client_id="cid", client_secret="csec")
    assert env.hs.app_ready_scopes(SUB) == ["org", "group"]
    assert connector_flow.readiness("hubspot", SUB) == (True, ["org", "group"])
    with pytest.raises(env.hs.HubSpotOAuthError, match="user_client_id"):
        env.hs.build_auth_url(SUB, "member")
    _reglages(monkeypatch, user_client_id="ucid", user_client_secret="ucsec")
    assert connector_flow.readiness("hubspot", SUB) == (True, ["member"])


def test_le_flux_est_declare_avec_sa_promesse(env):
    from oto_mcp.connectors import flow as connector_flow

    assert connector_flow.supports("hubspot")
    assert connector_flow.entries()["hubspot"].app_ready(SUB) is True
    assert connector_flow.readiness("hubspot", SUB) == (True, ["member", "org", "group"])
    # A connector that does not answer per level keeps the single answer.
    assert connector_flow.readiness("pas-un-connecteur", SUB) == (None, None)


# ── Return ──────────────────────────────────────────────────────────────────

def _callback(query: dict):
    from starlette.requests import Request

    from oto_mcp.api import hubspot as api_hubspot

    route = api_hubspot.make_routes(None, None, None, None, None)[0]
    req = Request({"type": "http", "method": "GET", "path": route.path,
                   "query_string": urlencode(query).encode(), "headers": []})
    return asyncio.run(route.endpoint(req))


def test_le_retour_perso_range_le_refresh_token_le_portail_et_le_owner(env, monkeypatch):
    from oto_mcp.auth import flow as oauth_flow

    echanges: list[dict] = []
    monkeypatch.setattr(oauth_flow, "exchange_code", lambda url, **k: (
        echanges.append(k) or {"access_token": "at-1", "refresh_token": "rt-1",
                               "expires_in": 1800}))
    env.reponses[env.hs.INTROSPECT_URL] = [_introspection()]
    state = env.hs.make_state(SUB, ORG, "member")
    resp = _callback({"code": "c0de", "state": state})
    assert resp.headers["location"].endswith("?connector=hubspot&connect=connected")
    ligne = env.coffre.lignes[("member", MEMBRE, "")]
    assert ligne["secret"] == "rt-1"
    assert ligne["meta"]["auth"] == "oauth" and ligne["meta"]["hub_id"] == 123
    assert ligne["meta"]["app"] == "user" and ligne["meta"]["owner_id"] == "owner-7"
    assert ligne["meta"]["scopes"] == "crm.lists.read crm.objects.contacts.read"
    # The personal app's client did the exchange and the introspection (secret in
    # the BODY), and the owner was looked up with the exchange's access token.
    assert echanges[0]["client_id"] == "ucid"
    url, data = env.appels[0]
    assert url == env.hs.INTROSPECT_URL and data["client_secret"] == "ucsec"
    assert env.owners == [("someone@example.test", "at-1")]


def test_le_retour_org_range_l_app_account_sans_owner(env, monkeypatch):
    from oto_mcp.auth import flow as oauth_flow

    monkeypatch.setattr("oto_mcp.roles.is_org_admin", lambda sub, org: True)
    monkeypatch.setattr(oauth_flow, "exchange_code", lambda url, **k: {
        "access_token": "at-1", "refresh_token": "rt-1", "expires_in": 1800})
    env.reponses[env.hs.INTROSPECT_URL] = [_introspection()]
    resp = _callback({"code": "c0de", "state": env.hs.make_state(SUB, ORG, "org")})
    assert resp.headers["location"].endswith("connect=connected")
    (cle, ligne), = env.coffre.lignes.items()
    assert cle[0] == "org"
    assert ligne["meta"]["app"] == "account" and "owner_id" not in ligne["meta"]
    assert env.appels[0][1]["client_id"] == "cid" and not env.owners


def test_un_state_rejoue_n_ecrit_rien(env, monkeypatch):
    monkeypatch.setattr("oto_mcp.auth.flow.consume_state", lambda aud, data: False)
    resp = _callback({"code": "c0de", "state": env.hs.make_state(SUB, ORG, "member")})
    assert resp.headers["location"].endswith("connect=error")
    assert not env.coffre.lignes


def test_un_role_perdu_entre_le_clic_et_le_retour_n_ecrit_rien(env):
    resp = _callback({"code": "c0de", "state": env.hs.make_state(SUB, ORG, "org")})
    assert resp.headers["location"].endswith("connect=forbidden")
    assert not env.coffre.lignes


def test_un_refus_chez_hubspot_revient_en_denied(env):
    resp = _callback({"error": "access_denied",
                      "state": env.hs.make_state(SUB, ORG, "member")})
    assert resp.headers["location"].endswith("connect=denied")


# ── The Bearer the tools use ───────────────────────────────────────────────

def _rc(key, meta=None):
    return types.SimpleNamespace(key=key, config=dict(meta or {}), entity_type="member",
                                 entity_id=MEMBRE, account="")


def test_une_cle_collee_passe_telle_quelle(env, monkeypatch):
    monkeypatch.setattr("oto_mcp.access.resolve_credential",
                        lambda *a, **k: _rc("pat-eu1-xyz"))
    assert env.hs.bearer() == "pat-eu1-xyz"
    assert not env.appels


def test_une_connexion_oauth_mint_un_jeton_puis_le_garde(env, monkeypatch):
    monkeypatch.setattr("oto_mcp.access.resolve_credential",
                        lambda *a, **k: _rc("rt-1", {"auth": "oauth"}))
    env.reponses[env.hs.TOKEN_URL] = [_Reponse(200, {
        "access_token": "at-1", "refresh_token": "rt-1", "expires_in": 1800})]
    assert env.hs.bearer() == "at-1"
    assert env.hs.bearer() == "at-1"          # cached: one refresh only
    assert len(env.appels) == 1
    assert env.appels[0][1]["grant_type"] == "refresh_token"
    # No `meta.app`: a row from before the personal app, renewed by the org app.
    assert env.appels[0][1]["client_id"] == "cid"


def test_une_connexion_perso_se_renouvelle_avec_son_app(env, monkeypatch):
    monkeypatch.setattr("oto_mcp.access.resolve_credential",
                        lambda *a, **k: _rc("rt-1", {"auth": "oauth", "app": "user"}))
    env.reponses[env.hs.TOKEN_URL] = [_Reponse(200, {
        "access_token": "at-1", "expires_in": 1800})]
    assert env.hs.bearer() == "at-1"
    assert env.appels[0][1]["client_id"] == "ucid"
    assert env.appels[0][1]["client_secret"] == "ucsec"


# ── The default owner of a personal connection ─────────────────────────────

def _servir(env, monkeypatch, meta):
    monkeypatch.setattr("oto_mcp.access.resolve_credential",
                        lambda *a, **k: _rc("rt-1" if meta else "pat-x", meta))
    env.reponses[env.hs.TOKEN_URL] = [_Reponse(200, {
        "access_token": "at-1", "expires_in": 1800})]
    env.hs.bearer()


def test_un_record_cree_en_perso_revient_a_qui_a_connecte(env, monkeypatch):
    _servir(env, monkeypatch, {"auth": "oauth", "app": "user", "owner_id": "owner-7"})
    d = env.hs.default_owner
    assert d("contacts", {"email": "a@b.test"}) == {
        "email": "a@b.test", "hubspot_owner_id": "owner-7"}
    assert d("deals", {})["hubspot_owner_id"] == "owner-7"
    # The call's own owner wins — even an empty one (a deliberate "unassigned").
    assert d("contacts", {"hubspot_owner_id": "9"}) == {"hubspot_owner_id": "9"}
    assert d("contacts", {"hubspot_owner_id": ""}) == {"hubspot_owner_id": ""}
    # An object without owners (a custom one) is left alone.
    assert d("2-7", {"name": "x"}) == {"name": "x"}


@pytest.mark.parametrize("meta", [
    {"auth": "oauth", "app": "account"},            # org connection
    {"auth": "oauth"},                              # row from before the personal app
    {"auth": "oauth", "app": "user"},               # personal, no owner record
    None])                                          # pasted key
def test_aucun_owner_par_defaut_hors_connexion_perso(env, monkeypatch, meta):
    _servir(env, monkeypatch, meta)
    assert env.hs.default_owner("contacts", {"email": "a@b.test"}) == {"email": "a@b.test"}


def test_un_refresh_token_qui_tourne_est_reecrit(env, monkeypatch):
    env.coffre.lignes[("member", MEMBRE, "")] = {
        "secret": "rt-1", "meta": {"auth": "oauth", "hub_id": 123}, "set_by": SUB}
    monkeypatch.setattr("oto_mcp.access.resolve_credential",
                        lambda *a, **k: _rc("rt-1", {"auth": "oauth"}))
    env.reponses[env.hs.TOKEN_URL] = [_Reponse(200, {
        "access_token": "at-2", "refresh_token": "rt-2", "expires_in": 1800})]
    assert env.hs.bearer() == "at-2"
    ligne = env.coffre.lignes[("member", MEMBRE, "")]
    assert ligne["secret"] == "rt-2"
    assert ligne["meta"] == {"auth": "oauth", "hub_id": 123}   # meta passed back


def test_un_grant_mort_marque_la_ligne_et_demande_de_reconnecter(env, monkeypatch):
    monkeypatch.setattr("oto_mcp.access.resolve_credential",
                        lambda *a, **k: _rc("rt-1", {"auth": "oauth"}))
    env.reponses[env.hs.TOKEN_URL] = [_Reponse(400, {"error": "invalid_grant"})]
    with pytest.raises(env.hs.HubSpotReauthRequired, match="Reconnect HubSpot"):
        env.hs.bearer()
    assert env.coffre.rejets == [("member", MEMBRE, "", "unauthorized")]


def test_une_config_fausse_ne_marque_pas_la_ligne(env, monkeypatch):
    monkeypatch.setattr("oto_mcp.access.resolve_credential",
                        lambda *a, **k: _rc("rt-1", {"auth": "oauth"}))
    env.reponses[env.hs.TOKEN_URL] = [_Reponse(400, {"error": "invalid_client"})]
    with pytest.raises(env.hs.HubSpotOAuthError) as e:
        env.hs.bearer()
    assert not isinstance(e.value, env.hs.HubSpotReauthRequired)
    assert not env.coffre.rejets


# ── Probe ───────────────────────────────────────────────────────────────────

def test_la_sonde_d_une_connexion_oauth_introspecte(env):
    from oto_mcp.tools import hubspot as tools_hubspot

    env.reponses[env.hs.INTROSPECT_URL] = [_introspection()]
    out = tools_hubspot._verify({"key": "rt-1"}, {"auth": "oauth"})
    assert out["identity"] == {"portal_id": 123, "ui_domain": "portal.example",
                               "connected_as": "someone@example.test",
                               "connection": "org"}
    assert env.appels[0][1]["client_id"] == "cid"
    assert out["scopes"]["method"] == "introspection"
    assert out["scopes"]["families"]["contacts"] == "granted"
    assert out["scopes"]["families"]["tickets"] == "missing"
    assert [u for u, _ in env.appels] == [env.hs.INTROSPECT_URL]   # no token minted


def test_la_sonde_perso_introspecte_avec_son_app_et_dit_l_owner(env):
    from oto_mcp.tools import hubspot as tools_hubspot

    env.reponses[env.hs.INTROSPECT_URL] = [_introspection(), _introspection()]
    out = tools_hubspot._verify({"key": "rt-1"}, {"auth": "oauth", "app": "user",
                                                  "owner_id": "owner-7"})
    assert env.appels[0][1]["client_id"] == "ucid"
    assert out["identity"]["connection"] == "personal"
    assert out["identity"]["default_owner_id"] == "owner-7"
    out = tools_hubspot._verify({"key": "rt-1"}, {"auth": "oauth", "app": "user"})
    assert out["identity"]["default_owner_id"].startswith("none")


def test_la_sonde_d_une_connexion_desinstallee_echoue(env):
    from oto_mcp.tools import hubspot as tools_hubspot

    env.reponses[env.hs.INTROSPECT_URL] = [_Reponse(200, {"active": False})]
    with pytest.raises(env.hs.HubSpotReauthRequired):
        tools_hubspot._verify({"key": "rt-1"}, {"auth": "oauth"})


def test_un_scope_manquant_sur_une_connexion_oauth_dit_de_reconnecter(env, monkeypatch):
    from oto.tools.common.errors import UpstreamHTTPError

    from oto_mcp.tools.hubspot_scopes import missing_scopes_refusal

    monkeypatch.setattr("oto_mcp.access.resolve_credential",
                        lambda *a, **k: _rc("rt-1", {"auth": "oauth"}))
    env.reponses[env.hs.TOKEN_URL] = [_Reponse(200, {
        "access_token": "at-1", "expires_in": 1800})]

    def appel():
        env.hs.bearer()
        e = UpstreamHTTPError(403, "missing", service="hubspot")
        e.body = {"category": "MISSING_SCOPES", "message": "x"}
        return missing_scopes_refusal(e, object_type="tickets")

    import contextvars
    refus = contextvars.copy_context().run(appel)
    assert refus is not None and "reconnect it from the connector card" in str(refus)


# ── Removal: uninstall the app from the portal ──────────────────────────────

AUTRE = f"{ORG}:un-collegue"


def _oauth(app="account", hub_id=123):
    return {"secret": "rt-1", "meta": {"auth": "oauth", "app": app, "hub_id": hub_id}}


@pytest.fixture
def retrait(env, monkeypatch):
    """The vault's other rows (by `hub_id`) and the uninstall calls, both faked."""
    from oto_mcp import credentials_store

    autres: list[dict] = []
    desinstallations: list[str] = []
    monkeypatch.setattr(credentials_store, "holders_by_meta",
                        lambda connector, field, value: [
                            h for h in autres if str(h["meta"].get(field)) == value])
    monkeypatch.setattr(env.hs, "_uninstall_call", lambda token: (
        desinstallations.append(token) or _Reponse(204, {})))
    env.autres, env.desinstallations = autres, desinstallations
    return env


@pytest.mark.parametrize("app", ["account", "user"])
def test_le_retrait_desinstalle_l_app_quand_personne_d_autre_ne_la_tient(retrait, app):
    retrait.coffre.lignes[("member", MEMBRE, "")] = _oauth(app)
    retrait.autres.append({"entity_type": "member", "entity_id": MEMBRE, "account": "",
                           "meta": _oauth(app)["meta"]})
    retrait.reponses[retrait.hs.TOKEN_URL] = [_Reponse(200, {
        "access_token": "at-1", "expires_in": 1800})]
    retrait.hs.uninstall_before_clear("member", MEMBRE)
    assert retrait.desinstallations == ["at-1"]
    assert retrait.appels[0][1]["client_id"] == ("ucid" if app == "user" else "cid")


@pytest.mark.parametrize("app", ["account", "user"])
def test_le_retrait_ne_desinstalle_pas_une_app_qu_une_autre_ligne_tient(retrait, app):
    """HubSpot uninstalls from the WHOLE portal, the personal app too: a colleague's
    connection through the same app in the same portal keeps it installed."""
    retrait.coffre.lignes[("member", MEMBRE, "")] = _oauth(app)
    retrait.autres.append({"entity_type": "member", "entity_id": AUTRE, "account": "",
                           "meta": _oauth(app)["meta"]})
    retrait.hs.uninstall_before_clear("member", MEMBRE)
    assert retrait.desinstallations == [] and retrait.appels == []


def test_l_autre_app_du_meme_portail_ne_retient_pas_la_desinstallation(retrait):
    retrait.coffre.lignes[("org", str(ORG), "")] = _oauth("account")
    retrait.autres.append({"entity_type": "member", "entity_id": AUTRE, "account": "",
                           "meta": _oauth("user")["meta"]})
    retrait.reponses[retrait.hs.TOKEN_URL] = [_Reponse(200, {
        "access_token": "at-1", "expires_in": 1800})]
    retrait.hs.uninstall_before_clear("org", str(ORG))
    assert retrait.desinstallations == ["at-1"]


def test_dans_le_doute_sur_les_autres_lignes_on_ne_desinstalle_pas(retrait, monkeypatch):
    from oto_mcp import credentials_store

    def illisible(*a):
        raise RuntimeError("vault unreachable")

    monkeypatch.setattr(credentials_store, "holders_by_meta", illisible)
    retrait.coffre.lignes[("member", MEMBRE, "")] = _oauth()
    retrait.hs.uninstall_before_clear("member", MEMBRE)
    assert retrait.desinstallations == []


def test_un_grant_mort_au_retrait_ne_bloque_rien(retrait):
    retrait.coffre.lignes[("member", MEMBRE, "")] = _oauth()
    retrait.reponses[retrait.hs.TOKEN_URL] = [_Reponse(400, {"error": "invalid_grant"})]
    retrait.hs.uninstall_before_clear("member", MEMBRE)
    assert retrait.desinstallations == []


def test_un_refus_de_desinstallation_ne_bloque_rien(retrait, monkeypatch):
    retrait.coffre.lignes[("member", MEMBRE, "")] = _oauth()
    retrait.reponses[retrait.hs.TOKEN_URL] = [_Reponse(200, {
        "access_token": "at-1", "expires_in": 1800})]
    monkeypatch.setattr(retrait.hs, "_uninstall_call",
                        lambda token: _Reponse(403, {"message": "no"}))
    retrait.hs.uninstall_before_clear("member", MEMBRE)


@pytest.mark.parametrize("ligne", [None, {"secret": "pat-1", "meta": {}},
                                   _oauth(hub_id=None)])
def test_rien_a_desinstaller_pour_une_cle_collee_ou_sans_portail(retrait, ligne):
    if ligne is not None:
        retrait.coffre.lignes[("member", MEMBRE, "")] = ligne
    retrait.hs.uninstall_before_clear("member", MEMBRE)
    assert retrait.desinstallations == [] and retrait.appels == []


def test_le_retrait_oublie_le_jeton_d_acces_en_cache(retrait):
    retrait.coffre.lignes[("member", MEMBRE, "")] = _oauth()
    retrait.hs._keep(("member", MEMBRE, ""), "rt-1", "at-cache", 1800)
    retrait.autres.append({"entity_type": "member", "entity_id": AUTRE, "account": "",
                           "meta": _oauth()["meta"]})
    retrait.hs.uninstall_before_clear("member", MEMBRE)
    assert retrait.hs._TOKENS == {}


@pytest.mark.parametrize("surface", ["org", "group"])
def test_les_surfaces_de_retrait_desinstallent_avant_de_retirer(monkeypatch, surface):
    """The org and team removals call the uninstall BEFORE the row goes (it reads the
    row to find the portal), and only for HubSpot."""
    from oto_mcp.auth import hubspot as hs

    ordre: list[tuple] = []
    monkeypatch.setattr(hs, "uninstall_before_clear",
                        lambda et, eid, account: ordre.append(("uninstall", et, eid, account)))
    if surface == "org":
        from oto_mcp.capabilities.orgs import secrets as cap
        monkeypatch.setattr(cap.org_store, "delete_org_secret",
                            lambda oid, p, account="": ordre.append(("clear", p)) or True)
        inp = types.SimpleNamespace(org_id=ORG, provider="hubspot", account=" ")
    else:
        from oto_mcp.capabilities.groups import secrets as cap
        monkeypatch.setattr(cap.group_store, "delete_group_secret",
                            lambda gid, p, account="": ordre.append(("clear", p)) or True)
        inp = types.SimpleNamespace(group_id=7, provider="hubspot", account="")
    cap._delete_secret(None, inp)
    assert ordre == [("uninstall", surface, str(inp.org_id if surface == "org" else 7), ""),
                     ("clear", "hubspot")]
    ordre.clear()
    inp.provider = "pipedrive"
    cap._delete_secret(None, inp)
    assert ordre == [("clear", "pipedrive")]
