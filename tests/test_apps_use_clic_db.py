"""Use et View de bout en bout, contre un PostgreSQL réel, sans double des capacités de
lignes. Use fait cliquer ; il ne donne AUCUN droit sur ce que l'app touche : le geste
reste jugé aux droits du lecteur, bornés par l'auteur. View lit sans cliquer. Un partage
Use à une org ou à une équipe vaut pour ses membres. L'app d'une org suspendue se ferme
à ses lecteurs d'une autre org ; une org d'appel suspendue reste refusée."""
from __future__ import annotations

import uuid

import pytest

HOTE = "https://manage.oto.cx"   # une origine que le banc déclare (`conftest.ORIGINES_CORS`)
SCHEMA = {"fields": [{"key": "name", "type": "text"}, {"key": "status", "type": "text"}]}


@pytest.fixture
def monde(live, monkeypatch):
    from oto_mcp import db, org_store
    from oto_mcp.capabilities import resources as R
    from oto_mcp.datastore.core import make_store
    monkeypatch.setattr(R.email, "send_resource_shared_email", lambda to, **kw: True)
    u = uuid.uuid4().hex[:8]
    auteur, lecteur = f"auteur_{u}", f"lecteur_{u}"
    for s in (auteur, lecteur):
        db.upsert_user(s, email=f"{s}@example.test")
    org = org_store.create_org(f"o_{u}", created_by=auteur)
    org_store.add_org_member(org, auteur, "org_admin")
    ns = f"leads_{u}"
    ns_id = db.create_datastore("user", auteur, ns, context_org_id=org)
    st = make_store(auteur)
    st.set_schema(str(ns_id), SCHEMA)
    ligne = st.append_row(str(ns_id), {"name": "Acme", "status": "open"})
    manifest = {"tables": {"leads": {"table": str(ns_id), "fields": ["name", "status"],
                                     "set": {"status": ["won", "lost"]}, "add": []}}}
    app = db.apps.create(("user", auteur), org, title=f"Desk {u}", manifest=manifest,
                         html="", thumbnail=None, by=auteur)
    return {"auteur": auteur, "lecteur": lecteur, "org": org, "ns_id": ns_id,
            "app": int(app["id"]), "row": ligne["_id"], "rev": ligne["_revision"], "u": u}


def _partager_app(m, role, **principal):
    from oto_mcp.capabilities import resources as R
    from oto_mcp.capabilities._types import ResolvedCtx
    from oto_mcp.capabilities.resources_v2 import ResourceInputV2
    audience = ("org" if "org_id" in principal else "team" if "group_id" in principal
                else "person")
    principal = principal or {"email": f"{m['lecteur']}@example.test"}
    return R._resources(ResolvedCtx(sub=m["auteur"], org_id=m["org"]),
                        ResourceInputV2(op="share", resource_type="app",
                                        resource_id=str(m["app"]), audience=audience,
                                        role=role, **principal))


def _tableau_au_lecteur(m, role):
    from oto_mcp import db
    db.grant_resource("datastore_namespace", str(m["ns_id"]), "user", m["lecteur"],
                      role=role, granted_by=m["auteur"])


def _comme(sub):
    """Ce que l'adaptateur REST pose pour l'écran hôte : session interactive, origine
    déclarée."""
    from contextlib import contextmanager

    from oto_mcp import client_trace, geste

    @contextmanager
    def ctx():
        jeton = client_trace.set_current(ip=None, user_agent=None, origin=HOTE, fetch_dest="empty")
        try:
            with geste.portee(geste.CONSOLE, sub, f"g-{uuid.uuid4().hex[:6]}"):
                yield
        finally:
            client_trace.reset(jeton)
    return ctx()


def _ctx(m, sub, org=None):
    from oto_mcp.capabilities._types import ResolvedCtx
    return ResolvedCtx(sub=sub, org_id=org or m["org"], channel="rest")


def _appel(m, sub, org=None, **kw):
    from oto_mcp.capabilities import apps_appel as C
    return C._call(_ctx(m, sub, org), C.AppCallInput(app_id=m["app"], **kw))


def _accord(m, sub, **kw):
    from oto_mcp.capabilities import apps_appel as C
    return C._consent(_ctx(m, sub), C.AppCallInput(app_id=m["app"], **kw))


def _ecrire(m):
    return dict(action="write_cell", table="leads", row_id=m["row"], field="status",
                value="won", expected_revision=m["rev"])


def test_use_clique_avec_ses_propres_droits_sur_le_tableau(monde):
    from oto_mcp import db
    m = monde
    _partager_app(m, "contributor")
    _tableau_au_lecteur(m, "editor")
    with _comme(m["lecteur"]):
        accord = _accord(m, m["lecteur"], **_ecrire(m))
        assert accord["summary"]["text"].endswith("as you.")
        out = _appel(m, m["lecteur"], consent=accord["consent"], **_ecrire(m))
    assert out["row"]["status"] == "won"
    with db._connect() as conn:
        dernier = conn.execute(
            "SELECT acteur FROM datastore_row_revisions WHERE ns_id = %s AND row_id = %s "
            "ORDER BY id DESC LIMIT 1", (m["ns_id"], m["row"])).fetchone()
    assert dernier["acteur"] == m["lecteur"]


def test_use_ne_donne_aucun_acces_au_tableau(monde):
    """Use sans accès propre au tableau : ni lecture ni écriture par l'app."""
    from oto_mcp.capabilities._types import AuthzDenied
    m = monde
    _partager_app(m, "contributor")
    with pytest.raises(AuthzDenied):
        _appel(m, m["lecteur"], action="read_rows", table="leads")
    with _comme(m["lecteur"]):
        accord = _accord(m, m["lecteur"], **_ecrire(m))
        with pytest.raises(AuthzDenied) as e:
            _appel(m, m["lecteur"], consent=accord["consent"], **_ecrire(m))
    assert e.value.code != "app_view_only"
    from oto_mcp.datastore.core import make_store
    (ligne,) = make_store(m["auteur"]).list_rows(str(m["ns_id"]))
    assert ligne["status"] == "open"


def test_view_lit_et_ne_clique_pas_meme_s_il_peut_ecrire_le_tableau(monde):
    from oto_mcp.capabilities import apps as A
    from oto_mcp.capabilities._types import AuthzDenied
    m = monde
    _partager_app(m, "viewer")
    _tableau_au_lecteur(m, "editor")
    lu = _appel(m, m["lecteur"], action="read_rows", table="leads")
    assert [r["name"] for r in lu["rows"]] == ["Acme"]
    with _comme(m["lecteur"]):
        with pytest.raises(AuthzDenied) as e:
            _accord(m, m["lecteur"], **_ecrire(m))
    assert (e.value.status, e.value.code) == (403, "app_view_only")
    with pytest.raises(AuthzDenied) as e:
        _appel(m, m["lecteur"], consent="x", **_ecrire(m))
    assert e.value.code == "app_view_only"
    vue = A._app(_ctx(m, m["lecteur"]), A.AppInput(op="get", app_id=m["app"]))["app"]
    assert vue["can_use"] is False


def test_use_d_org_et_d_equipe_vaut_pour_leurs_membres(monde):
    from oto_mcp import db, group_store, org_store
    from oto_mcp.capabilities import apps_appel as C
    m = monde
    org_store.add_org_member(m["org"], m["lecteur"], "org_member")
    assert not C.peut_cliquer(m["lecteur"], m["app"])
    gid = group_store.create_group(m["org"], f"g_{m['u']}", created_by=m["auteur"])
    group_store.add_group_member(gid, m["lecteur"])
    _partager_app(m, "contributor", group_id=gid)
    assert C.peut_cliquer(m["lecteur"], m["app"])
    db.revoke_resource_grant("app", str(m["app"]), "group", str(gid))
    assert not C.peut_cliquer(m["lecteur"], m["app"])
    _partager_app(m, "contributor", org_id=m["org"])
    assert C.peut_cliquer(m["lecteur"], m["app"])
    # L'écran des apps distingue Use de View pour « toute l'org ».
    assert db.apps.visibilites({m["app"]: m["org"]})[m["app"]]["org"] == "contributor"
    _partager_app(m, "viewer", org_id=m["org"])
    assert db.apps.visibilites({m["app"]: m["org"]})[m["app"]]["org"] == "viewer"
    assert not C.peut_cliquer(m["lecteur"], m["app"])


def test_l_org_de_l_app_suspendue_ferme_l_app_et_n_ouvre_rien(monde):
    from oto_mcp import org_store, org_suspension
    from oto_mcp.capabilities import apps as A
    from oto_mcp.capabilities._types import AuthzDenied
    m = monde
    _partager_app(m, "contributor")
    _tableau_au_lecteur(m, "editor")
    maison = org_store.ensure_home_org(m["lecteur"], f"{m['lecteur']}@example.test")
    assert maison != m["org"]
    ctx = _ctx(m, m["lecteur"], maison)
    try:
        # L'org d'appel suspendue : l'adaptateur refuse, comme avant.
        org_store.suspend_org(maison, by="test", reason="trial")
        org_suspension.invalider()
        with pytest.raises(AuthzDenied) as e:
            org_suspension.garde_capacite("me.app.call", ctx)
        assert e.value.code == "org_suspended"
        org_store.resume_org(maison)
        org_suspension.invalider()
        assert [r["name"] for r in _appel(m, m["lecteur"], maison, action="read_rows",
                                          table="leads")["rows"]] == ["Acme"]
        # L'org de l'APP suspendue : l'app se ferme au lecteur d'une autre org.
        org_store.suspend_org(m["org"], by="test", reason="trial")
        org_suspension.invalider()
        for geste in (lambda: A._app(ctx, A.AppInput(op="get", app_id=m["app"])),
                      lambda: _appel(m, m["lecteur"], maison, action="read_rows",
                                     table="leads")):
            with pytest.raises(AuthzDenied) as e:
                geste()
            assert e.value.code == "org_suspended"
    finally:
        org_store.resume_org(maison)
        org_store.resume_org(m["org"])
        org_suspension.invalider()
