"""Une app qui PRÊTE l'accès d'une personne nommée (`oto_apps op=act_as`), de bout en
bout contre un PostgreSQL réel, sans double des capacités de lignes.

Le cas qui l'a fait naître : une app partagée à quelqu'un d'une AUTRE org, qui n'a reçu
aucun de ses tableaux — l'app s'ouvre, chaque lecture échoue. Avec le prêt, ce qu'elle
déclare se lit avec les droits de la personne qui s'y est nommée ; chaque garde retirée
tour à tour refuse, nommément (`app_author_not_lending`), en disant pourquoi.

Le lecteur appelle comme l'écran hôte : org de CONSULTATION = la sienne (ce que pose
l'adaptateur REST), jamais celle de l'app — c'est elle que le store aurait prise pour
résoudre le tableau au nom du prêteur sans l'épinglage de `apps_appel._sous`.
"""
from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest

HOTE = "https://manage.oto.cx"   # une origine que le banc déclare (`conftest.ORIGINES_CORS`)
SCHEMA = {"fields": [{"key": "name", "type": "text"}, {"key": "status", "type": "text"},
                     {"key": "secret", "type": "text"}]}


@pytest.fixture
def monde(live, monkeypatch):
    """L'org O de l'app : son admin `auteur` y possède l'app, et l'org possède le
    tableau. Le `lecteur` n'est membre que de SON org P ; l'app lui est partagée, le
    tableau non. O permet le prêt ; personne ne prête encore."""
    from oto_mcp import db, org_store, session_org
    from oto_mcp.capabilities import resources as R
    from oto_mcp.datastore.core import make_store
    monkeypatch.setattr(R.email, "send_resource_shared_email", lambda to, **kw: True)
    monkeypatch.delenv("OTO_APPS_ACT_AS_DEFAULT", raising=False)
    u = uuid.uuid4().hex[:8]
    auteur, lecteur, membre = f"auteur_{u}", f"lecteur_{u}", f"membre_{u}"
    for s in (auteur, lecteur, membre):
        db.upsert_user(s, email=f"{s}@example.test")
    org = org_store.create_org(f"o_{u}", created_by=auteur)
    org_store.add_org_member(org, auteur, "org_admin")
    org_store.add_org_member(org, membre, "org_member")
    autre = org_store.create_org(f"p_{u}", created_by=lecteur)
    org_store.add_org_member(autre, lecteur, "org_admin")
    ns_id = db.create_datastore("org", str(org), f"leads_{u}", context_org_id=org)
    jeton = session_org.set_call_org(org)
    try:
        st = make_store(auteur)
        st.set_schema(str(ns_id), SCHEMA)
        ligne = st.append_row(str(ns_id), {"name": "Acme", "status": "open", "secret": "x"})
    finally:
        session_org.reset_call_org(jeton)
    manifest = {"tables": {"leads": {"table": str(ns_id), "fields": ["name", "status"],
                                     "set": {"status": ["won", "lost"]}, "add": []}}}
    app = db.apps.create(("user", auteur), org, title=f"Desk {u}", manifest=manifest,
                         html="", thumbnail=None, by=auteur)
    db.apps.poser_apps_act_as(org, True)
    m = {"auteur": auteur, "lecteur": lecteur, "membre": membre, "org": org,
         "autre": autre, "ns_id": ns_id, "app": int(app["id"]), "row": ligne["_id"],
         "rev": ligne["_revision"], "manifest": manifest}
    _partager_app(m, "contributor")
    return m


def _partager_app(m, role, email=None):
    from oto_mcp.capabilities import resources as R
    from oto_mcp.capabilities._types import ResolvedCtx
    from oto_mcp.capabilities.resources_v2 import ResourceInputV2
    return R._resources(ResolvedCtx(sub=m["auteur"], org_id=m["org"]),
                        ResourceInputV2(op="share", resource_type="app",
                                        resource_id=str(m["app"]), audience="person",
                                        role=role,
                                        email=email or f"{m['lecteur']}@example.test"))


@contextmanager
def _ecran(sub, org):
    """Ce que l'adaptateur REST pose pour l'écran hôte : session interactive, origine
    déclarée, geste au nom du porteur, org de CONSULTATION = celle de l'écran."""
    from oto_mcp import client_trace, geste, session_org
    trace = client_trace.set_current(ip=None, user_agent=None, origin=HOTE, fetch_dest="empty")
    vue = session_org.set_view_org(org)
    try:
        with geste.portee(geste.CONSOLE, sub, f"g-{uuid.uuid4().hex[:6]}"):
            yield
    finally:
        session_org.reset_view_org(vue)
        client_trace.reset(trace)


def _lire(m, sub=None, org=None, **options):
    from oto_mcp.capabilities import apps_appel as C
    from oto_mcp.capabilities._types import ResolvedCtx
    sub, org = sub or m["lecteur"], org or m["autre"]
    with _ecran(sub, org):
        return C._call(ResolvedCtx(sub=sub, org_id=org, channel="rest"),
                       C.AppCallInput(app_id=m["app"], action="read_rows", table="leads",
                                      options=options))


def _refus_de_lecture(m, **kw):
    from oto_mcp.capabilities._types import AuthzDenied
    with pytest.raises(AuthzDenied) as e:
        _lire(m, **kw)
    return e.value


def _app_op(m, sub, org, **kw):
    from oto_mcp.capabilities import apps as A
    from oto_mcp.capabilities._types import ResolvedCtx
    return A._app(ResolvedCtx(sub=sub, org_id=org), A.AppInput(app_id=m["app"], **kw))


def _preter(m, sub=None, enabled=True):
    """Prêter comme le fait un client : lire l'app, prêter CETTE version."""
    sub = sub or m["auteur"]
    if not enabled:
        return _app_op(m, sub, m["org"], op="act_as", enabled=False)
    rev = _app_op(m, sub, m["org"], op="get")["app"]["rev"]
    return _app_op(m, sub, m["org"], op="act_as", enabled=True, expected_rev=rev)


# ── Sans prêt : le refus dit pourquoi ───────────────────────────────────────

def test_sans_pret_le_lecteur_d_une_autre_org_recoit_un_refus_nomme(monde):
    e = _refus_de_lecture(monde)
    assert (e.status, e.code) == (403, "app_author_not_lending")
    assert "nobody lends their access" in e.message


# ── Avec prêt : servi, borné au manifeste ───────────────────────────────────

def test_pret_sert_les_lignes_au_lecteur_d_une_autre_org(monde):
    out = _preter(monde)
    assert out["app"]["acts_as"]["sub"] == monde["auteur"]
    assert out["app"]["acts_as"]["tables"] == {"leads": None}
    assert "warnings" not in out
    lu = _lire(monde)
    assert [r["name"] for r in lu["rows"]] == ["Acme"]
    # Le champ que l'app ne déclare pas ne sort pas, prêt ou non.
    assert "secret" not in lu["rows"][0]


def test_le_lecteur_qui_a_son_propre_acces_lit_avec_ses_droits(monde):
    """Le prêt ne s'applique qu'à qui n'a pas d'accès propre : celui qui en a lit comme
    avant, même sans prêt."""
    from oto_mcp import db
    db.grant_resource("datastore_namespace", str(monde["ns_id"]), "user", monde["lecteur"],
                      role="viewer", granted_by=monde["auteur"])
    assert [r["name"] for r in _lire(monde)["rows"]] == ["Acme"]


def test_ecrire_par_le_pret_reste_le_geste_du_lecteur(monde):
    from oto_mcp import db
    from oto_mcp.capabilities import apps_appel as C
    from oto_mcp.capabilities._types import ResolvedCtx
    m = monde
    _preter(m)
    corps = dict(action="write_cell", table="leads", row_id=m["row"], field="status",
                 value="won", expected_revision=m["rev"])
    ctx = ResolvedCtx(sub=m["lecteur"], org_id=m["autre"], channel="rest")
    with _ecran(m["lecteur"], m["autre"]):
        accord = C._consent(ctx, C.AppCallInput(app_id=m["app"], **corps))
        assert "with the access this app lends" in accord["summary"]["text"]
        out = C._call(ctx, C.AppCallInput(app_id=m["app"], consent=accord["consent"], **corps))
    assert out["row"]["status"] == "won" and out["through_app"] is True
    with db._connect() as conn:
        dernier = conn.execute(
            "SELECT acteur FROM datastore_row_revisions WHERE ns_id = %s AND row_id = %s "
            "ORDER BY id DESC LIMIT 1", (m["ns_id"], m["row"])).fetchone()
    assert dernier["acteur"] == m["lecteur"]


def test_une_valeur_non_declaree_reste_refusee_par_le_pret(monde):
    from oto_mcp.capabilities import apps_appel as C
    from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
    m = monde
    _preter(m)
    ctx = ResolvedCtx(sub=m["lecteur"], org_id=m["autre"], channel="rest")
    with _ecran(m["lecteur"], m["autre"]), pytest.raises(AuthzDenied) as e:
        C._consent(ctx, C.AppCallInput(app_id=m["app"], action="write_cell", table="leads",
                                       row_id=m["row"], field="status", value="open",
                                       expected_revision=m["rev"]))
    assert e.value.code == "undeclared_value"


# ── Chaque garde, retirée tour à tour ───────────────────────────────────────

def test_org_qui_ne_permet_pas_le_pret(monde):
    from oto_mcp import db
    _preter(monde)
    db.apps.poser_apps_act_as(monde["org"], False)
    e = _refus_de_lecture(monde)
    assert e.code == "app_author_not_lending" and "doesn't let apps lend" in e.message


def test_defaut_de_l_instance_quand_l_org_n_a_rien_choisi(monde, monkeypatch):
    from oto_mcp import db
    _preter(monde)
    db.apps.poser_apps_act_as(monde["org"], None)
    assert _refus_de_lecture(monde).code == "app_author_not_lending"
    monkeypatch.setenv("OTO_APPS_ACT_AS_DEFAULT", "1")
    assert [r["name"] for r in _lire(monde)["rows"]] == ["Acme"]
    # Le choix de l'org passe devant le défaut.
    db.apps.poser_apps_act_as(monde["org"], False)
    assert _refus_de_lecture(monde).code == "app_author_not_lending"


def test_manifeste_reecrit_par_un_autre_fige_le_pret(monde):
    """Un éditeur qui n'est pas le prêteur change le manifeste : l'app ne prête plus,
    l'écriture le dit ; le prêteur prête à nouveau, c'est servi."""
    from oto_mcp import db
    m = monde
    _preter(m)
    db.grant_resource("app", str(m["app"]), "user", m["membre"], role="editor",
                      granted_by=m["auteur"])
    db.grant_resource("datastore_namespace", str(m["ns_id"]), "user", m["membre"],
                      role="viewer", granted_by=m["auteur"])
    autre = {"tables": {"leads": {**m["manifest"]["tables"]["leads"],
                                  "fields": ["name", "status", "secret"]}}}
    out = _app_op(m, m["membre"], m["org"], op="update", manifest=autre)
    assert any("no longer lends" in w for w in out.get("warnings", []))
    e = _refus_de_lecture(m)
    assert e.code == "app_author_not_lending" and "manifest changed" in e.message
    _preter(m)
    assert "secret" in _lire(m)["rows"][0]


def test_le_preteur_qui_reecrit_son_manifeste_garde_le_pret(monde):
    m = monde
    _preter(m)
    autre = {"tables": {"leads": {**m["manifest"]["tables"]["leads"], "fields": ["name"]}}}
    out = _app_op(m, m["auteur"], m["org"], op="update", manifest=autre)
    assert not any("no longer lends" in w for w in out.get("warnings", []))
    assert _lire(m)["rows"][0]["name"] == "Acme"


def test_tableau_declare_sans_liste_de_champs_ne_se_prete_pas(monde):
    m = monde
    tout = {"tables": {"leads": {**m["manifest"]["tables"]["leads"], "fields": None}}}
    _app_op(m, m["auteur"], m["org"], op="update", manifest=tout)
    out = _preter(m)
    assert "list of fields" in out["app"]["acts_as"]["tables"]["leads"]
    assert any("isn't lent" in w for w in out["warnings"])
    e = _refus_de_lecture(m)
    assert e.code == "app_author_not_lending" and "list of fields" in e.message


def test_preteur_qui_ne_peut_pas_partager_le_tableau(monde):
    """Un membre simple, propriétaire de SA copie de l'app, qui lit le tableau sans
    pouvoir le partager : prêter, c'est partager — refusé."""
    from oto_mcp import db
    m = monde
    db.grant_resource("datastore_namespace", str(m["ns_id"]), "user", m["membre"],
                      role="editor", granted_by=m["auteur"])
    app = db.apps.create(("user", m["membre"]), m["org"], title="Copie",
                         manifest=m["manifest"], html="", thumbnail=None, by=m["membre"])
    m2 = {**m, "app": int(app["id"])}
    db.grant_resource("app", str(m2["app"]), "user", m["lecteur"], role="viewer",
                      granted_by=m["membre"])
    out = _preter(m2, m["membre"])
    assert "can't share this table" in out["app"]["acts_as"]["tables"]["leads"]
    e = _refus_de_lecture(m2)
    assert e.code == "app_author_not_lending" and "can't share this table" in e.message


def test_preteur_sorti_de_l_org_ne_prete_plus(monde):
    """Le prêteur n'est pas l'auteur : un autre admin renomme l'app (il en devient
    l'auteur, le manifeste ne bouge pas, le prêt tient), puis le prêteur quitte l'org."""
    from oto_mcp import db, org_store
    m = monde
    _preter(m)
    org_store.add_org_member(m["org"], m["membre"], "org_admin")
    db.grant_resource("app", str(m["app"]), "user", m["membre"], role="editor",
                      granted_by=m["auteur"])
    _app_op(m, m["membre"], m["org"], op="update", title="Renamed")
    assert [r["name"] for r in _lire(m)["rows"]] == ["Acme"]
    org_store.remove_org_member(m["org"], m["auteur"])
    e = _refus_de_lecture(m)
    assert e.code == "app_author_not_lending" and "no longer in" in e.message


def test_l_app_cesse_de_preter(monde):
    m = monde
    _preter(m)
    assert _preter(m, enabled=False)["app"].get("acts_as") is None
    assert _refus_de_lecture(m).code == "app_author_not_lending"


# ── Qui prête ───────────────────────────────────────────────────────────────

def test_un_lecteur_avec_use_ne_prete_pas(monde):
    from oto_mcp.capabilities._types import AuthzDenied
    with pytest.raises(AuthzDenied) as e:
        _app_op(monde, monde["lecteur"], monde["autre"], op="act_as", enabled=True)
    assert (e.value.status, e.value.code) == (403, "forbidden")


def test_preter_dans_une_org_qui_ne_le_permet_pas(monde):
    from oto_mcp import db
    from oto_mcp.capabilities._types import AuthzDenied
    db.apps.poser_apps_act_as(monde["org"], False)
    with pytest.raises(AuthzDenied) as e:
        _preter(monde)
    assert (e.value.status, e.value.code) == (403, "act_as_off")


def test_le_pret_n_est_montre_qu_aux_editeurs(monde):
    m = monde
    _preter(m)
    vu = _app_op(m, m["lecteur"], m["autre"], op="get")["app"]
    assert "acts_as" not in vu
    assert _app_op(m, m["auteur"], m["org"], op="get")["app"]["acts_as"]["current"] is True


def test_preter_une_version_qu_on_n_a_pas_lue(monde):
    from oto_mcp.capabilities._types import AuthzDenied
    with pytest.raises(AuthzDenied) as e:
        _app_op(monde, monde["auteur"], monde["org"], op="act_as", enabled=True,
                expected_rev="0000000000000000")
    assert (e.value.status, e.value.code) == (409, "conflict")
    with pytest.raises(AuthzDenied) as e:
        _app_op(monde, monde["auteur"], monde["org"], op="act_as", enabled=True)
    assert e.value.code == "missing_expected_rev"


def test_un_agent_heberge_ne_prete_pas(monde, monkeypatch):
    from oto_mcp import verrou_org
    from oto_mcp.capabilities._types import AuthzDenied
    monkeypatch.setattr(verrou_org, "travail_de_la_requete", lambda: object())
    with pytest.raises(AuthzDenied) as e:
        _preter(monde)
    assert (e.value.status, e.value.code) == (403, "act_as_from_agent")


def test_preteur_qui_ne_gere_plus_l_app_ne_prete_plus_et_peut_toujours_l_arreter(monde):
    """L'app passe à un autre : celui qui a prêté ne décide plus à qui elle s'ouvre —
    l'app ne prête plus. Il ne la lit plus, et retire quand même son prêt."""
    from oto_mcp import db
    m = monde
    _preter(m)
    db.apps.reparent(m["app"], "user", m["membre"])
    db.grant_resource("datastore_namespace", str(m["ns_id"]), "user", m["membre"],
                      role="viewer", granted_by=m["auteur"])
    e = _refus_de_lecture(m)
    assert e.code == "app_author_not_lending" and "no longer manages" in e.message
    assert _preter(m, enabled=False) == {"ok": True}
    assert db.apps.get(m["app"])["acts_as"] is None


def test_un_lot_melange_le_prete_et_l_accessible(monde):
    """Un écran s'ouvre par un lot : la lecture d'un tableau prêté et celle d'un tableau
    que le lecteur lit lui-même, chacune servie sous son nom."""
    from oto_mcp import db, session_org
    from oto_mcp.capabilities import apps_appel as C
    from oto_mcp.capabilities._types import ResolvedCtx
    from oto_mcp.datastore.core import make_store
    m = monde
    propre = db.create_datastore("org", str(m["org"]), f"notes_{m['app']}", context_org_id=m["org"])
    jeton = session_org.set_call_org(m["org"])
    try:
        st = make_store(m["auteur"])
        st.set_schema(str(propre), {"fields": [{"key": "name", "type": "text"}]})
        st.append_row(str(propre), {"name": "Note"})
    finally:
        session_org.reset_call_org(jeton)
    db.grant_resource("datastore_namespace", str(propre), "user", m["lecteur"],
                      role="viewer", granted_by=m["auteur"])
    deux = {"tables": {**m["manifest"]["tables"],
                       "notes": {"table": str(propre), "fields": ["name"], "set": {},
                                 "add": []}}}
    _app_op(m, m["auteur"], m["org"], op="update", manifest=deux)
    _preter(m)
    ctx = ResolvedCtx(sub=m["lecteur"], org_id=m["autre"], channel="rest")
    with _ecran(m["lecteur"], m["autre"]):
        out = C._call(ctx, C.AppCallInput(app_id=m["app"], action="batch", reads=[
            {"id": "a", "action": "read_rows", "table": "leads"},
            {"id": "b", "action": "read_rows", "table": "notes"}]))
    par_id = {r["id"]: r for r in out["results"]}
    assert par_id["a"]["ok"] and par_id["a"]["rows"][0]["name"] == "Acme"
    assert par_id["b"]["ok"] and par_id["b"]["rows"][0]["name"] == "Note"
    _preter(m, enabled=False)
    with _ecran(m["lecteur"], m["autre"]):
        out = C._call(ctx, C.AppCallInput(app_id=m["app"], action="batch", reads=[
            {"id": "a", "action": "read_rows", "table": "leads"},
            {"id": "b", "action": "read_rows", "table": "notes"}]))
    par_id = {r["id"]: r for r in out["results"]}
    assert par_id["a"]["code"] == "app_author_not_lending" and par_id["b"]["ok"]


def test_la_liste_dit_le_pret_a_ses_editeurs_seuls(monde):
    """Une carte d'app porte `lends` pour qui peut la modifier — l'état du prêt sans
    lire le manifeste ni le HTML — et rien pour un lecteur."""
    from oto_mcp import db
    m = monde
    _preter(m)
    carte = next(c for c in _app_op(m, m["auteur"], m["org"], op="list")["apps"]
                 if c["id"] == m["app"])
    assert carte["lends"] == {"sub": m["auteur"], "email": f"{m['auteur']}@example.test",
                              "current": True}
    db.grant_resource("app", str(m["app"]), "user", m["membre"], role="editor",
                      granted_by=m["auteur"])
    db.grant_resource("datastore_namespace", str(m["ns_id"]), "user", m["membre"],
                      role="viewer", granted_by=m["auteur"])
    autre = {"tables": {"leads": {**m["manifest"]["tables"]["leads"], "fields": ["name"]}}}
    _app_op(m, m["membre"], m["org"], op="update", manifest=autre)
    carte = next(c for c in _app_op(m, m["auteur"], m["org"], op="list")["apps"]
                 if c["id"] == m["app"])
    assert carte["lends"]["current"] is False
    from oto_mcp.capabilities import apps as A
    vu = A._cards(m["lecteur"], [db.apps.get(m["app"])])[0]
    assert "lends" not in vu
