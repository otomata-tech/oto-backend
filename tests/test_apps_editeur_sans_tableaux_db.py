"""Un éditeur d'une app qui n'a reçu aucun de ses tableaux la modifie quand même, contre
un PostgreSQL réel, sans double : l'auteur (celui dont les droits bornent l'app) RESTE
celui d'avant, `edited_by` dit qui a écrit, et un lecteur qui a le tableau est toujours
servi après l'écriture. L'éditeur ne lit rien de plus par l'app, et ne change pas ce
qu'elle déclare (`capabilities/apps.py::_auteur_garde`)."""
from __future__ import annotations

import uuid

import pytest

SCHEMA = {"fields": [{"key": "name", "type": "text"}, {"key": "status", "type": "text"}]}


@pytest.fixture
def monde(live, monkeypatch):
    from oto_mcp import db, org_store
    from oto_mcp.capabilities import resources as R
    from oto_mcp.datastore.core import make_store
    monkeypatch.setattr(R.email, "send_resource_shared_email", lambda to, **kw: True)
    u = uuid.uuid4().hex[:8]
    auteur, editeur, lecteur = f"auteur_{u}", f"editeur_{u}", f"lecteur_{u}"
    for s in (auteur, editeur, lecteur):
        db.upsert_user(s, email=f"{s}@example.test")
    org = org_store.create_org(f"o_{u}", created_by=auteur)
    org_store.add_org_member(org, auteur, "org_admin")
    ns_id = db.create_datastore("user", auteur, f"leads_{u}", context_org_id=org)
    st = make_store(auteur)
    st.set_schema(str(ns_id), SCHEMA)
    st.append_row(str(ns_id), {"name": "Acme", "status": "open"})
    manifest = {"tables": {"leads": {"table": str(ns_id), "fields": ["name", "status"]}}}
    app = db.apps.create(("user", auteur), org, title=f"Desk {u}", manifest=manifest,
                         html="<p>v1</p>", thumbnail=None, by=auteur)
    # L'éditeur modifie l'app, sans le tableau ; le lecteur la lit, avec le tableau.
    db.grant_resource("app", str(app["id"]), "user", editeur, role="editor",
                      granted_by=auteur)
    db.grant_resource("app", str(app["id"]), "user", lecteur, role="viewer",
                      granted_by=auteur)
    db.grant_resource("datastore_namespace", str(ns_id), "user", lecteur, role="viewer",
                      granted_by=auteur)
    return {"auteur": auteur, "editeur": editeur, "lecteur": lecteur, "org": org,
            "ns_id": ns_id, "app": int(app["id"])}


def _ctx(m, sub):
    from oto_mcp.capabilities._types import ResolvedCtx
    return ResolvedCtx(sub=sub, org_id=m["org"], channel="rest")


def _app(m, sub, **kw):
    from oto_mcp.capabilities import apps as A
    return A._app(_ctx(m, sub), A.AppInput(app_id=m["app"], **kw))


def _lire(m, sub):
    from oto_mcp.capabilities import apps_appel as C
    return C._call(_ctx(m, sub), C.AppCallInput(app_id=m["app"], action="read_rows",
                                                table="leads"))


def _ligne(m):
    from oto_mcp import db
    return db.apps.get(m["app"])


def test_l_editeur_ecrit_l_auteur_reste_et_le_lecteur_est_servi(monde):
    from oto_mcp.capabilities._types import AuthzDenied
    m = monde
    out = _app(m, m["editeur"], op="patch", edits=[{"find": "v1", "replace": "v2"}])
    assert out["target"] == "live"
    app = _ligne(m)
    assert app["html"] == "<p>v2</p>"
    assert (app["updated_by"], app["edited_by"]) == (m["auteur"], m["editeur"])
    # Le lecteur qui a le tableau le lit toujours par l'app.
    assert [r["name"] for r in _lire(m, m["lecteur"])["rows"]] == ["Acme"]
    # L'éditeur, lui, ne lit rien de plus par l'app.
    with pytest.raises(AuthzDenied):
        _lire(m, m["editeur"])
    # Le titre, et le manifeste renvoyé tel quel avec une autre hauteur, aussi.
    _app(m, m["editeur"], op="update", title="Renommée")
    manifest = dict(_ligne(m)["manifest"], height=900)
    _app(m, m["editeur"], op="update", manifest=manifest)
    app = _ligne(m)
    assert (app["title"], app["manifest"]["height"]) == ("Renommée", 900)
    assert app["updated_by"] == m["auteur"]
    # L'historique nomme qui a écrit chaque état, pas l'auteur.
    revs = _app(m, m["auteur"], op="revisions")["revisions"]
    assert revs[0]["edited_by"] == m["editeur"]


def test_l_editeur_ne_change_pas_ce_que_l_app_declare(monde):
    from oto_mcp.capabilities._types import AuthzDenied
    m = monde
    avant = _ligne(m)
    plus_large = {"tables": {"leads": {"table": str(m["ns_id"])}}}   # tout lire
    with pytest.raises(AuthzDenied) as e:
        _app(m, m["editeur"], op="update", manifest=plus_large)
    assert e.value.code == "invalid_manifest"
    assert _ligne(m)["rev"] == avant["rev"]


def test_un_editeur_qui_a_le_tableau_devient_l_auteur(monde):
    from oto_mcp import db
    m = monde
    db.grant_resource("datastore_namespace", str(m["ns_id"]), "user", m["editeur"],
                      role="viewer", granted_by=m["auteur"])
    _app(m, m["editeur"], op="update", title="À moi")
    app = _ligne(m)
    assert (app["updated_by"], app["edited_by"]) == (m["editeur"], m["editeur"])


def test_brouillon_et_publication_sans_les_tableaux(monde):
    m = monde
    _app(m, m["auteur"], op="set_drafts", enabled=True)
    recu = _app(m, m["editeur"], op="patch", edits=[{"find": "v1", "replace": "v3"}])
    assert recu["target"] == "draft"
    brouillon = _app(m, m["editeur"], op="get")["draft"]
    assert (brouillon["updated_by"], brouillon["edited_by"]) == (m["auteur"], m["editeur"])
    _app(m, m["editeur"], op="publish", expected_rev=recu["rev"], note="v3")
    app = _ligne(m)
    assert app["html"] == "<p>v3</p>"
    assert (app["updated_by"], app["edited_by"]) == (m["auteur"], m["editeur"])
    assert [r["name"] for r in _lire(m, m["lecteur"])["rows"]] == ["Acme"]


def test_les_roles_comptent_comme_une_declaration():
    """Un manifeste qui ne change QUE ses rôles change ce qu'il ouvre : la comparaison
    les porte (une clé absente des deux côtés reste égale)."""
    from oto_mcp.capabilities import apps as A
    base = {"tables": {}, "agents": {}}
    assert A._ouvre({**base}) == A._ouvre({**base, "roles": None})
    assert A._ouvre({**base, "roles": {"user": {}}}) != A._ouvre(
        {**base, "roles": {"user": {"tables": {}}}})
    assert A._ouvre({**base, "default_role": "a"}) != A._ouvre({**base, "default_role": "b"})
