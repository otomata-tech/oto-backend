"""« Add to Slack » — l'app publiée par l'instance, installée en un clic (OAuth v2).

Ce que le flux garantit : coordonnées de l'app lues au palier PLATEFORME (refus nommé
sinon), state lié à son audience et à usage unique, droits re-vérifiés au retour, le
refus de Slack en HTTP 200 `ok:false` lu comme un REFUS d'échange (jamais une panne),
et les jetons rangés dans le credential multi-champs EXISTANT — un compte par espace
de travail, retrouvé par `meta.team_id`."""
import asyncio
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from oto_mcp.auth import flow as oauth_flow
from oto_mcp.auth import slack as slack_auth

ORG = 12
SUB = "user-1"
_COORDONNEES = {"client_id": "111.222", "client_secret": "secret-fictif"}


class _Coffre:
    """Le coffre au palier visé : `(entity_type, entity_id, account) → ligne`."""

    def __init__(self):
        self.lignes: dict[tuple, dict] = {}

    def list_accounts(self, etype, eid, connector):
        assert connector == "slack"
        return [{"account": a, "meta": dict(l["meta"])}
                for (t, i, a), l in sorted(self.lignes.items()) if (t, i) == (etype, eid)]

    def get(self, etype, eid, connector, account=""):
        ligne = self.lignes.get((etype, eid, account))
        return dict(ligne) if ligne else None

    def set(self, etype, eid, connector, secret, set_by=None, meta=None, conn=None,
            account="", expected_version=None):
        assert connector == "slack"
        self.lignes[(etype, eid, account)] = {"secret": secret, "meta": dict(meta or {}),
                                              "set_by": set_by}

    def champs(self, etype, eid, account):
        return json.loads(self.lignes[(etype, eid, account)]["secret"])


@pytest.fixture
def env(monkeypatch):
    from oto_mcp import access, credentials_store, roles
    from oto_mcp.db import connector_settings as store

    monkeypatch.setenv("OTO_MCP_OAUTH_STATE_SECRET", "test-secret")
    monkeypatch.setattr(oauth_flow, "redirect_uri",
                        lambda path, host=None: f"https://mcp.example.test{path}")
    reglages = dict(_COORDONNEES)
    monkeypatch.setattr(store, "list_connector_settings",
                        lambda key=None, conn=None: [
                            {"scope_type": "platform", "scope_id": "platform",
                             "connector": "slack", "key": k, "value": v}
                            for k, v in reglages.items()])
    monkeypatch.setattr(access, "current_org", lambda sub: ORG)
    monkeypatch.setattr(roles, "is_org_admin", lambda sub, org: True)
    coffre = _Coffre()
    monkeypatch.setattr(credentials_store, "list_accounts", coffre.list_accounts)
    monkeypatch.setattr(credentials_store, "get_credential_with_meta", coffre.get)
    monkeypatch.setattr(credentials_store, "set_credential", coffre.set)
    monkeypatch.setattr(credentials_store, "guard_account_write", lambda *a, **k: None)
    return SimpleNamespace(coffre=coffre, reglages=reglages, ctx=SimpleNamespace(sub=SUB))


def _etat(env, scope="org"):
    out = slack_auth.start(env.ctx, {"scope": scope, "app": "front-inconnu"})
    return out, {k: v[0] for k, v in parse_qs(urlsplit(out.auth_url).query).items()}


def _grant(team_id="T1", team_name="Acme", bot="xoxb-1", user="xoxp-1", **extra):
    body = {"ok": True, "access_token": bot, "team": {"id": team_id, "name": team_name},
            "bot_user_id": "UBOT", "app_id": "A1", **extra}
    if user:
        body["authed_user"] = {"id": "U1", "access_token": user}
    return body


# ── Départ ──────────────────────────────────────────────────────────────────

def test_le_flux_est_declare_avec_son_retour(env):
    from oto_mcp.connectors import flow as connector_flow

    assert connector_flow.supports("slack")
    assert connector_flow.describe("slack")["label"] == "Add to Slack"
    assert (connector_flow.callback_url("slack")
            == "https://mcp.example.test/api/slack/oauth/callback")
    assert connector_flow.app_ready("slack", SUB) is True


def test_l_url_d_installation_porte_l_app_les_scopes_et_un_state_lie(env):
    out, q = _etat(env)
    assert out.auth_url.startswith("https://slack.com/oauth/v2/authorize?")
    assert q["client_id"] == "111.222"
    assert q["redirect_uri"] == "https://mcp.example.test/api/slack/oauth/callback"
    assert set(q["scope"].split(",")) == set(slack_auth.BOT_SCOPES)
    assert set(q["user_scope"].split(",")) == set(slack_auth.USER_SCOPES)
    assert "secret-fictif" not in out.auth_url
    parsed = slack_auth.read_state(q["state"])
    assert (parsed["sub"], parsed["org"], parsed["scope"]) == (SUB, ORG, "org")
    assert parsed["app"] == "" and parsed["jti"]          # front hors liste → défaut
    # un state d'un AUTRE flux ne vaut rien ici
    assert slack_auth.read_state(oauth_flow.sign_state(
        "wordpress", {"sub": SUB, "org": ORG, "scope": "org", "jti": "x"})) is None


def test_les_scopes_sont_ceux_du_manifeste_documente():
    """Le manifeste de la fiche est ce que l'utilisateur colle quand il crée SA app :
    l'app publiée n'en demande ni moins (un outil marcherait d'un côté et
    `missing_scope` de l'autre), ni plus."""
    from oto_mcp import providers

    corps = " ".join(s.body_md for s in providers.REGISTRY["slack"].doc_sections)
    import yaml

    manifeste = yaml.safe_load(corps.split("```yaml", 1)[1].split("```", 1)[0])
    scopes = manifeste["oauth_config"]["scopes"]
    # …sauf ce que seul le bot de l'app publiée demande (`@Tulina`, 23/10) : une app
    # créée par l'utilisateur n'appelle jamais notre route d'événements.
    assert set(scopes["bot"]) == set(slack_auth.BOT_SCOPES) - set(slack_auth.SCOPES_DU_BOT)
    assert set(scopes["user"]) == set(slack_auth.USER_SCOPES)


def test_l_url_ne_demande_aucun_scope_sans_code_qui_s_en_sert(env):
    """Retirés le 22/10 tant qu'aucun code ne s'en servait. `app_mentions:read` est
    revenu le 23/10 avec le bot (`slack_events.py`) ; `assistant:write` et les
    commandes slash n'ont toujours aucun code."""
    _out, q = _etat(env)
    demandes = set(q["scope"].split(",")) | set(q["user_scope"].split(","))
    assert not demandes & {"assistant:write", "commands"}
    assert "app_mentions:read" in demandes


def test_sans_coordonnees_le_refus_nomme_la_cle_et_le_flux_reste_muet(env):
    from oto_mcp.capabilities._types import AuthzDenied

    env.reglages.pop("client_secret")
    assert slack_auth.coordonnees_manquantes() == ["client_secret"]
    assert slack_auth.app_disponible(SUB) is False
    with pytest.raises(slack_auth.SlackAppNotPublished, match="client_secret"):
        slack_auth.app()
    with pytest.raises(AuthzDenied) as e:
        slack_auth._start_flow(env.ctx, {"scope": "org"})
    assert e.value.status == 503 and "client_secret" in str(e.value)


def test_une_autre_panne_au_depart_n_est_pas_lue_comme_une_app_absente(env, monkeypatch):
    """Seule l'app absente devient `oauth_misconfigured` : une autre RuntimeError (ici
    l'adresse publique de l'instance) remonte telle quelle, jamais déguisée."""
    def _panne(path, host=None):
        raise RuntimeError("OTO_PUBLIC_BASE_URL manquante")

    monkeypatch.setattr(oauth_flow, "redirect_uri", _panne)
    with pytest.raises(RuntimeError, match="OTO_PUBLIC_BASE_URL"):
        slack_auth._start_flow(env.ctx, {"scope": "org"})


def test_le_palier_org_exige_un_admin(env, monkeypatch):
    from oto_mcp import roles
    from oto_mcp.capabilities._types import AuthzDenied

    monkeypatch.setattr(roles, "is_org_admin", lambda sub, org: False)
    with pytest.raises(AuthzDenied) as e:
        slack_auth._start_flow(env.ctx, {"scope": "org"})
    assert e.value.status == 403
    assert slack_auth.start(env.ctx, {"scope": "member"}).auth_url   # le sien, oui


def test_seul_client_secret_est_secret_et_suit_la_convention():
    from oto_mcp.capabilities import platform_connectors as pc

    assert [k for k in slack_auth._REGLAGES if "secret" in k] == ["client_secret"]
    lignes = pc._sans_les_secrets([
        {"connector": "slack", "key": k, "value": v} for k, v in _COORDONNEES.items()])
    assert "secret-fictif" not in str(lignes)


# ── Échange ─────────────────────────────────────────────────────────────────

def _repond(monkeypatch, status, body):
    class _R:
        status_code = status

        def json(self):
            return body

    appels = []
    monkeypatch.setattr(oauth_flow.requests, "post",
                        lambda url, data=None, timeout=None: appels.append((url, data)) or _R())
    return appels


def test_un_ok_false_en_http_200_est_un_refus_d_echange(env, monkeypatch):
    _repond(monkeypatch, 200, {"ok": False, "error": "invalid_code"})
    with pytest.raises(oauth_flow.OAuthExchangeRefused, match="invalid_code"):
        slack_auth.exchange("code-1")


def test_l_echange_part_en_corps_et_rend_le_grant(env, monkeypatch):
    appels = _repond(monkeypatch, 200, _grant())
    assert slack_auth.exchange("code-1")["team"]["id"] == "T1"
    url, data = appels[0]
    assert url == "https://slack.com/api/oauth.v2.access" and "?" not in url
    assert data["client_secret"] == "secret-fictif" and data["code"] == "code-1"


def test_un_grant_sans_espace_de_travail_est_refuse(env, monkeypatch):
    _repond(monkeypatch, 200, {"ok": True, "access_token": "xoxb-1"})
    with pytest.raises(oauth_flow.OAuthExchangeRefused):
        slack_auth.exchange("code-1")


# ── Coffre ──────────────────────────────────────────────────────────────────

def test_les_jetons_vont_dans_le_credential_existant_au_palier_org(env):
    _out, q = _etat(env)
    compte = slack_auth.finish(slack_auth.read_state(q["state"]), _grant())
    assert compte == "Acme"
    assert env.coffre.champs("org", str(ORG), "Acme") == {
        "bot_token": "xoxb-1", "user_token": "xoxp-1"}
    meta = env.coffre.lignes[("org", str(ORG), "Acme")]["meta"]
    assert meta["team_id"] == "T1" and meta["install_source"] == "oauth"
    assert "xoxb" not in json.dumps(meta)


def test_reinstaller_le_meme_espace_remplace_sa_ligne_meme_renommee(env):
    _out, q = _etat(env)
    parsed = slack_auth.read_state(q["state"])
    slack_auth.finish(parsed, _grant())
    compte = slack_auth.finish(parsed, _grant(team_name="Acme Corp", bot="xoxb-2",
                                              user=None))
    assert compte == "Acme" and len(env.coffre.lignes) == 1
    # même app : le jeton utilisateur non rendu cette fois est CONSERVÉ
    assert env.coffre.champs("org", str(ORG), "Acme") == {
        "bot_token": "xoxb-2", "user_token": "xoxp-1"}


def test_un_second_espace_ajoute_un_compte_et_un_nom_pris_se_suffixe(env):
    _out, q = _etat(env)
    parsed = slack_auth.read_state(q["state"])
    slack_auth.finish(parsed, _grant())
    assert slack_auth.finish(parsed, _grant(team_id="T2", team_name="Beta")) == "Beta"
    assert slack_auth.finish(parsed, _grant(team_id="T3", team_name="Acme")) == "Acme (T3)"
    assert len(env.coffre.lignes) == 3


def test_reinstaller_avec_une_autre_app_publiee_ne_garde_pas_le_jeton_utilisateur(env):
    """Même espace, déjà installé par ce flux, mais par une AUTRE app (l'instance a
    changé d'app publiée) : le jeton utilisateur de l'ancienne app n'est pas repris."""
    _out, q = _etat(env)
    parsed = slack_auth.read_state(q["state"])
    slack_auth.finish(parsed, _grant())
    compte = slack_auth.finish(parsed, _grant(bot="xoxb-2", user=None, app_id="A2"))
    assert compte == "Acme" and len(env.coffre.lignes) == 1
    assert env.coffre.champs("org", str(ORG), "Acme") == {"bot_token": "xoxb-2"}
    assert env.coffre.lignes[("org", str(ORG), "Acme")]["meta"]["app_id"] == "A2"


@pytest.mark.parametrize("rotation", [
    {"refresh_token": "xoxe-1", "expires_in": 43200},
    {"user": None, "authed_user": {"id": "U1", "access_token": "xoxe.xoxp-1",
                                   "refresh_token": "xoxe-2", "expires_in": 43200}},
], ids=["bot", "user"])
def test_un_refresh_token_recu_est_une_rotation_non_geree(env, rotation):
    _out, q = _etat(env)
    with pytest.raises(slack_auth.SlackTokenRotationEnabled, match="rotation"):
        slack_auth.finish(slack_auth.read_state(q["state"]), _grant(**rotation))
    assert not env.coffre.lignes


def test_remplacer_les_jetons_colles_d_une_autre_app_ne_les_melange_pas(env):
    """Une ligne collée (SON app) du même espace : l'installation la remplace, sans
    reprendre son jeton utilisateur, qui appartient à une AUTRE app que ce bot."""
    env.coffre.set("org", str(ORG), "slack",
                   json.dumps({"bot_token": "xoxb-old", "user_token": "xoxp-old"}),
                   meta={"team_id": "T1"}, account="Équipe")
    _out, q = _etat(env)
    compte = slack_auth.finish(slack_auth.read_state(q["state"]), _grant(user=None))
    assert compte == "Équipe"
    assert env.coffre.champs("org", str(ORG), "Équipe") == {"bot_token": "xoxb-1"}


# ── Retour de consentement ──────────────────────────────────────────────────

def _callback(query: dict):
    from starlette.requests import Request

    from oto_mcp.api import slack as api_slack

    route = api_slack.make_routes(None, None, None, None, None)[0]
    req = Request({"type": "http", "method": "GET", "path": route.path,
                   "query_string": urlencode(query).encode(), "headers": []})
    return asyncio.run(route.endpoint(req))


@pytest.fixture
def usage_unique(monkeypatch):
    vus = set()
    monkeypatch.setattr(oauth_flow, "consume_state",
                        lambda aud, data: not (data["jti"] in vus or vus.add(data["jti"])))


def test_retour_reussi_range_et_redirige_connected(env, usage_unique, monkeypatch):
    _repond(monkeypatch, 200, _grant())
    _out, q = _etat(env)
    resp = _callback({"code": "c", "state": q["state"]})
    assert resp.headers["location"].endswith("?connector=slack&connect=connected")
    assert ("org", str(ORG), "Acme") in env.coffre.lignes
    # le même state rejoué ne range rien de plus
    resp = _callback({"code": "c", "state": q["state"]})
    assert resp.headers["location"].endswith("connect=error")


def test_refus_chez_slack_revient_en_denied(env, usage_unique):
    _out, q = _etat(env)
    resp = _callback({"error": "access_denied", "state": q["state"]})
    assert resp.headers["location"].endswith("?connector=slack&connect=denied")
    assert not env.coffre.lignes


def test_un_admin_destitue_entre_temps_ne_range_rien(env, usage_unique, monkeypatch):
    from oto_mcp import roles

    _out, q = _etat(env)
    monkeypatch.setattr(roles, "is_org_admin", lambda sub, org: False)
    resp = _callback({"code": "c", "state": q["state"]})
    assert resp.headers["location"].endswith("connect=forbidden")
    assert not env.coffre.lignes


def test_echange_refuse_revient_en_error_sans_ranger(env, usage_unique, monkeypatch):
    _repond(monkeypatch, 200, {"ok": False, "error": "bad_redirect_uri"})
    _out, q = _etat(env)
    resp = _callback({"code": "c", "state": q["state"]})
    assert resp.headers["location"].endswith("connect=error")
    assert not env.coffre.lignes


def test_rotation_activee_revient_en_error_sans_ranger(env, usage_unique, monkeypatch):
    _repond(monkeypatch, 200, _grant(refresh_token="xoxe-1", expires_in=43200))
    _out, q = _etat(env)
    resp = _callback({"code": "c", "state": q["state"]})
    assert resp.headers["location"].endswith("connect=error")
    assert not env.coffre.lignes


def test_state_illisible(env, usage_unique):
    assert _callback({"code": "c", "state": "faux"}).headers["location"].endswith(
        "connect=error")
