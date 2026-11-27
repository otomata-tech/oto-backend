"""Le journal des lectures d'app (`app_reads`, `docs/apps.md` § Journal des lectures),
contre un PostgreSQL réel : chaque lecture SERVIE — seule ou d'un lot — s'ajoute aux
compteurs du jour, écrite APRÈS l'appel, hors de son emprunt ; un échec du journal ne
refuse jamais la lecture ; seuls qui gouvernent l'app ou le tableau le lisent ; le run
d'un agent lancé depuis l'app est noté ; la purge retire au-delà de 180 jours."""
from __future__ import annotations

import logging
import uuid

import pytest

SCHEMA = {"fields": [{"key": "name", "type": "text"}, {"key": "status", "type": "text"},
                     {"key": "notes", "type": "text"}]}


def _ctx(m, sub):
    from oto_mcp.capabilities._types import ResolvedCtx
    org = m["org_p"] if sub == m["partenaire"] else m["org"]
    return ResolvedCtx(sub=sub, org_id=org, channel="rest")


def _route(m, sub, **kw):
    """L'appel tel que la route le sert : le handler déclaré de `me.app.call`."""
    from oto_mcp.capabilities import apps_appel as C
    from oto_mcp.capabilities.registry import CAPABILITIES
    handler = next(c.handler for c in CAPABILITIES if c.key == "me.app.call")
    return handler(_ctx(m, sub), C.AppCallInput(app_id=m["app"], **kw))


def _lire(m, sub, **kw):
    return _route(m, sub, action="read_rows", table="fiches", **kw)


def _apps(m, sub, **kw):
    from oto_mcp.capabilities import apps as A
    kw.setdefault("app_id", m["app"])
    return A._app(_ctx(m, sub), A.AppInput(**kw))


def _refus(fn, *a, **kw):
    from oto_mcp.capabilities._types import AuthzDenied
    with pytest.raises(AuthzDenied) as e:
        fn(*a, **kw)
    return e.value


def _journal(m):
    from oto_mcp import db
    with db._connect() as conn:
        return {(r["sub"], r["access"]): r for r in conn.execute(
            "SELECT * FROM app_reads WHERE app_id = %s", (m["app"],)).fetchall()}


def _nouveau(prefixe):
    from oto_mcp import db
    s = f"{prefixe}_{uuid.uuid4().hex[:6]}"
    db.upsert_user(s, email=f"{s}@example.test")
    return s


@pytest.fixture
def monde(live, monkeypatch):
    """Un auteur (org_admin) et son tableau à deux lignes, une app qui le déclare ; un
    partenaire d'une AUTRE org qui tient l'app en Use et le tableau par l'app seule."""
    from oto_mcp import db, org_store
    from oto_mcp.capabilities import resources as R
    from oto_mcp.capabilities.apps_manifeste import forme
    from oto_mcp.capabilities.resources_v2 import ResourceInputV2
    from oto_mcp.datastore.core import make_store
    monkeypatch.setattr(R.email, "send_resource_shared_email", lambda to, **kw: True)
    u = uuid.uuid4().hex[:8]
    auteur, partenaire = f"auteur_{u}", f"partenaire_{u}"
    for s in (auteur, partenaire):
        db.upsert_user(s, email=f"{s}@example.test")
    org = org_store.create_org(f"o_{u}", created_by=auteur)
    org_store.add_org_member(org, auteur, "org_admin")
    org_p = org_store.create_org(f"p_{u}", created_by=partenaire)
    org_store.add_org_member(org_p, partenaire, "org_admin")
    ns_id = db.create_datastore("user", auteur, f"fiches_{u}", context_org_id=org)
    st = make_store(auteur)
    st.set_schema(str(ns_id), SCHEMA)
    for n in ("Acme", "Beta"):
        st.append_row(str(ns_id), {"name": n, "status": "open", "notes": "secret"})
    clean, problems = forme({
        "tables": {"fiches": {"table": str(ns_id), "fields": ["name", "status", "notes"]}},
        "roles": {"partner": {"tables": {"fiches": {"fields": ["name", "status"]}}}},
        "default_role": "partner"})
    assert problems == []
    clean["tables"]["fiches"]["table"] = str(ns_id)
    app = db.apps.create(("user", auteur), org, title=f"Desk {u}", manifest=clean,
                         html="", thumbnail=None, by=auteur)
    m = {"auteur": auteur, "partenaire": partenaire, "org": org, "org_p": org_p,
         "ns_id": ns_id, "app": int(app["id"])}
    R._resources(_ctx(m, auteur), ResourceInputV2(
        op="share", resource_type="app", resource_id=str(m["app"]), audience="person",
        email=f"{partenaire}@example.test", role="contributor"))
    _apps(m, auteur, op="grant_through_app", principal_type="user", principal_id=partenaire)
    return m


# ── Ce qui est noté ──────────────────────────────────────────────────────────────

def test_une_lecture_par_l_app_est_notee_et_les_suivantes_s_ajoutent(monde):
    m = monde
    assert len(_lire(m, m["partenaire"])["rows"]) == 2
    _lire(m, m["partenaire"], options={"limit": 1})
    j = _journal(m)
    assert set(j) == {(m["partenaire"], "app")}
    r = j[(m["partenaire"], "app")]
    assert (r["ns_id"], r["row_reads"], r["aggregate_reads"], r["rows_served"]) \
        == (m["ns_id"], 2, 0, 3)
    assert r["last_at"] >= r["first_at"]


def test_qui_lit_par_son_propre_partage_est_note_own(monde):
    m = monde
    _lire(m, m["auteur"])
    assert _journal(m)[(m["auteur"], "own")]["row_reads"] == 1


def test_chaque_lecture_servie_d_un_lot_est_notee_les_refus_non(monde):
    m = monde
    out = _route(m, m["partenaire"], action="batch", reads=[
        {"id": "a", "action": "read_rows", "table": "fiches"},
        {"id": "b", "action": "read_rows", "table": "fiches", "options": {"limit": 1}},
        {"id": "c", "action": "read_aggregate", "table": "fiches",
         "options": {"group_by": "status"}},
        {"id": "d", "action": "read_rows", "table": "absente"}])["results"]
    assert [r["ok"] for r in out] == [True, True, True, False]
    r = _journal(m)[(m["partenaire"], "app")]
    assert (r["row_reads"], r["aggregate_reads"], r["rows_served"]) == (2, 1, 3)


def test_une_lecture_refusee_n_est_pas_notee(monde):
    m = monde
    _refus(_lire, m, m["partenaire"], options={"filter": {"notes": "x"}})
    assert _journal(m) == {}


def test_un_geste_n_est_pas_une_lecture(monde):
    """Le journal des gestes, c'est `app_consents` : un geste refusé ou servi n'ajoute
    rien ici."""
    m = monde
    _refus(_route, m, m["partenaire"], action="add_row", table="fiches",
           values={"name": "x"})
    assert _journal(m) == {}


# ── Écrit après la lecture, hors de son emprunt ──────────────────────────────────

def test_un_emprunt_pour_la_lecture_un_pour_le_journal(monde, monkeypatch):
    from oto_mcp import db
    from oto_mcp.db import _conn as dbconn
    m = monde
    _lire(m, m["partenaire"])                       # caches chauffés
    vu = {}
    reel = db.apps.note_reads

    def noter(*a, **k):
        vu["partagee"] = db.connexion_partagee()
        return reel(*a, **k)
    monkeypatch.setattr(db.apps, "note_reads", noter)
    pool = dbconn._get_pool()
    emprunts = []
    pc = pool.connection
    monkeypatch.setattr(pool, "connection", lambda *a, **k: emprunts.append(1) or pc(*a, **k))
    _lire(m, m["partenaire"])
    assert vu == {"partagee": False}
    assert len(emprunts) == 2, f"{len(emprunts)} emprunts : un pour lire, un pour noter"


def test_un_journal_en_echec_ne_refuse_pas_la_lecture(monde, monkeypatch, caplog):
    from oto_mcp import db
    m = monde

    def casse(*a, **k):
        raise RuntimeError("base partie")
    monkeypatch.setattr(db.apps, "note_reads", casse)
    with caplog.at_level(logging.ERROR, logger="oto_mcp.capabilities.apps_appel"):
        assert len(_lire(m, m["partenaire"])["rows"]) == 2
    assert any("journal des lectures" in r.getMessage() and r.exc_info
               for r in caplog.records)


def test_hors_de_l_appel_rien_n_est_note(monde):
    """`_call` seul (le chemin gardé par `test_apps_appel_une_connexion`) n'écrit rien :
    c'est le handler de la route qui note."""
    from oto_mcp.capabilities import apps_appel as C
    m = monde
    C._call(_ctx(m, m["partenaire"]), C.AppCallInput(app_id=m["app"], action="read_rows",
                                                     table="fiches"))
    assert _journal(m) == {}


# ── Le run d'un agent lancé depuis l'app ─────────────────────────────────────────

def test_le_run_qui_ouvre_le_tableau_par_l_app_est_note(monde):
    from oto_mcp import db, verrou_org
    from oto_mcp.datastore.core import make_store
    m = monde
    with db._connect() as conn:
        conn.execute("UPDATE apps SET manifest = jsonb_set(manifest, "
                     "'{roles,partner,tables,fiches}', '{}'::jsonb) WHERE id = %s", (m["app"],))
        conn.execute("UPDATE apps SET manifest = manifest #- '{tables,fiches,fields}' "
                     "WHERE id = %s", (m["app"],))
    job = int(uuid.uuid4().int % 10**9)
    db.apps.consent_issue(m["app"], m["partenaire"], "start_agent", "f", uuid.uuid4().hex, 120)
    with db._connect() as conn:
        conn.execute("UPDATE app_consents SET used_at = now(), job_id = %s WHERE app_id = %s "
                     "AND sub = %s AND action = 'start_agent'", (job, m["app"], m["partenaire"]))
    j = verrou_org.poser(verrou_org.Verrou(sub=m["partenaire"], org_id=None, job_id=job))
    try:
        make_store(m["partenaire"]).get_schema(str(m["ns_id"]))
    finally:
        verrou_org._COURANT.reset(j)
    r = _journal(m)[(m["partenaire"], "run")]
    assert r["row_reads"] >= 1 and r["rows_served"] == 0


# ── Qui le lit ───────────────────────────────────────────────────────────────────

def test_le_gerant_de_l_app_lit_tout(monde):
    m = monde
    _lire(m, m["partenaire"])
    _lire(m, m["auteur"])
    out = _apps(m, m["auteur"], op="reads")["reads"]
    assert {(r["sub"], r["access"], r["table"]) for r in out} \
        == {(m["partenaire"], "app", "fiches"), (m["auteur"], "own", "fiches")}
    p = next(r for r in out if r["sub"] == m["partenaire"])
    assert p["email"] == f"{m['partenaire']}@example.test"
    assert p["table_id"] == str(m["ns_id"]) and p["rows_served"] == 2


def test_un_lecteur_de_l_app_ne_lit_pas_le_journal(monde):
    m = monde
    _lire(m, m["partenaire"])
    assert _refus(_apps, m, m["partenaire"], op="reads").code == "forbidden"
    x = _nouveau("x")
    assert _refus(_apps, m, x, op="reads").code == "unknown_app"
    assert _refus(_apps, m, x, op="reads", app_id=None,
                  tables=[str(m["ns_id"])]).code == "forbidden"


def test_le_gerant_du_tableau_seul_lit_les_lectures_de_son_tableau(monde):
    from oto_mcp import db, org_store
    m = monde
    _lire(m, m["partenaire"])
    g = _nouveau("gt")
    org_store.add_org_member(m["org"], g, "org_member")
    db.grant_resource("datastore_namespace", str(m["ns_id"]), "user", g, role="manager",
                      granted_by=m["auteur"])
    par_l_app = _apps(m, g, op="reads")["reads"]
    assert [(r["sub"], r["table_id"]) for r in par_l_app] == [(m["partenaire"], str(m["ns_id"]))]
    toutes = _apps(m, g, op="reads", app_id=None, tables=[str(m["ns_id"])])["reads"]
    assert [(r["app_id"], r["sub"]) for r in toutes] == [(m["app"], m["partenaire"])]


def test_la_fenetre_borne_la_lecture(monde):
    from oto_mcp import db
    m = monde
    _lire(m, m["partenaire"])
    with db._connect() as conn:
        conn.execute("UPDATE app_reads SET day = day - 10 WHERE app_id = %s", (m["app"],))
    assert _apps(m, m["auteur"], op="reads", days=5)["reads"] == []
    assert len(_apps(m, m["auteur"], op="reads", days=11)["reads"]) == 1


# ── Rétention ────────────────────────────────────────────────────────────────────

def test_la_purge_retire_au_dela_de_180_jours(monde):
    from oto_mcp import db, maintenance
    m = monde
    _lire(m, m["partenaire"])
    _lire(m, m["auteur"])
    with db._connect() as conn:
        conn.execute("UPDATE app_reads SET day = day - 181 WHERE app_id = %s AND sub = %s",
                     (m["app"], m["auteur"]))
    assert db.apps.count_purgeable_reads() >= 1
    avant = maintenance.app_reads(dry_run=True)
    assert avant["retention_days"] == 180 and avant["purgeables"] >= 1
    assert set(_journal(m)) == {(m["partenaire"], "app"), (m["auteur"], "own")}
    assert maintenance.app_reads()["purged"] >= 1
    assert set(_journal(m)) == {(m["partenaire"], "app")}
