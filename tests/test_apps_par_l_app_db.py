"""L'accès à un tableau À TRAVERS une app seulement (`datastore/par_l_app.py`), contre un
PostgreSQL réel : sans aucun partage du tableau, le lecteur le lit — et l'écrit par les
gestes de l'app — dans l'appel de CETTE app, borné par son rôle ; hors de l'app (outils
de tableau, une autre app), rien. L'accès part avec le partage de l'app ; seul qui peut
partager le tableau l'ouvre ainsi."""
from __future__ import annotations

import uuid

import pytest

HOTE = "https://manage.oto.cx"


def _ctx(m, sub):
    """L'org de l'APPEL : la sienne pour le partenaire (un invité de l'extérieur
    consulte depuis son espace), celle de l'app pour les autres."""
    from oto_mcp.capabilities._types import ResolvedCtx
    org = m["org_p"] if sub == m["partenaire"] else m["org"]
    return ResolvedCtx(sub=sub, org_id=org, channel="rest")


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



SCHEMA = {"fields": [{"key": "name", "type": "text"}, {"key": "status", "type": "text"},
                     {"key": "notes", "type": "text"}]}


@pytest.fixture
def monde(live, monkeypatch):
    from oto_mcp import db, org_store
    from oto_mcp.capabilities import resources as R
    from oto_mcp.capabilities.apps_manifeste import forme
    from oto_mcp.datastore.core import make_store
    monkeypatch.setattr(R.email, "send_resource_shared_email", lambda to, **kw: True)
    u = uuid.uuid4().hex[:8]
    auteur, partenaire, gerant_app = f"auteur_{u}", f"partenaire_{u}", f"gerant_{u}"
    for s in (auteur, partenaire, gerant_app):
        db.upsert_user(s, email=f"{s}@example.test")
    org = org_store.create_org(f"o_{u}", created_by=auteur)
    org_store.add_org_member(org, auteur, "org_admin")
    # Le partenaire vit dans SA propre org : un invité de l'extérieur.
    org_p = org_store.create_org(f"p_{u}", created_by=partenaire)
    org_store.add_org_member(org_p, partenaire, "org_admin")
    ns_id = db.create_datastore("user", auteur, f"fiches_{u}", context_org_id=org)
    st = make_store(auteur)
    st.set_schema(str(ns_id), SCHEMA)
    ligne = st.append_row(str(ns_id), {"name": "Acme", "status": "open", "notes": "secret"})
    clean, problems = forme({
        "tables": {"fiches": {"table": str(ns_id), "fields": ["name", "status", "notes"],
                              "set": {"status": ["won", "lost"]}, "add": ["name"]}},
        "roles": {"partner": {"tables": {"fiches": {"fields": ["name", "status"]}}},
                  "reader": {"tables": {"fiches": {"fields": ["name"], "set": {},
                                                   "add": []}}}},
        "default_role": "partner"})
    assert problems == []
    clean["tables"]["fiches"]["table"] = str(ns_id)
    app = db.apps.create(("user", auteur), org, title=f"Desk {u}", manifest=clean,
                         html="", thumbnail=None, by=auteur)
    m = {"auteur": auteur, "partenaire": partenaire, "gerant": gerant_app, "org": org,
         "org_p": org_p, "ns_id": ns_id, "app": int(app["id"]), "row": ligne["_id"],
         "rev": ligne["_revision"]}
    return m


@pytest.fixture(autouse=True)
def _partages(monde):
    from oto_mcp.capabilities import resources as R
    from oto_mcp.capabilities.resources_v2 import ResourceInputV2
    m = monde
    for s, role in ((m["partenaire"], "contributor"), (m["gerant"], "manager")):
        R._resources(_ctx(m, m["auteur"]), ResourceInputV2(
            op="share", resource_type="app", resource_id=str(m["app"]), audience="person",
            email=f"{s}@example.test", role=role))


def _ouvrir(m, by=None, **kw):
    return _apps(m, by or m["auteur"], op="grant_through_app", principal_type="user",
                 principal_id=m["partenaire"], **kw)


def _outil_de_tableau(m, sub):
    from oto_mcp.capabilities.datastore import rows as R
    return R._list_rows(_ctx(m, sub), R.ListRowsInput(datastore=str(m["ns_id"])))


def test_sans_acces_par_l_app_le_partenaire_ne_lit_rien(monde):
    m = monde
    _refus(_lire, m, m["partenaire"])


def test_par_l_app_il_lit_son_role_et_rien_hors_de_l_app(monde):
    m = monde
    out = _ouvrir(m)
    assert out["skipped"] == []
    acces = {g["principal_id"]: g["through_app"] for g in out["grants"]}[m["partenaire"]]
    assert acces == [{"table": "fiches", "permission": "write"}]
    rows = _lire(m, m["partenaire"])["rows"]
    assert rows[0]["name"] == "Acme" and "notes" not in rows[0]
    # Hors de l'app : l'outil de tableau ne le trouve pas.
    _refus(_outil_de_tableau, m, m["partenaire"])
    from oto_mcp.datastore.core import DatastoreNotFound, make_store
    with pytest.raises(DatastoreNotFound):
        make_store(m["partenaire"]).get_schema(str(m["ns_id"]))


def test_par_l_app_il_ecrit_par_les_gestes_de_l_app(monde):
    from oto_mcp.capabilities import apps_appel as C
    m = monde
    _ouvrir(m)
    corps = dict(action="write_cell", table="fiches", row_id=m["row"], field="status",
                 value="won", expected_revision=m["rev"])
    with _comme(m["partenaire"]):
        accord = C._consent(_ctx(m, m["partenaire"]), C.AppCallInput(app_id=m["app"], **corps))
        out = C._call(_ctx(m, m["partenaire"]), C.AppCallInput(
            app_id=m["app"], consent=accord["consent"], **corps))
    assert out["row"]["status"] == "won"


def test_l_acces_part_avec_le_partage_de_l_app(monde):
    from oto_mcp import ownership
    m = monde
    _ouvrir(m)
    ownership.revoke("app", str(m["app"]), "user", m["partenaire"])
    _refus(_lire, m, m["partenaire"])


def test_retirer_l_acces_par_l_app(monde):
    m = monde
    _ouvrir(m)
    _apps(m, m["auteur"], op="revoke_through_app", principal_type="user",
          principal_id=m["partenaire"])
    _refus(_lire, m, m["partenaire"])


def test_qui_ne_peut_partager_le_tableau_ne_l_ouvre_pas(monde):
    """Le gérant de l'APP n'a aucun droit de partage sur le tableau : sauté, nommé."""
    m = monde
    out = _ouvrir(m, by=m["gerant"])
    assert out["skipped"] == [{"table": "fiches", "reason": "you can't share this table"}]
    _refus(_lire, m, m["partenaire"])


def test_seulement_a_qui_l_app_est_partagee(monde):
    from oto_mcp import db
    m = monde
    x = f"x_{uuid.uuid4().hex[:6]}"
    db.upsert_user(x, email=f"{x}@example.test")
    e = _refus(_apps, m, m["auteur"], op="grant_through_app", principal_type="user",
               principal_id=x)
    assert e.code == "not_shared"


def test_la_portee_ne_sort_pas_de_l_appel(monde):
    from oto_mcp.datastore import par_l_app
    m = monde
    _ouvrir(m)
    _lire(m, m["partenaire"])
    assert par_l_app.droit(m["partenaire"], str(m["ns_id"])) is None


def _geste(m, sub, **corps):
    from oto_mcp.capabilities import apps_appel as C
    with _comme(sub):
        accord = C._consent(_ctx(m, sub), C.AppCallInput(app_id=m["app"], **corps))
        return C._call(_ctx(m, sub), C.AppCallInput(app_id=m["app"],
                                                     consent=accord["consent"], **corps))


def test_agregat_lot_et_ajout_passent_par_la_meme_portee(monde):
    from oto_mcp.capabilities import apps_appel as C
    m = monde
    _ouvrir(m)
    groupes = C._call(_ctx(m, m["partenaire"]), C.AppCallInput(
        app_id=m["app"], action="read_aggregate", table="fiches",
        options={"group_by": "status"}))["groups"]
    assert groupes
    lot = C._call(_ctx(m, m["partenaire"]), C.AppCallInput(
        app_id=m["app"], action="batch",
        reads=[{"id": "a", "action": "read_rows", "table": "fiches"}]))["results"][0]
    assert lot["ok"] and "notes" not in lot["rows"][0]
    out = _geste(m, m["partenaire"], action="add_row", table="fiches", values={"name": "Beta"})
    assert out["through_app"] is True


def test_l_ecriture_servie_par_l_app_le_dit(monde):
    m = monde
    _ouvrir(m)
    out = _geste(m, m["partenaire"], action="write_cell", table="fiches", row_id=m["row"],
                 field="status", value="lost", expected_revision=m["rev"])
    assert out["through_app"] is True


def test_un_role_qui_n_ecrit_plus_ramene_l_acces_a_la_lecture(monde):
    m = monde
    _ouvrir(m)
    _apps(m, m["auteur"], op="set_role", principal_type="user", principal_id=m["partenaire"],
          role="reader")
    rows = _lire(m, m["partenaire"])["rows"]
    assert set(rows[0]) & {"name", "status", "notes"} == {"name"}
    e = _refus(_geste, m, m["partenaire"], action="write_cell", table="fiches",
               row_id=m["row"], field="status", value="won", expected_revision=m["rev"])
    assert e.status in (400, 403)


def test_un_editeur_qui_ne_peut_partager_le_tableau_n_elargit_rien(monde):
    """Il réécrit l'app (le rôle du partenaire lit maintenant `notes`) : il en devient
    l'auteur, et l'accès par l'app s'éteint au lieu de s'élargir."""
    from oto_mcp import db
    from oto_mcp.capabilities import resources as R
    from oto_mcp.capabilities.resources_v2 import ResourceInputV2
    m = monde
    _ouvrir(m)
    ed = f"ed_{uuid.uuid4().hex[:6]}"
    db.upsert_user(ed, email=f"{ed}@example.test")
    from oto_mcp import org_store
    org_store.add_org_member(m["org"], ed, "org_member")
    db.grant_resource("datastore_namespace", str(m["ns_id"]), "user", ed, role="viewer",
                      granted_by=m["auteur"])
    R._resources(_ctx(m, m["auteur"]), ResourceInputV2(
        op="share", resource_type="app", resource_id=str(m["app"]), audience="person",
        email=f"{ed}@example.test", role="editor"))
    vu = _apps(m, ed, op="get")["app"]
    manifeste = vu["manifest"]
    manifeste["roles"]["partner"]["tables"]["fiches"]["fields"] = ["name", "status", "notes"]
    _apps(m, ed, op="update", manifest=manifeste, expected_rev=vu["rev"])
    _refus(_lire, m, m["partenaire"])


def test_un_gerant_de_l_app_seul_ne_change_pas_le_role_d_un_lecteur_par_l_app(monde):
    m = monde
    _ouvrir(m)
    e = _refus(_apps, m, m["gerant"], op="set_role", principal_type="user",
               principal_id=m["partenaire"], role="reader")
    assert e.code == "forbidden"


def test_la_portee_ne_sert_que_son_lecteur(monde):
    from oto_mcp.datastore import par_l_app
    m = monde
    p = par_l_app.Portee(m["partenaire"], lambda ns: "write")
    with par_l_app.portee(p):
        assert par_l_app.droit(m["partenaire"], "1") == "write"
        assert par_l_app.droit(m["auteur"], "1") is None
        assert par_l_app.droit(None, "1") is None


# ── Le run d'un agent lancé depuis l'app ─────────────────────────────────────────

def _run_depuis_l_app(m, sub, job_id=None):
    """Un travail que `sub` a lancé depuis l'app : son consentement `start_agent`
    brûlé sur ce travail — ce que `_geste` écrit au lancement."""
    from oto_mcp import db
    job_id = job_id or int(uuid.uuid4().int % 10**9)
    cid = db.apps.consent_issue(m["app"], sub, "start_agent", "f", uuid.uuid4().hex, 120)
    with db._connect() as conn:
        conn.execute("UPDATE app_consents SET used_at = now(), job_id = %s "
                     "WHERE app_id = %s AND sub = %s AND action = 'start_agent' "
                     "AND job_id IS NULL", (job_id, m["app"], sub))
    return job_id


def _dans_le_run(sub, job_id):
    from oto_mcp import verrou_org
    from contextlib import contextmanager

    @contextmanager
    def ctx():
        j = verrou_org.poser(verrou_org.Verrou(sub=sub, org_id=None, job_id=job_id))
        try:
            yield
        finally:
            verrou_org._COURANT.reset(j)
    return ctx()


def _manifeste_entier(m):
    """Le rôle du partenaire ouvre le tableau ENTIER (sans liste de champs)."""
    from oto_mcp import db
    with db._connect() as conn:
        conn.execute("UPDATE apps SET manifest = jsonb_set(manifest, "
                     "'{roles,partner,tables,fiches}', '{}'::jsonb) WHERE id = %s",
                     (m["app"],))
        conn.execute("UPDATE apps SET manifest = manifest #- '{tables,fiches,fields}' "
                     "WHERE id = %s", (m["app"],))


def test_l_agent_lance_depuis_l_app_lit_ce_que_son_lanceur_y_lit(monde):
    from oto_mcp.datastore.core import DatastoreNotFound, DatastoreReadOnly, make_store
    m = monde
    _ouvrir(m)
    _manifeste_entier(m)
    job = _run_depuis_l_app(m, m["partenaire"])
    with _dans_le_run(m["partenaire"], job):
        assert make_store(m["partenaire"]).get_schema(str(m["ns_id"]))
        with pytest.raises((DatastoreReadOnly, DatastoreNotFound)):
            make_store(m["partenaire"]).append_row(str(m["ns_id"]), {"name": "x"})
    # Un AUTRE travail du même compte, pas lancé depuis l'app : rien.
    with _dans_le_run(m["partenaire"], job + 1):
        with pytest.raises(DatastoreNotFound):
            make_store(m["partenaire"]).get_schema(str(m["ns_id"]))


def test_l_agent_n_herite_pas_d_un_tableau_aux_champs_bornes(monde):
    from oto_mcp.datastore.core import DatastoreNotFound, make_store
    m = monde
    _ouvrir(m)                      # le rôle partner ne lit que name, status
    job = _run_depuis_l_app(m, m["partenaire"])
    with _dans_le_run(m["partenaire"], job):
        with pytest.raises(DatastoreNotFound):
            make_store(m["partenaire"]).get_schema(str(m["ns_id"]))


# ── Le gérant du tableau voit et retire ──────────────────────────────────────────

def test_le_gerant_du_tableau_voit_qui_le_lit_par_une_app_et_le_retire(monde):
    from oto_mcp import db, org_store
    from oto_mcp.capabilities import resources as R
    from oto_mcp.capabilities.resources_v2 import ResourceInputV2
    m = monde
    _ouvrir(m)
    vu = R._resources(_ctx(m, m["auteur"]), ResourceInputV2(
        op="get", resource_type="datastore_namespace", resource_id=str(m["ns_id"])))
    assert [(a["app_id"], a["principal_id"], a["permission"]) for a in vu["through_app"]] \
        == [(m["app"], m["partenaire"], "write")]
    # Un gérant du TABLEAU, sans aucun droit sur l'app, retire l'accès.
    g = f"gt_{uuid.uuid4().hex[:6]}"
    db.upsert_user(g, email=f"{g}@example.test")
    org_store.add_org_member(m["org"], g, "org_member")
    db.grant_resource("datastore_namespace", str(m["ns_id"]), "user", g, role="manager",
                      granted_by=m["auteur"])
    out = _apps(m, g, op="revoke_through_app", principal_type="user",
                principal_id=m["partenaire"], tables=[str(m["ns_id"])])
    assert out["ok"] is True
    _refus(_lire, m, m["partenaire"])


def test_sans_gouverner_le_tableau_on_ne_retire_rien(monde):
    from oto_mcp import db
    m = monde
    _ouvrir(m)
    x = f"x_{uuid.uuid4().hex[:6]}"
    db.upsert_user(x, email=f"{x}@example.test")
    e = _refus(_apps, m, x, op="revoke_through_app", principal_type="user",
               principal_id=m["partenaire"], tables=[str(m["ns_id"])])
    assert e.code == "forbidden"
    assert _lire(m, m["partenaire"])["rows"]


# ── Par les VRAIES routes REST (authentification, garde d'org, contexte de requête) ──

class _Verifier:
    def __init__(self, sub):
        self.sub = sub

    async def verify_token(self, token):
        import types
        return types.SimpleNamespace(claims={"sub": self.sub})


def _client(sub, org):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient
    from oto_mcp.api import routes as api_routes
    c = TestClient(Starlette(routes=list(api_routes.make_routes(_Verifier(sub)))))
    c.headers.update({"Authorization": "Bearer jwt-de-test", "Origin": HOTE,
                      "X-Oto-Org": str(org)})
    return c


def test_par_les_routes_rest_l_app_sert_et_les_tableaux_refusent(monde):
    from oto_mcp import org_store
    m = monde
    _ouvrir(m)
    org_store.set_active_org(m["partenaire"], m["org_p"])
    c = _client(m["partenaire"], m["org_p"])
    r = c.post(f"/api/me/apps/{m['app']}/call", json={"action": "read_rows", "table": "fiches"})
    assert r.status_code == 200, r.text
    ligne = r.json()["rows"][0]
    assert ligne["name"] == "Acme" and "notes" not in ligne
    # Les routes de tableau, elles, ne le connaissent pas.
    for chemin in (f"/api/datastores/{m['ns_id']}", f"/api/datastores/{m['ns_id']}/rows"):
        r = c.get(chemin)
        assert r.status_code in (403, 404), (chemin, r.status_code, r.text)
    # Et un geste par la route : consentement puis appel.
    corps = {"action": "write_cell", "table": "fiches", "row_id": m["row"], "field": "status",
             "value": "won", "expected_revision": m["rev"]}
    r = c.post(f"/api/me/apps/{m['app']}/consent", json=corps)
    assert r.status_code == 200, r.text
    r = c.post(f"/api/me/apps/{m['app']}/call", json={**corps, "consent": r.json()["consent"]})
    assert r.status_code == 200, r.text
    assert r.json().get("through_app") is True


# ── Un accès lié au partage qui l'a vu ouvrir : il ne revit pas sous un autre ────────

def _partager(m, s, role="contributor", **kw):
    from oto_mcp.capabilities import resources as R
    from oto_mcp.capabilities.resources_v2 import ResourceInputV2
    R._resources(_ctx(m, m["auteur"]), ResourceInputV2(
        op="share", resource_type="app", resource_id=str(m["app"]), audience="person",
        email=f"{s}@example.test", role=role, **kw))


def _echoir(m, ptype, pid):
    from oto_mcp import db
    with db._connect() as conn:
        conn.execute("UPDATE resource_grants SET expires_at = NOW() - interval '1 day' "
                     "WHERE resource_type = 'app' AND resource_id = %s "
                     "AND principal_type = %s AND principal_id = %s",
                     (str(m["app"]), ptype, str(pid)))


def _lignes(m):
    from oto_mcp import db
    with db._connect() as conn:
        return conn.execute("SELECT principal_type, principal_id FROM app_table_access "
                            "WHERE app_id = %s", (m["app"],)).fetchall()


def _par_l_app_liste(m):
    """Ce que listent `op=roles` (par partage) et le tableau (`through_app`)."""
    from oto_mcp.capabilities import resources as R
    from oto_mcp.capabilities.resources_v2 import ResourceInputV2
    roles = _apps(m, m["auteur"], op="roles")
    par_partage = {g["principal_id"]: g["through_app"] for g in roles["grants"]}
    tableau = R._resources(_ctx(m, m["auteur"]), ResourceInputV2(
        op="get", resource_type="datastore_namespace", resource_id=str(m["ns_id"])))
    return par_partage, tableau.get("through_app") or []


def test_retrait_puis_repartage_ne_rouvre_rien(monde):
    from oto_mcp import ownership
    m = monde
    _ouvrir(m)
    ownership.revoke("app", str(m["app"]), "user", m["partenaire"])
    assert _lignes(m) == []                      # parti avec le partage
    _partager(m, m["partenaire"])
    _refus(_lire, m, m["partenaire"])


def test_echeance_puis_repartage_ne_rouvre_rien(monde):
    """Aucun événement à l'échéance : la ligne reste — c'est l'identité du partage
    repris qui l'éteint."""
    m = monde
    _ouvrir(m)
    _apps(m, m["auteur"], op="set_role", principal_type="user", principal_id=m["partenaire"],
          role="reader")
    _echoir(m, "user", m["partenaire"])
    _refus(_lire, m, m["partenaire"])
    _partager(m, m["partenaire"])
    _refus(_lire, m, m["partenaire"])
    par_partage, tableau = _par_l_app_liste(m)
    assert par_partage[m["partenaire"]] == [] and tableau == []
    # Partage neuf : le rôle d'app posé sur l'ancien n'a pas suivi non plus.
    roles = _apps(m, m["auteur"], op="roles")
    assert {g["principal_id"]: g["app_role"] for g in roles["grants"]}[m["partenaire"]] is None
    # Rouvert sous le partage neuf : il vaut de nouveau.
    _ouvrir(m)
    assert _lire(m, m["partenaire"])["rows"]


def test_changer_le_role_du_partage_vivant_garde_l_acces(monde):
    """Voir → Utiliser, une échéance posée : le MÊME partage, l'accès et le rôle d'app
    restent."""
    m = monde
    _ouvrir(m)
    _apps(m, m["auteur"], op="set_role", principal_type="user", principal_id=m["partenaire"],
          role="reader")
    _partager(m, m["partenaire"], role="viewer", ttl_days=30)
    _partager(m, m["partenaire"], role="contributor")
    rows = _lire(m, m["partenaire"])["rows"]
    assert set(rows[0]) & {"name", "status", "notes"} == {"name"}     # rôle `reader` gardé
    par_partage, tableau = _par_l_app_liste(m)
    assert par_partage[m["partenaire"]] == [{"table": "fiches", "permission": "write"}]
    assert [a["principal_id"] for a in tableau] == [m["partenaire"]]


def test_un_role_ne_se_pose_pas_sur_un_partage_echu(monde):
    m = monde
    _echoir(m, "user", m["partenaire"])
    e = _refus(_apps, m, m["auteur"], op="set_role", principal_type="user",
               principal_id=m["partenaire"], role="reader")
    assert e.code == "not_shared"


def test_partage_d_org_par_defaut_retire_puis_repose(monde):
    """Le partage d'org par défaut (marqué) est un partage comme un autre : son retrait
    emporte ce qui avait été ouvert à l'org, son retour ne le rend pas."""
    from oto_mcp import db, ownership
    m = monde
    ownership.revoke("app", str(m["app"]), "user", m["partenaire"])
    ownership.grant("app", str(m["app"]), "org", str(m["org_p"]), role="contributor",
                    granted_by=db.MARQUE_PARTAGE_PAR_DEFAUT)
    _apps(m, m["auteur"], op="grant_through_app", principal_type="org",
          principal_id=str(m["org_p"]))
    assert _lire(m, m["partenaire"])["rows"]
    ownership.revoke("app", str(m["app"]), "org", str(m["org_p"]))
    assert _lignes(m) == []
    ownership.grant("app", str(m["app"]), "org", str(m["org_p"]), role="contributor",
                    granted_by=db.MARQUE_PARTAGE_PAR_DEFAUT)
    _refus(_lire, m, m["partenaire"])


def test_transfert_aller_retour_ne_rouvre_rien(monde):
    """Le partenaire devient propriétaire (son partage tombe, l'accès avec), puis l'app
    revient à l'auteur : le partage `write` que le transfert lui rend est neuf."""
    from oto_mcp import ownership
    m = monde
    _ouvrir(m)
    ownership.transfer("app", str(m["app"]), "user", m["partenaire"])
    assert _lignes(m) == []
    ownership.transfer("app", str(m["app"]), "user", m["auteur"])
    assert ownership.can_access(m["partenaire"], "app", str(m["app"]), "read")
    _refus(_lire, m, m["partenaire"])


def test_transfert_garde_l_acces_des_autres(monde):
    """L'app passe à son org : les AUTRES partages restent, leur accès aussi (toujours
    borné par l'auteur du manifeste, qui peut partager le tableau)."""
    from oto_mcp import ownership
    m = monde
    _ouvrir(m)
    ownership.transfer("app", str(m["app"]), "org", str(m["org"]))
    assert _lire(m, m["partenaire"])["rows"]


def test_equipe_supprimee_emporte_son_acces_par_l_app(monde):
    from oto_mcp import db, group_store, ownership
    m = monde
    ownership.revoke("app", str(m["app"]), "user", m["partenaire"])
    g = group_store.create_group(m["org_p"], f"eq_{uuid.uuid4().hex[:6]}",
                                 created_by=m["partenaire"])
    group_store.add_group_member(g, m["partenaire"])
    db.grant_resource("app", str(m["app"]), "group", str(g), role="contributor",
                      granted_by=m["auteur"])
    _apps(m, m["auteur"], op="grant_through_app", principal_type="group",
          principal_id=str(g))
    assert _lire(m, m["partenaire"])["rows"]
    group_store.delete_group(g)
    assert _lignes(m) == []
    _refus(_lire, m, m["partenaire"])


def test_une_ligne_sans_son_partage_ne_vaut_rien(monde):
    """Le cœur, sans passer par aucun chemin de retrait : une ligne liée à une AUTRE
    identité que celle du partage vivant — ce que laisserait un chemin oublié."""
    from oto_mcp import db
    m = monde
    _ouvrir(m)
    with db._connect() as conn:
        conn.execute("UPDATE app_table_access SET share_id = share_id + 1000000 "
                     "WHERE app_id = %s", (m["app"],))
    _refus(_lire, m, m["partenaire"])
    assert _par_l_app_liste(m)[1] == []


def test_un_partage_d_avant_l_identite_est_numerote_a_l_ouverture(monde):
    from oto_mcp import db
    m = monde
    with db._connect() as conn:
        conn.execute("UPDATE resource_grants SET share_id = NULL WHERE resource_type = 'app' "
                     "AND resource_id = %s", (str(m["app"]),))
    _ouvrir(m)
    assert _lire(m, m["partenaire"])["rows"]


def test_la_fusion_de_comptes_garde_partage_et_acces_ensemble(monde):
    from oto_mcp import db
    from oto_mcp.capabilities import apps_appel as C
    from oto_mcp.capabilities._types import ResolvedCtx
    m = monde
    _ouvrir(m)
    neuf = f"neuf_{uuid.uuid4().hex[:6]}"
    db.upsert_user(neuf, email=f"{neuf}@example.test")
    assert db.migrate_sub(m["partenaire"], neuf, operator_source="test") is True
    out = C._call(ResolvedCtx(sub=neuf, org_id=m["org_p"], channel="rest"),
                  C.AppCallInput(app_id=m["app"], action="read_rows", table="fiches"))
    assert out["rows"][0]["name"] == "Acme"
