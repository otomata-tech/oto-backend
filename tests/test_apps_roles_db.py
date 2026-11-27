"""Les rôles d'une app de bout en bout, contre un PostgreSQL réel : le rôle posé sur le
partage borne lectures, lot, gestes et `op=get` ; sans rôle posé, `default_role` ; qui
modifie l'app n'est borné par aucun ; seul qui la gouverne pose un rôle, et seulement
sur un partage existant ; un éditeur essaie un rôle par `as_role`."""
from __future__ import annotations

import uuid

import pytest

HOTE = "https://manage.oto.cx"
SCHEMA = {"fields": [{"key": "name", "type": "text"}, {"key": "status", "type": "text"},
                     {"key": "notes", "type": "text"}]}


@pytest.fixture
def monde(live, monkeypatch):
    from oto_mcp import db, org_store
    from oto_mcp.capabilities import resources as R
    from oto_mcp.datastore.core import make_store
    monkeypatch.setattr(R.email, "send_resource_shared_email", lambda to, **kw: True)
    u = uuid.uuid4().hex[:8]
    auteur, chef, membre = f"auteur_{u}", f"chef_{u}", f"membre_{u}"
    for s in (auteur, chef, membre):
        db.upsert_user(s, email=f"{s}@example.test")
    org = org_store.create_org(f"o_{u}", created_by=auteur)
    org_store.add_org_member(org, auteur, "org_admin")
    ns_id = db.create_datastore("user", auteur, f"fiches_{u}", context_org_id=org)
    st = make_store(auteur)
    st.set_schema(str(ns_id), SCHEMA)
    ligne = st.append_row(str(ns_id), {"name": "Acme", "status": "open", "notes": "secret"})
    for s in (chef, membre):
        db.grant_resource("datastore_namespace", str(ns_id), "user", s, role="editor",
                          granted_by=auteur)
    manifest = {
        "tables": {"fiches": {"table": str(ns_id), "fields": ["name", "status", "notes"],
                              "set": {"status": ["won", "lost"]}, "add": []}},
        "roles": {
            "admin": {"label": "Admin", "sections": ["pipeline", "notes"]},
            "user": {"label": "Utilisateur", "sections": ["pipeline"],
                     "tables": {"fiches": {"fields": ["name", "status"], "set": {}}}},
        },
        "default_role": "user",
    }
    from oto_mcp.capabilities.apps_manifeste import forme
    clean, problems = forme(manifest)
    assert problems == []
    clean["tables"]["fiches"]["table"] = str(ns_id)
    app = db.apps.create(("user", auteur), org, title=f"Desk {u}", manifest=clean,
                         html="", thumbnail=None, by=auteur)
    m = {"auteur": auteur, "chef": chef, "membre": membre, "org": org, "ns_id": ns_id,
         "app": int(app["id"]), "row": ligne["_id"], "rev": ligne["_revision"]}
    for s in (chef, membre):
        _partager(m, s)
    return m


def _ctx(m, sub):
    from oto_mcp.capabilities._types import ResolvedCtx
    return ResolvedCtx(sub=sub, org_id=m["org"], channel="rest")


def _partager(m, sub):
    from oto_mcp.capabilities import resources as R
    from oto_mcp.capabilities.resources_v2 import ResourceInputV2
    R._resources(_ctx(m, m["auteur"]),
                 ResourceInputV2(op="share", resource_type="app", resource_id=str(m["app"]),
                                 audience="person", email=f"{sub}@example.test",
                                 role="contributor"))


def _apps(m, sub, **kw):
    from oto_mcp.capabilities import apps as A
    return A._app(_ctx(m, sub), A.AppInput(app_id=m["app"], **kw))


def _lire(m, sub, **kw):
    from oto_mcp.capabilities import apps_appel as C
    return C._call(_ctx(m, sub), C.AppCallInput(app_id=m["app"], action="read_rows",
                                                table="fiches", **kw))


def _refus(fn, *a, **kw):
    from oto_mcp.capabilities._types import AuthzDenied
    with pytest.raises(AuthzDenied) as e:
        fn(*a, **kw)
    return e.value


def _comme(sub):
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


def test_sans_role_pose_le_role_par_defaut_borne_tout(monde):
    from oto_mcp.capabilities import apps_appel as C
    m = monde
    rows = _lire(m, m["membre"])["rows"]
    assert rows and "notes" not in rows[0] and rows[0]["name"] == "Acme"
    # Filtrer sur un champ que le rôle ne lit pas : refusé comme un champ non déclaré.
    _refus(_lire, m, m["membre"], options={"filter": {"notes": "secret"}})
    # Le lot passe par la même porte.
    out = C._call(_ctx(m, m["membre"]), C.AppCallInput(
        app_id=m["app"], action="batch",
        reads=[{"id": "a", "action": "read_rows", "table": "fiches"}]))
    assert "notes" not in out["results"][0]["rows"][0]
    # Écrire : `set: {}` — aucune valeur posable, même `won` que le manifeste ouvre.
    with _comme(m["membre"]):
        e = _refus(C._consent, _ctx(m, m["membre"]), C.AppCallInput(
            app_id=m["app"], action="write_cell", table="fiches", row_id=m["row"],
            field="status", value="won", expected_revision=m["rev"]))
    assert e.status == 403 and "may not change" in e.message
    vue = _apps(m, m["membre"], op="get")["app"]
    assert vue["access"] == {"role": "user", "role_label": "Utilisateur",
                             "sections": ["pipeline"]}
    assert "roles" not in vue["manifest"]
    assert vue["manifest"]["tables"]["fiches"]["fields"] == ["name", "status"]


def test_le_role_pose_sur_le_partage_ouvre_ce_qu_il_ouvre(monde):
    from oto_mcp.capabilities import apps_appel as C
    m = monde
    out = _apps(m, m["auteur"], op="set_role", principal_type="user",
                principal_id=m["chef"], role="admin")
    assert {g["principal_id"]: g["app_role"] for g in out["grants"]}[m["chef"]] == "admin"
    assert _lire(m, m["chef"])["rows"][0]["notes"] == "secret"
    with _comme(m["chef"]):
        accord = C._consent(_ctx(m, m["chef"]), C.AppCallInput(
            app_id=m["app"], action="write_cell", table="fiches", row_id=m["row"],
            field="status", value="won", expected_revision=m["rev"]))
    assert accord["consent"]
    assert _apps(m, m["chef"], op="get")["app"]["access"]["role"] == "admin"
    # Le membre reste au rôle par défaut.
    assert "notes" not in _lire(m, m["membre"])["rows"][0]
    # Retirer le rôle : retour au rôle par défaut.
    _apps(m, m["auteur"], op="set_role", principal_type="user", principal_id=m["chef"])
    assert "notes" not in _lire(m, m["chef"])["rows"][0]


def test_qui_modifie_l_app_n_est_borne_par_aucun_role(monde):
    m = monde
    assert _lire(m, m["auteur"])["rows"][0]["notes"] == "secret"
    vue = _apps(m, m["auteur"], op="get")["app"]
    assert "roles" in vue["manifest"] and vue["access"]["role"] is None


def test_as_role_pour_les_editeurs_seuls(monde):
    m = monde
    assert "notes" not in _lire(m, m["auteur"], as_role="user")["rows"][0]
    assert _refus(_lire, m, m["membre"], as_role="admin").code == "as_role_editors_only"
    assert _refus(_lire, m, m["auteur"], as_role="nope").code == "unknown_role"


def test_seul_qui_gouverne_pose_un_role_sur_un_partage_existant(monde):
    from oto_mcp import db
    m = monde
    e = _refus(_apps, m, m["membre"], op="set_role", principal_type="user",
               principal_id=m["membre"], role="admin")
    assert e.code == "forbidden"
    assert _refus(_apps, m, m["auteur"], op="set_role", principal_type="user",
                  principal_id=m["chef"], role="nope").code == "unknown_role"
    inconnu = f"x_{uuid.uuid4().hex[:6]}"
    db.upsert_user(inconnu, email=f"{inconnu}@example.test")
    assert _refus(_apps, m, m["auteur"], op="set_role", principal_type="user",
                  principal_id=inconnu, role="admin").code == "not_shared"


def test_un_role_retire_du_manifeste_retombe_au_defaut(monde):
    from oto_mcp import db
    m = monde
    db.apps.set_app_role(str(m["app"]), "user", m["chef"], "ancien")
    assert "notes" not in _lire(m, m["chef"])["rows"][0]


def test_le_role_part_avec_le_partage(monde):
    from oto_mcp import ownership
    m = monde
    _apps(m, m["auteur"], op="set_role", principal_type="user", principal_id=m["chef"],
          role="admin")
    ownership.revoke("app", str(m["app"]), "user", m["chef"])
    _partager(m, m["chef"])
    assert "notes" not in _lire(m, m["chef"])["rows"][0]


def test_jsonb_ne_garde_pas_l_ordre_mais_role_order_si(monde):
    """`admin` est écrit avant `user` ; JSONB rend `user` d'abord (clés plus courtes en
    tête). Deux partages d'ÉQUIPE de rôles différents : le premier ÉCRIT gagne."""
    from oto_mcp import db, group_store, org_store
    m = monde
    lu = db.apps.get_for_call(m["app"])["manifest"]
    assert lu["role_order"] == ["admin", "user"]
    autre = f"autre_{uuid.uuid4().hex[:6]}"
    db.upsert_user(autre, email=f"{autre}@example.test")
    org_store.add_org_member(m["org"], autre, "org_member")
    db.grant_resource("datastore_namespace", str(m["ns_id"]), "user", autre, role="editor",
                      granted_by=m["auteur"])
    g1 = group_store.create_group(m["org"], f"g1_{autre}", created_by=m["auteur"])
    g2 = group_store.create_group(m["org"], f"g2_{autre}", created_by=m["auteur"])
    for g, role in ((g1, "user"), (g2, "admin")):
        gid = g["id"] if isinstance(g, dict) else g
        group_store.add_group_member(int(gid), autre)
        db.grant_resource("app", str(m["app"]), "group", str(gid), role="contributor",
                          granted_by=m["auteur"])
        assert db.apps.set_app_role(str(m["app"]), "group", str(gid), role)
    assert _lire(m, autre)["rows"][0]["notes"] == "secret"     # admin, écrit en premier


def test_changer_view_use_garde_le_role_d_app(monde):
    from oto_mcp import db
    m = monde
    _apps(m, m["auteur"], op="set_role", principal_type="user", principal_id=m["chef"],
          role="admin")
    db.grant_resource("app", str(m["app"]), "user", m["chef"], role="viewer",
                      granted_by=m["auteur"])
    db.grant_resource("app", str(m["app"]), "user", m["chef"], role="contributor",
                      granted_by=m["auteur"])
    assert _lire(m, m["chef"])["rows"][0]["notes"] == "secret"
