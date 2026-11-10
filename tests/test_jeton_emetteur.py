"""Le jeton ÉMETTEUR (portée `issue`) : il émet des jetons à portée sans session humaine.

Il renverse « émettre un jeton reste un acte humain » (décision d'Alexis, 08/10/2026).
Le motif de l'ancienne règle — une fuite qui s'auto-entretient — doit donc être tenu par
ce que ce banc fige :

1. la portée : le plafond ne porte que tableaux et projets, l'inclusion est stricte ;
2. le passage (`_authenticate`) : seul l'émetteur franchit une route de jetons, et
   jamais celles du palier admin ; sans l'id de son jeton, il ne franchit rien ;
3. l'émission : enfant toujours à portée, inclus dans le plafond, ni émetteur ni
   runner, échéancé ≤ 90 j ; l'émetteur lui-même ne naît pas sans échéance ;
4. la base (vrai PostgreSQL) : un enfant meurt avec son parent, révoqué ou échu.
"""
from __future__ import annotations

import asyncio
import json
import types

import pytest

from _datastore_rest import call, stub_authz

from oto_mcp.api import routes as api_routes
from oto_mcp.auth import token_scopes
from oto_mcp.capabilities import api_tokens as at
from oto_mcp.datastore import core as datastore

_PLAFOND = {"namespaces": {"12": "write"}, "projects": {"5": "read"}}
_EMETTEUR = {"issue": _PLAFOND}


@pytest.fixture(autouse=True)
def _portee_neuve():
    token_scopes.set_current(None)
    yield
    token_scopes.set_current(None)


# --- 1. La portée -------------------------------------------------------------

def test_le_plafond_se_parse_et_se_rend_tel_quel():
    assert token_scopes.parse(_EMETTEUR) == _EMETTEUR


@pytest.mark.parametrize("plafond", [
    {"runner": True}, {"issue": {"namespaces": {"12": "read"}}}, {}, "tout",
    {"namespaces": {"12": "read"}, "runner": True}])
def test_un_plafond_ne_porte_que_tableaux_et_projets(plafond):
    with pytest.raises(token_scopes.ScopeError):
        token_scopes.parse({"issue": plafond})


@pytest.mark.parametrize("portee,dedans", [
    ({"namespaces": {"12": "read"}}, True),
    ({"namespaces": {"12": "write"}, "projects": {"5": "read"}}, True),
    ({"namespaces": {"13": "read"}}, False),
    ({"namespaces": {"12": "read"}, "runner": True}, False),
    ({"issue": _PLAFOND}, False),
    (None, False),
])
def test_l_inclusion_est_stricte(portee, dedans):
    assert token_scopes.inclus(portee, _PLAFOND) is dedans


def test_l_inclusion_compare_les_droits():
    assert not token_scopes.inclus({"namespaces": {"12": "write"}},
                                   {"namespaces": {"12": "read"}})


def test_une_portee_issue_sans_id_de_jeton_leve():
    """Sans l'id, `emetteur()` rendrait None : le handler prendrait le jeton pour une
    session humaine et lui ouvrirait tous les jetons du compte."""
    with pytest.raises(ValueError):
        token_scopes.set_current(_EMETTEUR)
    assert token_scopes.current() is None


def test_sans_id_de_jeton_les_routes_d_emission_restent_fermees():
    assert not token_scopes.authorize(_EMETTEUR, "POST", "/api/me/tokens")
    token_scopes.set_current(_EMETTEUR, 41)
    assert token_scopes.authorize(_EMETTEUR, "POST", "/api/me/tokens")
    assert token_scopes.emetteur() == (41, _PLAFOND)


# --- 2. Le passage ------------------------------------------------------------

def _req(method: str, path: str, token: str = "oto_deadbeef"):
    from starlette.requests import Request
    return Request({
        "type": "http", "method": method, "path": path, "query_string": b"",
        "root_path": "", "scheme": "http", "server": ("test", 80),
        "http_version": "1.1",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
    })


class _FakeVerifier:
    async def verify_token(self, token):
        return types.SimpleNamespace(claims={"sub": "u-jwt", "email": "a@b.c"})


@pytest.fixture
def jeton(monkeypatch):
    box = {"row": {"sub": "u-1", "scopes": _EMETTEUR, "token_id": 41,
                   "token_kind": "user"}}
    monkeypatch.setattr(api_routes.db, "verify_api_token", lambda t: box["row"])
    monkeypatch.setattr(api_routes.db, "upsert_user", lambda *a, **k: None)
    monkeypatch.setattr(api_routes.db, "get_suspension", lambda sub: None)
    return box


def _auth(request, **kw):
    async def scenario():
        sub, err = await api_routes._authenticate(request, verifier=_FakeVerifier(),
                                                  **kw)
        return sub, err, token_scopes.emetteur()
    return asyncio.run(scenario())


def _erreur(err) -> str:
    return json.loads(bytes(err.body).decode())["error"]


_ROUTE_MEMBRE = {"allow_api_token": False, "allow_issuer_token": True}


@pytest.mark.parametrize("method,path", [("GET", "/api/me/tokens"),
                                         ("POST", "/api/me/tokens"),
                                         ("DELETE", "/api/me/tokens/9")])
def test_l_emetteur_franchit_les_routes_de_ses_jetons(jeton, method, path):
    sub, err, em = _auth(_req(method, path), **_ROUTE_MEMBRE)
    assert (sub, err) == ("u-1", None)
    assert em == (41, _PLAFOND)


@pytest.mark.parametrize("scopes", [None, {"namespaces": {"12": "write"}},
                                    {"runner": True}])
def test_un_autre_jeton_reste_refuse(jeton, scopes):
    jeton["row"]["scopes"] = scopes
    sub, err, _ = _auth(_req("POST", "/api/me/tokens"), **_ROUTE_MEMBRE)
    assert sub is None and err.status_code == 403
    assert _erreur(err) == "api_token_forbidden"


def test_l_emetteur_n_atteint_pas_le_palier_admin(jeton):
    sub, err, _ = _auth(_req("POST", "/api/admin/users/u-9/tokens"),
                        allow_api_token=False)
    assert sub is None and _erreur(err) == "api_token_forbidden"


def test_l_emetteur_n_ouvre_rien_d_autre(jeton):
    for method, path in (("GET", "/api/me"), ("GET", "/api/datastores/12/rows")):
        sub, err, _ = _auth(_req(method, path))
        assert sub is None and _erreur(err) == "token_scope_forbidden", path


def test_une_session_humaine_n_est_pas_un_emetteur(jeton):
    sub, err, em = _auth(_req("POST", "/api/me/tokens", token="eyJ-jwt"),
                         **_ROUTE_MEMBRE)
    assert (sub, err, em) == ("u-jwt", None, None)


# --- 3. L'émission ------------------------------------------------------------

@pytest.fixture
def socle(monkeypatch):
    stub_authz(monkeypatch)
    vus: list = []
    monkeypatch.setattr(at.db, "create_api_token",
                        lambda sub, **kw: vus.append(("create", sub, kw)) or "oto_ENFANT")
    monkeypatch.setattr(at.db, "list_api_tokens",
                        lambda sub, **kw: vus.append(("list", sub, kw)) or [])
    monkeypatch.setattr(at.db, "revoke_api_token",
                        lambda sub, tid, **kw: vus.append(("revoke", sub, tid, kw)) or True)
    monkeypatch.setattr(at.org_store, "org_du_compte_de_service", lambda sub: None)

    class _Store:
        def list_datastores(self):
            return [{"id": 12, "datastore": "clients"}, {"id": 13, "datastore": "autre"}]

    monkeypatch.setattr(datastore, "make_store", lambda sub: _Store())
    return vus


@pytest.fixture
def emetteur():
    token_scopes.set_current(_EMETTEUR, 41)


def test_l_emetteur_emet_un_enfant_qui_le_nomme_parent(socle, emetteur):
    code, out = call("me.token.create",
                     body={"scopes": {"namespaces": {"clients": "write"}}, "ttl_days": 30})
    assert code == 201, out
    assert out["scopes"] == {"namespaces": {"12": "write"}}
    assert socle[-1][2]["parent_id"] == 41
    assert socle[-1][2]["ttl_days"] == 30


@pytest.mark.parametrize("scopes", [
    None, {"namespaces": {"13": "read"}}, {"runner": True},
    {"issue": {"namespaces": {"12": "read"}}}, {"projects": {"6": "read"}}])
def test_hors_du_plafond_rien_ne_s_emet(socle, emetteur, scopes):
    code, out = call("me.token.create", body={"scopes": scopes, "ttl_days": 30})
    assert code == 403 and out["error"] == "scope_exceeds_issuer", out
    assert socle == []


@pytest.mark.parametrize("ttl", [None, 0, 91, "jamais"])
def test_l_enfant_est_echeance_au_plus_90_jours(socle, emetteur, ttl):
    body = {"scopes": {"namespaces": {"12": "read"}}}
    if ttl is not None:
        body["ttl_days"] = ttl
    code, out = call("me.token.create", body=body)
    assert code == 400 and out["error"] == "issued_token_ttl", out
    assert socle == []


def test_l_emetteur_ne_liste_et_ne_revoque_que_ses_enfants(socle, emetteur):
    assert call("me.token.list")[0] == 200
    assert socle[-1][2]["parent_id"] == 41
    assert call("me.token.delete", path_params={"token_id": "9"})[0] == 200
    assert socle[-1][3]["parent_id"] == 41


def test_un_jeton_porte_non_emetteur_n_est_jamais_pris_pour_un_humain(socle):
    """Même si l'authentification le laissait passer, le handler refuse."""
    token_scopes.set_current({"namespaces": {"12": "write"}}, 41)
    for cle, pp in (("me.token.create", None), ("me.token.list", None),
                    ("me.token.delete", {"token_id": "9"})):
        code, out = call(cle, path_params=pp, body={})
        assert code == 403 and out["error"] == "api_token_forbidden", cle
    assert socle == []


@pytest.mark.parametrize("cle,pp", [("me.token.create", None),
                                    ("platform.token.create", {"sub": "u-9"})])
def test_un_emetteur_ne_nait_pas_sans_echeance(monkeypatch, socle, cle, pp):
    from oto_mcp.capabilities import _authz
    monkeypatch.setattr(_authz.access, "is_super_admin", lambda sub: True)
    monkeypatch.setattr(at.db, "get_user", lambda sub: {"sub": sub})
    code, out = call(cle, path_params=pp, body={"scopes": _EMETTEUR})
    assert code == 400 and out["error"] == "issuer_ttl_required", out
    code, out = call(cle, path_params=pp,
                     body={"scopes": {"issue": {"namespaces": {"clients": "write"}}},
                           "ttl_days": 90})
    assert code in (200, 201), out
    assert out["scopes"] == {"issue": {"namespaces": {"12": "write"}}}, (
        "le plafond se range par identifiant, comme une portée")
    assert socle[-1][2].get("parent_id") is None


# --- 3 bis. Le plafond « toute une org » ----------------------------------------

_PLAFOND_ORG = {"orgs": {"2": "write"}}


def test_le_plafond_d_org_se_parse_et_se_rend_tel_quel():
    assert token_scopes.parse({"issue": {"orgs": {2: "write"}}}) == {
        "issue": {"orgs": {"2": "write"}}}
    mixte = {"issue": {"orgs": {"2": "read"}, "namespaces": {"12": "write"}}}
    assert token_scopes.parse(mixte) == mixte


@pytest.mark.parametrize("brut", [
    {"issue": {"orgs": {}}}, {"issue": {"orgs": {"2": "admin"}}},
    {"issue": {"orgs": {"deux": "read"}}}, {"issue": {"orgs": ["2"]}},
    {"orgs": {"2": "write"}}])
def test_une_org_ne_se_nomme_que_dans_un_plafond_et_avec_un_droit(brut):
    with pytest.raises(token_scopes.ScopeError):
        token_scopes.parse(brut)


@pytest.mark.parametrize("portee,orgs_des,dedans", [
    ({"namespaces": {"12": "write"}}, {"namespaces": {"12": 2}}, True),
    ({"projects": {"5": "read"}}, {"projects": {"5": 2}}, True),
    ({"namespaces": {"12": "write"}}, {"namespaces": {"12": 3}}, False),
    ({"namespaces": {"12": "write"}}, {"namespaces": {"12": None}}, False),
    ({"namespaces": {"12": "write"}}, None, False),
    ({"namespaces": {"12": "read"}, "runner": True}, {"namespaces": {"12": 2}}, False),
])
def test_sous_un_plafond_d_org_la_ressource_doit_vivre_dans_l_org(portee, orgs_des,
                                                                 dedans):
    assert token_scopes.inclus(portee, _PLAFOND_ORG, orgs_des) is dedans


def test_le_plafond_d_org_borne_le_droit():
    assert not token_scopes.inclus({"namespaces": {"12": "write"}},
                                   {"orgs": {"2": "read"}}, {"namespaces": {"12": 2}})


def test_plafond_nomme_et_plafond_d_org_s_additionnent():
    plafond = {"orgs": {"2": "read"}, "namespaces": {"12": "write"}}
    assert token_scopes.inclus({"namespaces": {"12": "write", "13": "read"}}, plafond,
                               {"namespaces": {"12": None, "13": 2}})


@pytest.fixture
def plafond_d_org(socle, monkeypatch):
    """12 est à l'org 2, 14 à l'équipe 7 de l'org 2, 13 est PERSONNEL — rangé dans
    l'org 2, il n'en fait pas partie pour autant."""
    proprios = {"12": ("org", "2"), "13": ("user", "u-1"), "14": ("group", "7")}
    monkeypatch.setattr(at.ownership, "owner_of",
                        lambda rtype, cle: proprios.get(cle) if rtype ==
                        at.ownership.TYPE_RESSOURCE_DATASTORE else None)
    monkeypatch.setattr(at.org_origin, "group_orgs", lambda owners: {7: 2})

    class _Store:
        def list_datastores(self):
            return [{"id": int(i), "datastore": f"t{i}"} for i in proprios]

    monkeypatch.setattr(datastore, "make_store", lambda sub: _Store())
    token_scopes.set_current({"issue": _PLAFOND_ORG}, 41)
    return socle


@pytest.mark.parametrize("ns", ["12", "14"])
def test_l_emetteur_d_org_emet_sur_un_tableau_de_l_org(plafond_d_org, ns):
    code, out = call("me.token.create",
                     body={"scopes": {"namespaces": {ns: "write"}}, "ttl_days": 30})
    assert code == 201, out
    assert plafond_d_org[-1][2]["parent_id"] == 41


@pytest.mark.parametrize("scopes", [
    {"namespaces": {"13": "read"}}, {"projects": {"5": "read"}},
    {"namespaces": {"12": "write", "13": "read"}}])
def test_l_emetteur_d_org_n_atteint_ni_le_personnel_ni_l_inconnu(plafond_d_org, scopes):
    code, out = call("me.token.create", body={"scopes": scopes, "ttl_days": 30})
    assert code == 403 and out["error"] == "scope_exceeds_issuer", out
    assert plafond_d_org == []


@pytest.mark.parametrize("membre,attendu", [(True, 201), (False, 400)])
def test_un_plafond_d_org_exige_d_en_etre_membre(socle, monkeypatch, membre, attendu):
    monkeypatch.setattr(at.roles, "is_org_member", lambda sub, org: membre)
    code, out = call("me.token.create",
                     body={"scopes": {"issue": _PLAFOND_ORG}, "ttl_days": 90})
    assert code == attendu, out
    if not membre:
        assert out["error"] == "unknown_org" and socle == []
    else:
        assert out["scopes"] == {"issue": _PLAFOND_ORG}


# --- 4. La base : un enfant meurt avec son parent -----------------------------

@pytest.fixture()
def base(pg_module_dsn, monkeypatch):
    import psycopg
    from psycopg.rows import dict_row

    monkeypatch.setenv("DATABASE_URL", pg_module_dsn)
    from oto_mcp.db import _conn
    monkeypatch.setattr(_conn, "_database_url", lambda: pg_module_dsn)
    _conn._pool = None
    from oto_mcp import db
    db.init_db()
    with psycopg.connect(pg_module_dsn, row_factory=dict_row, autocommit=True) as c:
        c.execute("DELETE FROM user_api_tokens")
    yield db, pg_module_dsn
    _conn._pool = None


def _famille(db):
    parent = db.create_api_token("u-1", "agent", ttl_days=30,
                                 scopes={"issue": {"namespaces": {"12": "write"}}})
    pid = db.verify_api_token(parent)["token_id"]
    enfant = db.create_api_token("u-1", "integration", ttl_days=30,
                                 scopes={"namespaces": {"12": "write"}}, parent_id=pid)
    return parent, pid, enfant


def test_revoquer_le_parent_revoque_l_enfant_et_le_dit(base):
    db, _ = base
    parent, pid, enfant = _famille(db)
    assert db.verify_api_token(enfant) is not None
    assert [t["parent_id"] for t in db.list_api_tokens("u-1", parent_id=pid)] == [pid]
    assert db.revoke_api_token("u-1", pid, revoked_by="u-1", reason="fuite")
    assert db.verify_api_token(enfant) is None
    enfants = db.list_api_tokens("u-1", include_revoked=True, parent_id=pid)
    assert enfants[0]["revoked_reason"] == f"émetteur {pid} révoqué"


def test_un_parent_echu_emporte_l_enfant(base):
    import psycopg

    db, dsn = base
    parent, pid, enfant = _famille(db)
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("UPDATE user_api_tokens SET expires_at = NOW() - INTERVAL '1 second' "
                  "WHERE id = %s", (pid,))
    assert db.verify_api_token(enfant) is None


def test_un_emetteur_ne_revoque_pas_le_jeton_d_un_autre(base):
    db, _ = base
    _, pid, _ = _famille(db)
    voisin = db.create_api_token("u-1", "humain")
    vid = db.verify_api_token(voisin)["token_id"]
    assert not db.revoke_api_token("u-1", vid, revoked_by="u-1", reason=None,
                                   parent_id=pid)
    assert db.verify_api_token(voisin) is not None
