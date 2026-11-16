"""Brouillon et publication de bout en bout : la capacité `oto_apps` sur une vraie base.

`test_apps_versions.py` juge la capacité sur un brouillon en mémoire, et
`test_app_versions_db.py` le stockage seul ; ici le chemin entier — mode brouillon,
patch, lecture du brouillon, publication, historique, restauration vers le brouillon —
passe par `apps._app` et `db.app_versions` à la fois, sans double.
"""
from __future__ import annotations

import pytest

from oto_mcp import db
from oto_mcp.capabilities import apps as A
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx

MOI = "sub-apps-bout-en-bout"
CTX = ResolvedCtx(sub=MOI, org_id=None)


@pytest.fixture(scope="module")
def app_id(live):
    projet = int(db.create_project("user", MOI, "Bout en bout", created_by=MOI))
    app = db.apps.create(("user", MOI), None, project_id=projet, title="E2E",
                         manifest={}, html="<p>v1</p>", thumbnail=None, by=MOI)
    return int(app["id"])


def _app(app_id, **kw):
    return A._app(CTX, A.AppInput(app_id=app_id, **kw))


def test_du_brouillon_a_la_publication_et_retour(app_id):
    assert _app(app_id, op="set_drafts", enabled=True)["app"]["drafts_enabled"] is True

    # Le patch va au brouillon ; les lecteurs gardent v1.
    recu = _app(app_id, op="patch", edits=[{"find": "v1", "replace": "v2"}])
    assert recu["target"] == "draft"
    vivant = _app(app_id, op="get")
    assert vivant["app"]["html"] == "<p>v1</p>"
    assert vivant["draft"]["rev"] == recu["rev"]

    # Le brouillon se lit, et un second patch s'écrit contre SON rev.
    lu = _app(app_id, op="get", draft=True)
    assert (lu["app"]["html"], lu["app"]["rev"]) == ("<p>v2</p>", recu["rev"])
    with pytest.raises(AuthzDenied) as e:
        _app(app_id, op="update", html="<p>x</p>", expected_rev=vivant["app"]["rev"])
    assert e.value.code == "draft_exists"

    # Publier : v2 devient vivant, avec sa note ; v1 est dans l'historique.
    publie = _app(app_id, op="publish", expected_rev=recu["rev"], note="Second version")
    assert publie["app"]["html"] == "<p>v2</p>" and publie["app"]["version_note"] == "Second version"
    assert "draft" not in _app(app_id, op="get")
    (v1,) = _app(app_id, op="revisions")["revisions"]
    assert _app(app_id, op="revision", revision_id=v1["id"])["revision"]["html"] == "<p>v1</p>"

    # Restaurer v1 vers le brouillon : l'app publiée reste v2.
    out = _app(app_id, op="restore", revision_id=v1["id"], to_draft=True)
    assert out["target"] == "draft"
    assert _app(app_id, op="get")["app"]["html"] == "<p>v2</p>"
    assert _app(app_id, op="get", draft=True)["app"]["html"] == "<p>v1</p>"

    # Couper le mode brouillon avec un brouillon en cours : refusé ; jeté, accepté.
    with pytest.raises(AuthzDenied) as e:
        _app(app_id, op="set_drafts", enabled=False)
    assert e.value.code == "draft_pending"
    assert _app(app_id, op="delete_draft")["ok"] is True
    assert _app(app_id, op="set_drafts", enabled=False)["app"]["drafts_enabled"] is False
