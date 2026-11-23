"""Les écrivains de service d'un tableau : une org ouvre un de ses tableaux à un rôle de
service, et ce service y écrit sous SON identité — sans jeton de personne.

Ce que le banc tient :

1. la règle : seul un client machine portant le rôle `visitors` passe la route
   d'écriture ; ni le commerce, ni une personne (fût-elle super admin) ;
2. l'ouverture : seul qui GOUVERNE le tableau l'ouvre, seul un tableau d'ORG s'ouvre,
   seul un rôle écrivain s'inscrit ; l'ouverture est idempotente et se referme ;
3. l'écriture, sur la base réelle : elle aboutit dans le tableau ouvert, et rend le MÊME
   404 pour un tableau non ouvert, ouvert mais d'une autre org que celle du chemin, ou
   refermé ; une org suspendue n'est pas écrite ;
4. par les vraies routes REST : le `PUT` sans corps du front, le lot du worker sous un
   vrai jeton de service, et l'historique de ligne qui porte le service.
"""
from __future__ import annotations

import uuid

import pytest

from oto_mcp.auth import service_identity
from oto_mcp.capabilities._types import AuthzDenied, RawCtx, ResolvedCtx

SCHEMA = {"key": "domain", "fields": [
    {"key": "domain", "type": "text"}, {"key": "company", "type": "text"}]}


def _cap(cle):
    from oto_mcp.capabilities.registry import CAPABILITIES
    [c] = [c for c in CAPABILITIES if c.key == cle]
    return c


@pytest.fixture
def principal():
    """Pose une identité de service le temps d'un test, la retire ensuite."""
    def poser(*roles, client="m2m"):
        p = {"sub": service_identity.SUB_PREFIX + client, "client_id": client,
             "roles": frozenset(roles)}
        service_identity.set_current(p)
        return p
    yield poser
    service_identity.set_current(None)


# ── 1. la règle ──────────────────────────────────────────────────────────────

def test_la_route_d_ecriture_est_celle_du_service_visitors():
    from oto_mcp.capabilities._authz import VISITORS_SERVICE
    c = _cap("service.datastore.write_rows")
    assert c.authz is VISITORS_SERVICE and c.mcp is None
    [b] = c.rest_bindings()
    assert (b.verb, b.path) == (
        "POST", "/api/service/orgs/{id}/datastores/{datastore}/rows/batch")


def test_le_service_visitors_passe(principal):
    p = principal("visitors")
    ctx = _cap("service.datastore.write_rows").authz(RawCtx(sub=p["sub"]))
    assert ctx.role == "service:visitors" and ctx.org_id is None


def test_le_commerce_ne_passe_pas_la_route_d_ecriture(principal):
    p = principal("commerce")
    with pytest.raises(AuthzDenied) as e:
        _cap("service.datastore.write_rows").authz(RawCtx(sub=p["sub"]))
    assert (e.value.status, e.value.code) == (403, "service_role_missing")


def test_le_service_visitors_ne_passe_pas_les_routes_du_commerce(principal):
    p = principal("visitors")
    with pytest.raises(AuthzDenied) as e:
        _cap("service.org.suspension").authz(RawCtx(sub=p["sub"]))
    assert (e.value.status, e.value.code) == (403, "service_role_missing")


def test_une_personne_meme_super_admin_ne_passe_pas(monkeypatch):
    from oto_mcp import access
    monkeypatch.setattr(access, "is_super_admin", lambda sub: True)
    with pytest.raises(AuthzDenied) as e:
        _cap("service.datastore.write_rows").authz(RawCtx(sub="usr_super"))
    assert (e.value.status, e.value.code) == (403, "service_required")


def test_un_service_ne_passe_pas_l_ouverture(principal):
    """L'ouverture exige un COMPTE : un service ne s'ouvre pas lui-même un tableau."""
    p = principal("visitors")
    with pytest.raises(AuthzDenied) as e:
        _cap("me.datastore.service_writer.put").authz(RawCtx(sub=p["sub"]))
    assert e.value.status == 403


def test_le_commerce_n_est_pas_un_role_ecrivain():
    assert "commerce" in service_identity.ROLES
    assert "commerce" not in service_identity.ROLES_ECRIVAINS
    assert service_identity.ROLES_ECRIVAINS <= service_identity.ROLES


# ── 2 et 3. sur la base réelle ───────────────────────────────────────────────

@pytest.fixture
def monde(live):
    """Deux orgs ; dans A un admin et un membre, deux tableaux d'org (`a`, `a2`) et un
    tableau personnel de l'admin ; dans B un tableau d'org (`b`) — tous au même schéma."""
    from oto_mcp import db, org_store
    from oto_mcp.datastore.core import make_store
    u = uuid.uuid4().hex[:8]
    admin, membre, admin_b = f"adm_{u}", f"mbr_{u}", f"admb_{u}"
    for sub in (admin, membre, admin_b):
        db.upsert_user(sub, email=f"{sub}@example.test")
    org_a = int(org_store.create_org(f"a_{u}"))
    org_b = int(org_store.create_org(f"b_{u}"))
    org_store.add_org_member(org_a, admin, "org_admin")
    org_store.add_org_member(org_a, membre)
    org_store.add_org_member(org_b, admin_b, "org_admin")
    for sub, org in ((admin, org_a), (membre, org_a), (admin_b, org_b)):
        org_store.set_active_org(sub, org)
    t = {"a": db.create_datastore("org", str(org_a), f"va_{u}"),
         "a2": db.create_datastore("org", str(org_a), f"va2_{u}"),
         "perso": db.create_datastore("user", admin, f"vp_{u}"),
         "b": db.create_datastore("org", str(org_b), f"vb_{u}")}
    make_store(admin).set_schema(str(t["a"]), SCHEMA)
    make_store(admin).set_schema(str(t["a2"]), SCHEMA)
    make_store(admin).set_schema(str(t["perso"]), SCHEMA)
    make_store(admin_b).set_schema(str(t["b"]), SCHEMA)
    return {"admin": admin, "membre": membre, "admin_b": admin_b,
            "org_a": org_a, "org_b": org_b, "t": {k: int(v) for k, v in t.items()}}


def _ouvrir(sub, org, ns, role="visitors"):
    from oto_mcp.capabilities.datastore import ecrivains_de_service as E
    return E._ouvrir(ResolvedCtx(sub=sub, org_id=org),
                     E.ServiceWriterInput(datastore=str(ns), role=role))


def _refermer(sub, org, ns, role="visitors"):
    from oto_mcp.capabilities.datastore import ecrivains_de_service as E
    return E._refermer(ResolvedCtx(sub=sub, org_id=org),
                       E.ServiceWriterInput(datastore=str(ns), role=role))


def _ecrire(org, ns, rows, *, client="m2m", key="domain"):
    from oto_mcp.capabilities.datastore import ecrivains_de_service as E
    ctx = ResolvedCtx(sub=service_identity.SUB_PREFIX + client, org_id=None,
                      role="service:visitors")
    return E._ecrire(ctx, E.ServiceWriteRowsInput(org_id=org, datastore=str(ns),
                                                  rows=rows, key=key))


def _lignes(ns):
    from oto_mcp import db
    with db._connect() as conn:
        return [r["data"] for r in conn.execute(
            "SELECT data FROM datastore_rows WHERE ns_id = %s ORDER BY row_id",
            (ns,)).fetchall()]


def test_l_admin_ouvre_le_tableau_et_l_ouverture_est_idempotente(monde):
    m = monde
    premiere = _ouvrir(m["admin"], m["org_a"], m["t"]["a"])
    assert premiere["role"] == "visitors" and premiere["granted_by"] == m["admin"]
    assert premiere["datastore"] == str(m["t"]["a"])
    seconde = _ouvrir(m["admin"], m["org_a"], m["t"]["a"])
    assert seconde["granted_at"] == premiere["granted_at"]
    from oto_mcp.capabilities.datastore import ecrivains_de_service as E
    vue = E._lister(ResolvedCtx(sub=m["admin"], org_id=m["org_a"]),
                    E.ServiceWritersInput(datastore=str(m["t"]["a"])))
    assert [w["role"] for w in vue["service_writers"]] == ["visitors"]


def test_un_membre_qui_ne_gouverne_pas_n_ouvre_pas(monde):
    with pytest.raises(AuthzDenied) as e:
        _ouvrir(monde["membre"], monde["org_a"], monde["t"]["a2"])
    assert e.value.status == 403


def test_un_tableau_personnel_ne_s_ouvre_pas(monde):
    with pytest.raises(AuthzDenied) as e:
        _ouvrir(monde["admin"], monde["org_a"], monde["t"]["perso"])
    assert (e.value.status, e.value.code) == (400, "org_table_required")


def test_le_commerce_ne_s_inscrit_pas_comme_ecrivain(monde):
    with pytest.raises(AuthzDenied) as e:
        _ouvrir(monde["admin"], monde["org_a"], monde["t"]["a"], role="commerce")
    assert (e.value.status, e.value.code) == (400, "unknown_service_role")


def test_le_service_ecrit_le_tableau_ouvert_et_designe_par_la_cle(monde):
    m, ns = monde, monde["t"]["a"]
    _ouvrir(m["admin"], m["org_a"], ns)
    r = _ecrire(m["org_a"], ns, [{"domain": "example.com", "company": "Example"}])
    assert (r["inserted"], r["updated"]) == (1, 0)
    r = _ecrire(m["org_a"], ns, [{"domain": "example.com", "company": "Example SA"},
                                 {"domain": "example.org", "company": "Other"}])
    assert (r["inserted"], r["updated"]) == (1, 1)
    assert sorted(d["company"] for d in _lignes(ns)) == ["Example SA", "Other"]


def test_un_tableau_non_ouvert_est_un_404(monde):
    with pytest.raises(AuthzDenied) as e:
        _ecrire(monde["org_a"], monde["t"]["a2"], [{"domain": "x.example"}])
    assert (e.value.status, e.value.code) == (404, "datastore_not_found")
    assert _lignes(monde["t"]["a2"]) == []


def test_un_tableau_ouvert_d_une_autre_org_que_le_chemin_est_un_404(monde):
    """Le tableau de B est ouvert au service, mais le chemin dit A : refusé, et rien
    n'est écrit chez B. Le même refus, chemin B et tableau de A."""
    m = monde
    _ouvrir(m["admin_b"], m["org_b"], m["t"]["b"])
    _ouvrir(m["admin"], m["org_a"], m["t"]["a"])
    for org, ns in ((m["org_a"], m["t"]["b"]), (m["org_b"], m["t"]["a"])):
        avant = _lignes(ns)
        with pytest.raises(AuthzDenied) as e:
            _ecrire(org, ns, [{"domain": "croise.example"}])
        assert (e.value.status, e.value.code) == (404, "datastore_not_found")
        assert _lignes(ns) == avant


def test_un_tableau_referme_est_un_404(monde):
    m, ns = monde, monde["t"]["a2"]
    _ouvrir(m["admin"], m["org_a"], ns)
    _ecrire(m["org_a"], ns, [{"domain": "avant.example"}])
    assert _refermer(m["admin"], m["org_a"], ns)["ok"] is True
    with pytest.raises(AuthzDenied) as e:
        _ecrire(m["org_a"], ns, [{"domain": "apres.example"}])
    assert e.value.status == 404
    with pytest.raises(AuthzDenied) as e:
        _refermer(m["admin"], m["org_a"], ns)
    assert (e.value.status, e.value.code) == (404, "service_writer_not_found")


def test_un_tableau_se_designe_par_son_numero(monde):
    with pytest.raises(AuthzDenied) as e:
        _ecrire(monde["org_a"], "va_nom", [{"domain": "x.example"}])
    assert (e.value.status, e.value.code) == (400, "datastore_by_id_required")


def test_une_org_inconnue_est_un_404(monde):
    with pytest.raises(AuthzDenied) as e:
        _ecrire(10_000_000, monde["t"]["a"], [{"domain": "x.example"}])
    assert (e.value.status, e.value.code) == (404, "unknown_org")


def test_une_org_suspendue_n_est_pas_ecrite(monde):
    from oto_mcp import org_store, org_suspension
    m, ns = monde, monde["t"]["a"]
    _ouvrir(m["admin"], m["org_a"], ns)
    org_store.suspend_org(m["org_a"], by="svc", reason="trial_ended")
    org_suspension.invalider()
    try:
        with pytest.raises(AuthzDenied) as e:
            _ecrire(m["org_a"], ns, [{"domain": "suspendue.example"}])
        assert (e.value.status, e.value.code) == (409, "org_suspended")
    finally:
        org_store.resume_org(m["org_a"])
        org_suspension.invalider()


def test_une_colonne_non_declaree_est_refusee_comme_au_lot_rest(monde):
    m, ns = monde, monde["t"]["a"]
    _ouvrir(m["admin"], m["org_a"], ns)
    with pytest.raises(AuthzDenied) as e:
        _ecrire(m["org_a"], ns, [{"domain": "x.example", "inconnue": 1}])
    assert e.value.status == 400


# ── 4. par les vraies routes REST ────────────────────────────────────────────
#
# La personne passe par son jeton (verifier simulé, comme les autres bancs REST) ; le
# service par un VRAI JWT ES384 adressé à `…/api/service`, vérifié par le vrai verifier des
# services — seul le JWKS est servi localement (`tests/test_service_identity_1068.py`).

_LOGTO = "https://annuaire.exemple.test"
_AUD_SERVICES = "https://mcp.exemple.test/api/service"   # conftest : adresse publique


class _Claims:
    def __init__(self, sub: str):
        self.claims = {"sub": sub, "email": f"{sub}@example.test", "name": sub}


class _Verifier:
    async def verify_token(self, token: str):
        return _Claims(token)


@pytest.fixture
def rest(monde, monkeypatch):
    from authlib.jose import JsonWebKey
    from fastmcp.server.auth.providers.jwt import JWTVerifier
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from oto_mcp.api import routes as api_routes
    cle = JsonWebKey.generate_key("EC", "P-384", is_private=True)
    pem = JsonWebKey.import_key(cle.as_dict(is_private=False)).as_pem().decode()
    monkeypatch.setenv("LOGTO_ENDPOINT", _LOGTO)
    service_identity._verifier_pour.cache_clear()

    async def cle_servie(self, token):
        return pem
    monkeypatch.setattr(JWTVerifier, "_get_verification_key", cle_servie)
    client = TestClient(Starlette(routes=api_routes.make_routes(_Verifier(), mcp_instance=None)))
    yield client, cle
    service_identity.set_current(None)
    service_identity._verifier_pour.cache_clear()


def _jeton_service(cle, scope="visitors", client="m2m-visitors") -> str:
    import time

    from authlib.jose import jwt as authlib_jwt
    now = int(time.time())
    claims = {"iss": f"{_LOGTO}/oidc", "aud": _AUD_SERVICES, "sub": client,
              "client_id": client, "scope": scope, "iat": now, "exp": now + 600}
    return authlib_jwt.encode({"alg": "ES384", "kid": "k1"}, claims, cle).decode()


def test_par_les_routes_la_personne_ouvre_puis_le_service_ecrit(rest, monde):
    """Le geste du front tel qu'il part (un `PUT` SANS corps), puis le lot du worker tel
    qu'il part (`{"rows": […], "key": "domain"}`) : la ligne est dans le tableau, et son
    historique porte le service, pas une personne."""
    from oto_mcp import db
    client, cle = rest
    m, ns = monde, monde["t"]["a"]
    h = {"Authorization": f"Bearer {m['admin']}", "X-Oto-Org": str(m["org_a"])}
    r = client.put(f"/api/datastores/{ns}/service-writers/visitors", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "visitors"
    r = client.get(f"/api/datastores/{ns}/service-writers", headers=h)
    assert [w["role"] for w in r.json()["service_writers"]] == ["visitors"]

    hs = {"Authorization": f"Bearer {_jeton_service(cle)}"}
    r = client.post(f"/api/service/orgs/{m['org_a']}/datastores/{ns}/rows/batch", headers=hs,
                    json={"rows": [{"domain": "route.example", "company": "Route"}],
                          "key": "domain"})
    assert r.status_code == 200, r.text
    assert (r.json()["inserted"], r.json()["ns_id"]) == (1, ns)
    assert any(d.get("domain") == "route.example" for d in _lignes(ns))
    with db._connect() as conn:
        acteurs = {r["acteur"] for r in conn.execute(
            "SELECT acteur FROM datastore_row_revisions WHERE ns_id = %s", (ns,)).fetchall()}
    assert "service:m2m-visitors" in acteurs and m["admin"] not in acteurs

    # Le même jeton, chemin d'une autre org : le même 404, rien d'écrit chez elle.
    r = client.post(f"/api/service/orgs/{m['org_b']}/datastores/{ns}/rows/batch", headers=hs,
                    json={"rows": [{"domain": "croise.example"}], "key": "domain"})
    assert r.status_code == 404, r.text


def test_par_les_routes_le_commerce_et_une_personne_n_ecrivent_pas(rest, monde):
    client, cle = rest
    m, ns = monde, monde["t"]["a"]
    url = f"/api/service/orgs/{m['org_a']}/datastores/{ns}/rows/batch"
    corps = {"rows": [{"domain": "x.example"}], "key": "domain"}
    r = client.post(url, json=corps,
                    headers={"Authorization": f"Bearer {_jeton_service(cle, 'commerce')}"})
    assert r.status_code == 403, r.text
    r = client.post(url, json=corps, headers={"Authorization": f"Bearer {m['admin']}"})
    assert r.status_code == 403, r.text
    # Un service n'ouvre pas un tableau lui-même.
    r = client.put(f"/api/datastores/{ns}/service-writers/visitors",
                   headers={"Authorization": f"Bearer {_jeton_service(cle)}"})
    assert r.status_code == 403, r.text
