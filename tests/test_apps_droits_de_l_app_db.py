"""Les droits de l'APP de bout en bout, contre un PostgreSQL réel, sans aucun double
des capacités de lignes : un auteur qui possède un tableau perso le sert, par son app,
à une collègue qui a Use sur l'app et AUCUN accès au tableau — elle le lit, y écrit
d'un clic consenti, et le journal des révisions la nomme, elle. View lit sans cliquer ;
un auteur qui ne peut pas partager le tableau ne le sert pas."""
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
    auteur, use, view = f"auteur_{u}", f"use_{u}", f"view_{u}"
    for s in (auteur, use, view):
        db.upsert_user(s, email=f"{s}@example.test")
    org = org_store.create_org(f"o_{u}", created_by=auteur)
    org_store.add_org_member(org, auteur, "org_admin")
    ns = f"leads_{u}"
    ns_id = db.create_datastore("user", auteur, ns, context_org_id=org)
    st = make_store(auteur)
    st.set_schema(ns, SCHEMA)
    ligne = st.append_row(ns, {"name": "Acme", "status": "open"})
    manifest = {"tables": {"leads": {"table": str(ns_id), "fields": ["name", "status"],
                                     "set": {"status": ["won", "lost"]}, "add": []}}}
    app = db.apps.create(("user", auteur), org, title=f"Desk {u}", manifest=manifest,
                         html="", thumbnail=None, by=auteur)
    return {"auteur": auteur, "use": use, "view": view, "org": org, "ns_id": ns_id,
            "app": int(app["id"]), "row": ligne["_id"], "rev": ligne["_revision"], "u": u}


def _partager(m, email, role):
    from oto_mcp.capabilities import resources as R
    from oto_mcp.capabilities._types import ResolvedCtx
    from oto_mcp.capabilities.resources_v2 import ResourceInputV2
    return R._resources(ResolvedCtx(sub=m["auteur"], org_id=m["org"]),
                        ResourceInputV2(op="share", resource_type="app",
                                        resource_id=str(m["app"]), audience="person",
                                        email=email, role=role))


def _comme(sub):
    """Ce que l'adaptateur REST pose pour l'écran hôte de `sub` : session interactive,
    origine déclarée."""
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


def _appel(m, sub, **kw):
    from oto_mcp.capabilities import apps_appel as C
    from oto_mcp.capabilities._types import ResolvedCtx
    return C._call(ResolvedCtx(sub=sub, org_id=m["org"], channel="rest"),
                   C.AppCallInput(app_id=m["app"], **kw))


def _accord(m, sub, **kw):
    from oto_mcp.capabilities import apps_appel as C
    from oto_mcp.capabilities._types import ResolvedCtx
    return C._consent(ResolvedCtx(sub=sub, org_id=m["org"], channel="rest"),
                      C.AppCallInput(app_id=m["app"], **kw))


def test_use_lit_et_ecrit_par_l_app_et_le_journal_la_nomme(monde):
    from oto_mcp import db, ownership
    m = monde
    _partager(m, f"{m['use']}@example.test", "contributor")
    assert not ownership.can_access(m["use"], ownership.TYPE_RESSOURCE_DATASTORE,
                                    str(m["ns_id"]), "read"), "aucun accès propre au tableau"
    lu = _appel(m, m["use"], action="read_rows", table="leads")
    assert [r["name"] for r in lu["rows"]] == ["Acme"]

    ecrire = dict(action="write_cell", table="leads", row_id=m["row"], field="status",
                  value="won", expected_revision=m["rev"])
    with _comme(m["use"]):
        accord = _accord(m, m["use"], **ecrire)
        assert "through this app" in accord["summary"]["text"]
        out = _appel(m, m["use"], consent=accord["consent"], **ecrire)
    assert out["row"]["status"] == "won"
    with db._connect() as conn:
        acteurs = {r["acteur"] for r in conn.execute(
            "SELECT acteur FROM datastore_row_revisions WHERE ns_id = %s AND row_id = %s "
            "AND acteur IS NOT NULL", (m["ns_id"], m["row"])).fetchall()}
    assert acteurs == {m["use"]}, acteurs   # le clic, jamais l'auteur sous qui le store écrit
    with db._connect() as conn:
        dernier = conn.execute(
            "SELECT acteur FROM datastore_row_revisions WHERE ns_id = %s AND row_id = %s "
            "ORDER BY id DESC LIMIT 1", (m["ns_id"], m["row"])).fetchone()
    assert dernier["acteur"] == m["use"]


def test_view_lit_et_ne_clique_pas(monde):
    from oto_mcp.capabilities._types import AuthzDenied
    m = monde
    _partager(m, f"{m['view']}@example.test", "viewer")
    assert [r["name"] for r in _appel(m, m["view"], action="read_rows", table="leads")["rows"]] == ["Acme"]
    with _comme(m["view"]):
        with pytest.raises(AuthzDenied) as e:
            _accord(m, m["view"], action="write_cell", table="leads", row_id=m["row"],
                    field="status", value="won", expected_revision=m["rev"])
    assert e.value.code == "app_view_only"


def test_un_auteur_qui_ne_peut_pas_partager_ne_sert_pas_le_tableau(monde):
    """Le tableau passe à l'org (l'auteur, simple membre, ne le gouverne plus) : la
    collègue du dehors n'y a pas accès, l'app ne le lui sert pas."""
    from oto_mcp import db, org_store, ownership
    from oto_mcp.capabilities._types import AuthzDenied
    m = monde
    _partager(m, f"{m['use']}@example.test", "contributor")
    ownership.transfer(ownership.TYPE_RESSOURCE_DATASTORE, str(m["ns_id"]), "org", str(m["org"]))
    org_store.add_org_member(m["org"], m["auteur"], "org_member")
    assert not ownership.can_govern(m["auteur"], ownership.TYPE_RESSOURCE_DATASTORE, str(m["ns_id"]))
    with pytest.raises(AuthzDenied):
        _appel(m, m["use"], action="read_rows", table="leads")
    assert db.apps.get_for_call(m["app"]) is not None


def test_l_invite_dont_l_espace_est_suspendu_ouvre_toujours_l_app(monde):
    """Une app se juge par l'org de l'APP : la maison suspendue d'un invité du dehors
    (essai fini) n'éteint pas l'app d'une org qui paie ; l'org de l'app suspendue la
    ferme à tous."""
    from oto_mcp import org_store, org_suspension
    from oto_mcp.capabilities import apps as A
    from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
    m = monde
    _partager(m, f"{m['use']}@example.test", "contributor")
    maison = org_store.ensure_home_org(m["use"], f"{m['use']}@example.test")
    assert maison != m["org"]
    ctx = ResolvedCtx(sub=m["use"], org_id=maison)
    org_store.suspend_org(maison, by="test", reason="trial")
    org_suspension.invalider()
    # La garde de l'adaptateur la laisse passer ; le handler juge l'org de l'app.
    org_suspension.garde_capacite("me.app.call", ctx)
    org_suspension.garde_capacite("me.app", ctx)
    assert A._app(ctx, A.AppInput(op="get", app_id=m["app"]))["app"]["can_use"] is True
    assert [r["name"] for r in _appel(m, m["use"], action="read_rows", table="leads")["rows"]] == ["Acme"]
    # Lister ou créer reste jugé par l'org de l'appel, la maison suspendue.
    with pytest.raises(AuthzDenied) as e:
        A._app(ctx, A.AppInput(op="list"))
    assert e.value.code == "org_suspended"

    org_store.suspend_org(m["org"], by="test", reason="trial")
    org_suspension.invalider()
    for geste in (lambda: A._app(ctx, A.AppInput(op="get", app_id=m["app"])),
                  lambda: _appel(m, m["use"], action="read_rows", table="leads")):
        with pytest.raises(AuthzDenied) as e:
            geste()
        assert e.value.code == "org_suspended"
    org_store.resume_org(maison)
    org_store.resume_org(m["org"])
    org_suspension.invalider()
