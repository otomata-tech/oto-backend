"""`me.app.call` — une app ouverte ne LIT que si son AUTEUR et son LECTEUR le peuvent,
et que le tableau vit dans l'org du projet de l'app.

On monkeypatche l'app, l'ownership, l'org du projet et les capacités exécutées pour le
lecteur : ce qu'on vérifie ici, c'est le jugement (qui, quoi, borné à quoi), pas le
stockage. Assertions sur le CODE des refus.
"""
import asyncio
import types

import pytest
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

from oto_mcp import db, ownership
from oto_mcp.capabilities import apps_appel as C
from oto_mcp.capabilities import registry
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
from oto_mcp.capabilities.datastore import rows as R

LECTEUR = "lecteur"
AUTEUR = "auteur"
ORG_DU_PROJET = 42
CTX = ResolvedCtx(sub=LECTEUR, org_id=43)  # l'org active du lecteur n'est PAS celle du projet
MANIFEST = {
    "tables": {"leads": {"table": "12", "fields": ["name", "status"],
                         "set": {"status": ["ok", "no"]}, "add": ["name"]},
               "all": {"table": "13", "fields": None, "set": {}, "add": []}},
    "agents": {"enrich": {"id": 9, "run": True, "input": True}},
}
APP = {"id": 5, "owner_type": "user", "owner_id": "auteur", "context_org_id": 42, "title": "Leads", "manifest": MANIFEST,
       "thumbnail": None, "updated_by": AUTEUR, "archived_at": None}


@pytest.fixture
def seams(monkeypatch):
    rec = {"list": [], "agg": []}
    # Ce que chacun peut : par défaut, tout ; un test retire ce qu'il éprouve.
    droits = {(LECTEUR, "app"): True, (AUTEUR, "12", "read"): True,
              (AUTEUR, "13", "read"): True}
    org_des_tableaux = {"12": ORG_DU_PROJET, "13": ORG_DU_PROJET}

    def can_access(sub, rtype, rid, want="read"):
        if rtype == "app":
            return droits.get((sub, "app"), False)
        return droits.get((sub, rid, want), False)

    monkeypatch.setattr(ownership, "can_access", can_access)
    monkeypatch.setattr(db.apps, "get_for_call", lambda i: dict(APP) if i == 5 else None)
    monkeypatch.setattr(C, "org_de_l_app", lambda app: ORG_DU_PROJET if app.get("id") == 5 else None)
    monkeypatch.setattr(C, "tableau_de_l_org",
                        lambda ns, org: org is not None and org_des_tableaux.get(ns) == org)
    monkeypatch.setattr(R, "_list_rows", lambda ctx, inp: rec["list"].append((ctx.sub, inp)) or {
        "rows": [{"_id": "r1", "_revision": "1", "name": "A", "status": "ok", "email": "x@y"}], "total": 1})
    monkeypatch.setattr(R, "_aggregate", lambda ctx, inp: rec["agg"].append(inp) or {"groups": [{"n": 1}]})
    rec["droits"], rec["orgs"] = droits, org_des_tableaux
    return rec


def call(**kw):
    return C._call(CTX, C.AppCallInput(app_id=5, **kw))


def refus(**kw) -> AuthzDenied:
    with pytest.raises(AuthzDenied) as e:
        call(**kw)
    return e.value


# ── La surface ──────────────────────────────────────────────────────────────

def test_la_capacite_n_a_pas_de_jeton_api_ni_d_outil_mcp():
    for cle in ("me.app.call", "me.app.consent"):
        cap = next(c for c in registry.CAPABILITIES if c.key == cle)
        assert cap.mcp is None
        (b,) = cap.rest_bindings()
        assert b.allow_api_token is False and b.allow_issuer_token is False


# ── Qui ─────────────────────────────────────────────────────────────────────

def test_le_lecteur_lit_comme_lui_et_seulement_les_champs_declares(seams):
    out = call(action="read_rows", table="leads")
    (sub, inp), = seams["list"]
    assert sub == LECTEUR and inp.datastore == "12"
    assert out["rows"] == [{"_id": "r1", "_revision": "1", "name": "A", "status": "ok"}]


def test_app_illisible_pour_le_lecteur_meme_404_qu_inconnue(seams):
    seams["droits"][(LECTEUR, "app")] = False
    assert refus(action="read_rows", table="leads").code == "unknown_app"
    with pytest.raises(AuthzDenied) as e:
        C._call(CTX, C.AppCallInput(app_id=404, action="read_rows", table="leads"))
    assert e.value.code == "unknown_app"


def test_l_auteur_qui_perd_le_tableau_coupe_l_app(seams):
    seams["droits"][(AUTEUR, "12", "read")] = False
    e = refus(action="read_rows", table="leads")
    assert (e.status, e.code) == (403, "app_author_lost_access")
    assert seams["list"] == []


def test_auteur_inconnu_refuse(seams, monkeypatch):
    monkeypatch.setattr(db.apps, "get_for_call", lambda i: dict(APP, updated_by=None))
    assert refus(action="read_rows", table="leads").code == "app_author_unknown"


# ── Où : l'org du projet, pas celle de l'auteur ni du lecteur ───────────────

def test_tableau_sorti_de_l_org_du_projet_refuse(seams):
    seams["orgs"]["12"] = 99
    e = refus(action="read_rows", table="leads")
    assert (e.status, e.code) == (403, "table_outside_project_org")
    e = refus(action="read_aggregate", table="leads")
    assert e.code == "table_outside_project_org"
    assert seams["list"] == [] and seams["agg"] == []


def test_projet_sans_org_ne_sert_aucun_tableau(seams, monkeypatch):
    monkeypatch.setattr(C, "org_de_l_app", lambda app: None)
    assert refus(action="read_rows", table="leads").code == "table_outside_project_org"


# ── Quoi : borné au manifeste ───────────────────────────────────────────────

def test_tableau_non_declare(seams):
    assert refus(action="read_rows", table="secret").code == "undeclared_table"


@pytest.mark.parametrize("options", [{"filter": {"email": "x"}}, {"order_by": "email"}, {"q": "x"}])
def test_lecture_ne_touche_pas_un_champ_non_declare(seams, options):
    assert refus(action="read_rows", table="leads", options=options).status in (400, 403)
    assert seams["list"] == []


@pytest.mark.parametrize("options", [{"limit": "beaucoup"}, {"offset": "x"}, {"limit": 1.5},
                                     {"limit": True}])
def test_limite_non_numerique_400(seams, options):
    e = refus(action="read_rows", table="leads", options=options)
    assert (e.status, e.code) == (400, "invalid_options")
    assert seams["list"] == []


def test_limite_bornee(seams):
    call(action="read_rows", table="leads", options={"limit": 10_000, "offset": -3})
    inp = seams["list"][0][1]
    assert inp.limit == C.MAX_ROWS and inp.offset == 0


def test_filtre_sur_les_dates_permis(seams):
    call(action="read_rows", table="leads", options={"filter": {"_created_at": {"gte": "2026-10-01"}}})
    assert seams["list"]


def test_recherche_permise_sans_liste_de_champs(seams):
    call(action="read_rows", table="all", options={"q": "acme"})
    assert seams["list"][0][1].q == "acme"


def test_agregat_borne(seams):
    call(action="read_aggregate", table="leads", options={"group_by": "status"})
    assert seams["agg"][0].group_by == "status"
    assert refus(action="read_aggregate", table="leads", options={"group_by": "email"}).code == "undeclared_field"
    assert refus(action="read_aggregate", table="leads",
                 options={"metrics": [{"op": "sum", "field": "email"}]}).code == "undeclared_field"


# ── La porte : un jeton `oto_` est refusé avant le handler ─────────────────

@pytest.mark.parametrize("cle,chemin", [("me.app.call", "/api/me/apps/5/call"),
                                        ("me.app.consent", "/api/me/apps/5/consent"),
                                        ("me.app", "/api/me/apps")])
def test_un_jeton_api_ou_delegue_est_refuse_sur_la_route_servie(monkeypatch, cle, chemin):
    """Rejoue l'adaptateur REST avec la VRAIE authentification : un porteur `oto_`
    (jeton API ou de délégation d'agent hébergé) prend 403 `api_token_forbidden`,
    et le handler ne tourne pas."""
    from oto_mcp.api import routes as api_routes
    from oto_mcp.auth import token_scopes
    from oto_mcp.capabilities import _rest_adapter

    lu = []
    monkeypatch.setattr(api_routes.db, "verify_api_token",
                        lambda t: lu.append(t) or {"sub": "u-1", "scopes": None,
                                                   "token_kind": "delegation"})
    cap = next(c for c in registry.CAPABILITIES if c.key == cle)
    (binding,) = cap.rest_bindings()

    class _Verifier:
        async def verify_token(self, token):
            return types.SimpleNamespace(claims={"sub": "u-jwt"})

    handler = _rest_adapter._make_handler(
        cap, binding, _Verifier(), api_routes._authenticate,
        lambda _r, p, status=200: JSONResponse(p, status_code=status),
        lambda _r, s, code, d=None: JSONResponse({"error": code}, status_code=s))
    req = Request({
        "type": "http", "method": "POST", "path": chemin, "query_string": b"",
        "root_path": "", "scheme": "http", "server": ("test", 80), "http_version": "1.1",
        "path_params": {"app_id": "5"} if chemin != "/api/me/apps" else {},
        "headers": [(b"authorization", b"Bearer oto_deadbeef"),
                    (b"content-type", b"application/json")],
    }, receive=lambda: asyncio.sleep(0, {"type": "http.request", "body": b"{}"}))
    rep = asyncio.run(handler(req))
    token_scopes.set_current(None)
    assert rep.status_code == 403
    assert b"api_token_forbidden" in bytes(rep.body)
    assert lu == []  # refusé avant même de chercher le jeton
