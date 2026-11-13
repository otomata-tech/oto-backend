"""Le rôle Use d'une app (`contributor`), contre un PostgreSQL réel : il se pose sur une
app seulement, se lit `read` au plan contenu, et fait cliquer sans faire modifier ; les
runs qu'un clic lance au nom de l'auteur restent au lecteur qui a cliqué
(`app_consents.job_id`)."""
from __future__ import annotations

import uuid

import pytest


@pytest.fixture
def monde(live, monkeypatch):
    from oto_mcp import db, org_store
    from oto_mcp.capabilities import resources as R
    monkeypatch.setattr(R.email, "send_resource_shared_email", lambda to, **kw: True)
    u = uuid.uuid4().hex[:8]
    owner, use, view = f"owner_{u}", f"use_{u}", f"view_{u}"
    for s in (owner, use, view):
        db.upsert_user(s, email=f"{s}@example.test")
    org = org_store.create_org(f"a_{u}", created_by=owner)
    org_store.add_org_member(org, owner, "org_admin")
    app = db.apps.create(("user", owner), org, title=f"Desk {u}", manifest={}, html="",
                         thumbnail=None, by=owner)
    return {"owner": owner, "use": use, "view": view, "org": org, "app": app["id"], "u": u}


def _partager(m, rtype, rid, email, role):
    from oto_mcp.capabilities import resources as R
    from oto_mcp.capabilities._types import ResolvedCtx
    from oto_mcp.capabilities.resources_v2 import ResourceInputV2
    return R._resources(ResolvedCtx(sub=m["owner"], org_id=m["org"]),
                        ResourceInputV2(op="share", resource_type=rtype, resource_id=str(rid),
                                        audience="person", email=email, role=role))


def test_use_se_lit_read_et_clique_sans_modifier(monde):
    from oto_mcp import ownership
    from oto_mcp.capabilities import apps_appel as C
    m = monde
    out = _partager(m, "app", m["app"], f"{m['use']}@example.test", "contributor")
    assert (out["role"], out["permission"]) == ("contributor", "read")
    _partager(m, "app", m["app"], f"{m['view']}@example.test", "viewer")
    rid = str(m["app"])
    assert ownership.can_access(m["use"], "app", rid, "read")
    assert not ownership.can_access(m["use"], "app", rid, "write")
    assert C.peut_cliquer(m["use"], m["app"])
    assert ownership.can_access(m["view"], "app", rid, "read")
    assert not C.peut_cliquer(m["view"], m["app"])
    assert C.peut_cliquer(m["owner"], m["app"])
    roles = {g["principal_id"]: g["role"] for g in ownership.list_grants("app", rid)}
    assert roles == {m["use"]: "contributor", m["view"]: "viewer"}


def test_l_outil_herite_ne_prend_pas_contributor():
    """La surface héritée est sous cliquet : le rôle Use n'entre que par la v2."""
    from pydantic import ValidationError
    from oto_mcp.capabilities.resources import ResourceInput
    with pytest.raises(ValidationError):
        ResourceInput(op="share", resource_type="app", resource_id="1", role="contributor")


def test_le_demarrage_ne_ramene_pas_use_a_view(monde):
    """Le démarrage recalcule le rôle des partages depuis `permission` ; `contributor`
    (projection `read`) n'en est pas un à recalculer — sinon chaque boot le rendrait
    `viewer`."""
    from oto_mcp import db
    from oto_mcp.db import _init
    m = monde
    _partager(m, "app", m["app"], f"{m['use']}@example.test", "contributor")
    with db._connect() as conn:
        _init.apply_boot_schema(conn)
    roles = {g["principal_id"]: g["role"] for g in db.list_resource_grants("app", str(m["app"]))}
    assert roles[m["use"]] == "contributor"


def test_use_n_existe_que_sur_une_app(monde):
    from oto_mcp import db
    from oto_mcp.capabilities._types import AuthzDenied
    m = monde
    projet = db.create_project("user", m["owner"], f"p_{m['u']}", created_by=m["owner"])
    with pytest.raises(AuthzDenied) as e:
        _partager(m, "project", projet, f"{m['use']}@example.test", "contributor")
    assert e.value.code == "role_for_apps_only"


def test_le_check_de_la_base_prend_contributor_et_rien_d_autre(monde):
    import psycopg
    from oto_mcp import db
    m = monde
    db.grant_resource("app", str(m["app"]), "user", m["view"], role="contributor",
                      granted_by=m["owner"])
    with pytest.raises(psycopg.errors.CheckViolation):
        with db._connect() as conn:
            conn.execute("UPDATE resource_grants SET role = 'superuser' "
                         "WHERE resource_type = 'app' AND resource_id = %s", (str(m["app"]),))


def test_le_run_lance_d_un_clic_au_nom_de_l_auteur_reste_au_lecteur(monde):
    from oto_mcp import db
    m = monde
    run_id = f"run_{m['u']}"
    db.insert_run(run_id, sub=m["owner"], org_id=m["org"], label="app run")
    job = db.enqueue_job(m["org"], "start", {"trigger_id": 9}, run_id=run_id, sub=m["owner"])
    autre = db.enqueue_job(m["org"], "start", {"trigger_id": 9}, sub=m["owner"])
    db.apps.consent_issue(m["app"], m["use"], "start_agent", "fp", f"h_{m['u']}", 120)
    cid = db.apps.consent_burn(m["app"], m["use"], "start_agent", "fp", f"h_{m['u']}", None)
    db.apps.consent_job(cid, int(job["id"]), [11, 12])
    ids = [int(job["id"]), int(autre["id"])]
    assert db.apps.jobs_started_by(m["app"], m["use"], ids) == frozenset({int(job["id"])})
    assert db.apps.jobs_started_by(m["app"], m["view"], ids) == frozenset()
    assert db.apps.clicker_of_job(m["app"], int(job["id"]), m["owner"]) == {
        "sub": m["use"], "file_ids": frozenset({11, 12})}
    # Le travail d'un autre nom, d'une autre app, ou sans clic, ne se rattache à personne.
    assert db.apps.clicker_of_job(m["app"], int(job["id"]), m["view"]) is None
    assert db.apps.clicker_of_job(m["app"] + 1000, int(job["id"]), m["owner"]) is None
    assert db.apps.clicker_of_job(m["app"], int(autre["id"]), m["owner"]) is None


def test_pour_qui_se_lit_sur_le_jeton_jamais_sur_un_run_annonce(monde, monkeypatch):
    """Le travail vient du jeton de délégation (`verrou_org`) ; un run annoncé par
    l'appel (`_run_id=`) ne rattache personne."""
    from oto_mcp import db, session_org, verrou_org
    from oto_mcp.capabilities import apps_appel as C
    m = monde
    run_id = f"run2_{m['u']}"
    db.insert_run(run_id, sub=m["owner"], org_id=m["org"], label="app run")
    job = db.enqueue_job(m["org"], "start", {"trigger_id": 9}, run_id=run_id, sub=m["owner"])
    db.apps.consent_issue(m["app"], m["use"], "start_agent", "fp2", f"h2_{m['u']}", 120)
    cid = db.apps.consent_burn(m["app"], m["use"], "start_agent", "fp2", f"h2_{m['u']}", None)
    db.apps.consent_job(cid, int(job["id"]), [7])
    app = {"id": m["app"]}
    # Une session de l'auteur qui ANNONCE le run : rien.
    monkeypatch.setattr(session_org, "current_call_or_session_run", lambda: run_id)
    assert C.pour_qui(app, m["owner"]) == (m["owner"], None)
    # Le jeton du travail lui-même : le lecteur qui a cliqué, ses seuls fichiers remis.
    jeton = verrou_org.poser(verrou_org.Verrou(sub=m["owner"], org_id=m["org"],
                                               job_id=int(job["id"])))
    try:
        assert C.pour_qui(app, m["owner"]) == (m["use"], frozenset({7}))
        assert C.pour_qui(app, m["view"]) == (m["view"], None)
    finally:
        verrou_org.lever(jeton)
