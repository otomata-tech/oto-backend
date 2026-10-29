"""Capacité `oto_apps` (`me.app`) — apps rangées dans un projet, pas des pages.

L'accès est celui du PROJET ; le manifeste se juge à l'écriture contre l'AUTEUR et
l'org du PROJET ; écrire est réservé à la population bêta. On monkeypatche `db.apps`,
`ownership`, l'option bêta et les sources du jugement (store, déclencheurs, org).
Les assertions portent sur le CODE des refus, jamais sur leur phrase.
"""
import pytest

from oto_mcp import access, db, ownership
from oto_mcp.capabilities import apps as A
from oto_mcp.capabilities import apps_manifeste as M
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx

CTX = ResolvedCtx(sub="u1", org_id=42)
ROW = {"id": 5, "project_id": 7, "title": "Lead review", "manifest": {}, "html": "<p>x</p>",
       "thumbnail": None, "created_by": "u1", "created_at": "t", "updated_by": "u1",
       "updated_at": "t", "archived_at": None, "rev": "abc"}
GOOD = {"title": "Leads", "tables": {"leads": {"table": "12", "fields": ["name", "status"],
                                              "set": {"status": ["ok", "no"]}}},
        "agents": {"enrich": {"id": 9, "run": True, "input": True}, "watch": 10}}


class _Store:
    """Un store qui voit le tableau 12 (champs name, status) et rien d'autre."""

    def _resolve(self, ref, write=False):
        if str(ref) != "12":
            from oto_mcp.datastore.core import DatastoreNotFound
            raise DatastoreNotFound(ref)
        return 12

    def get_schema(self, ref):
        return {"fields": [{"key": "name"}, {"key": "status"}]}


ORG_DU_PROJET = 42


@pytest.fixture
def seams(monkeypatch):
    rec = {"create": [], "update": [], "access": {}, "beta": True, "options": []}
    rights = {"read": True, "write": True}
    monkeypatch.setattr(ownership, "can_access",
                        lambda sub, t, rid, want="read": rights[want])
    monkeypatch.setattr(ownership, "can_access_many",
                        lambda sub, t, owners, want="read": {k: k != "8" for k in owners})
    monkeypatch.setattr(db.apps, "get", lambda i: dict(ROW, id=i) if i == 5 else None)
    monkeypatch.setattr(db.apps, "create",
                        lambda pid, **k: rec["create"].append((pid, k)) or dict(ROW, project_id=pid, **{
                            x: k[x] for x in ("title", "manifest", "html", "thumbnail")}))

    def _update(app_id, *, by, expected_rev=None, **fields):
        if expected_rev not in (None, "abc"):
            raise db.apps.AppConflict("abc")
        rec["update"].append((app_id, by, fields))
        return dict(ROW, **{k: v for k, v in fields.items() if v is not None and v is not ...})

    monkeypatch.setattr(db.apps, "update", _update)
    monkeypatch.setattr(db.apps, "project_owners",
                        lambda ids: {str(i): ("org", "42") for i in ids})
    monkeypatch.setattr(db.apps, "list_for_projects",
                        lambda ids, archived=False: [dict(ROW, project_id=i) for i in ids])
    monkeypatch.setattr(db, "log_project_activity", lambda *a, **k: None)
    monkeypatch.setattr(db, "get_trigger",
                        lambda tid, org: {"id": tid} if tid in (9, 10) and org == ORG_DU_PROJET else None)
    import oto_mcp.datastore.core as core
    monkeypatch.setattr(core, "make_store", lambda sub: _Store())
    # Le tableau 12 vit dans l'org du projet ; tout autre ailleurs.
    org_des_tableaux = {"12": ORG_DU_PROJET}
    monkeypatch.setattr(M, "tableau_de_l_org",
                        lambda ns, org: org is not None and org_des_tableaux.get(str(ns)) == org)
    monkeypatch.setattr(A, "org_du_projet", lambda pid: ORG_DU_PROJET)

    def has_option(sub, option, *, org=None):
        rec["options"].append((sub, option, org))
        return rec["beta"]
    monkeypatch.setattr(access, "has_option", has_option)
    monkeypatch.setattr(db.apps, "set_archived", lambda i, a: dict(ROW, id=i))
    monkeypatch.setattr(db.apps, "revision", lambda app_id, rid: {
        "id": rid, "title": "old", "manifest": rec.get("old_manifest", {}), "html": "<p>0</p>",
        "thumbnail": None} if rid == 3 else None)
    rec["rights"], rec["orgs"] = rights, org_des_tableaux
    return rec


def _refus(fn) -> AuthzDenied:
    with pytest.raises(AuthzDenied) as e:
        fn()
    return e.value


# ── La forme ────────────────────────────────────────────────────────────────

def test_forme_normalise_agents_et_bornes():
    clean, problems = M.forme(GOOD)
    assert problems == []
    assert clean["agents"] == {"enrich": {"id": 9, "run": True, "input": True},
                               "watch": {"id": 10, "run": False, "input": False}}
    assert clean["tables"]["leads"]["set"] == {"status": ["ok", "no"]}
    assert clean["height"] == M.DEFAULT_HEIGHT


def test_forme_input_sans_run_ne_vaut_rien():
    clean, _ = M.forme({"agents": {"a": {"id": 3, "input": True}}})
    assert clean["agents"]["a"] == {"id": 3, "run": False, "input": False}


@pytest.mark.parametrize("raw", [
    [], {"tables": {"bad name": {"table": "1"}}}, {"tables": {"t": {}}},
    {"tables": {"t": {"table": "1", "fields": "name"}}}, {"agents": {"a": 0}},
])
def test_forme_refuse(raw):
    _, problems = M.forme(raw)
    assert problems


# ── L'auteur ────────────────────────────────────────────────────────────────

def test_auteur_voit_tout(seams):
    clean, _ = M.forme(GOOD)
    assert M.auteur("u1", ORG_DU_PROJET, clean) == []
    assert clean["tables"]["leads"]["table"] == "12"


def test_auteur_tableau_invisible_champ_inconnu_agent_absent(seams):
    clean, _ = M.forme({"tables": {"a": {"table": "99"}, "b": {"table": "12", "fields": ["secret"]}},
                        "agents": {"x": 77}})
    problems = M.auteur("u1", ORG_DU_PROJET, clean)
    assert len(problems) == 3


def test_auteur_borne_a_l_org_du_projet_pas_a_l_org_active(seams):
    """Le tableau et l'agent existent pour l'auteur, mais dans une AUTRE org que
    celle du projet : refusés (l'org active de l'appel n'y change rien)."""
    seams["orgs"]["12"] = 99
    clean, _ = M.forme(GOOD)
    problems = M.auteur("u1", ORG_DU_PROJET, clean)
    assert any("organization" in p and "tables.leads" in p for p in problems)
    clean, _ = M.forme(GOOD)
    seams["orgs"]["12"] = ORG_DU_PROJET
    problems = M.auteur("u1", 7, clean)  # agents 9 et 10 vivent dans l'org 42
    assert sorted(p.split(":")[0] for p in problems) == ["agents.enrich", "agents.watch",
                                                         "tables.leads"]


def test_projet_sans_org_ne_declare_rien(seams):
    clean, _ = M.forme(GOOD)
    assert len(M.auteur("u1", None, clean)) == 3


def test_manifeste_trop_lourd():
    _, problems = M.forme({"title": "x" * (M.MAX_MANIFEST + 1)})
    assert problems and "characters" in problems[0]


# ── La capacité ─────────────────────────────────────────────────────────────

def test_create_ecrit_le_manifeste_normalise(seams):
    out = A._app(CTX, A.AppInput(op="create", project_id=7, title=" Leads ", manifest=GOOD,
                                 html="<p>hi</p>"))
    (pid, k), = seams["create"]
    assert pid == 7 and k["title"] == "Leads" and k["by"] == "u1"
    assert k["manifest"]["agents"]["watch"]["run"] is False
    assert out["app"]["project_id"] == 7


def test_create_sans_ecriture_refuse(seams):
    seams["rights"]["write"] = False
    e = _refus(lambda: A._app(CTX, A.AppInput(op="create", project_id=7, title="x", manifest={})))
    assert (e.status, e.code) == (403, "forbidden")
    assert seams["create"] == []


def test_create_tableau_hors_de_portee_de_l_auteur(seams):
    e = _refus(lambda: A._app(CTX, A.AppInput(op="create", project_id=7, title="x",
                                              manifest={"tables": {"t": {"table": "99"}}})))
    assert (e.status, e.code) == (400, "invalid_manifest")
    assert seams["create"] == []


def test_validate_n_ecrit_rien(seams):
    out = A._app(CTX, A.AppInput(op="validate", project_id=7,
                                 manifest={"tables": {"t": {"table": "99"}}}))
    assert out["ok"] is False and out["problems"]
    assert seams["create"] == [] and seams["update"] == []
    assert A._app(CTX, A.AppInput(op="validate", app_id=5, manifest=GOOD))["ok"] is True


def test_validate_exige_le_projet(seams):
    """L'org où l'app vivra décide de ce qu'elle peut déclarer : sans projet, rien à
    juger — et rien n'est deviné de l'org active."""
    e = _refus(lambda: A._app(CTX, A.AppInput(op="validate", manifest=GOOD)))
    assert e.code == "missing_project"
    seams["rights"]["read"] = False
    e = _refus(lambda: A._app(CTX, A.AppInput(op="validate", project_id=7, manifest=GOOD)))
    assert (e.status, e.code) == (403, "forbidden")


# ── La population bêta ──────────────────────────────────────────────────────

_ECRITURES = [
    dict(op="create", project_id=7, title="x", manifest={}),
    dict(op="update", app_id=5, title="y"),
    dict(op="archive", app_id=5),
    dict(op="unarchive", app_id=5),
    dict(op="restore", app_id=5, revision_id=3),
]


@pytest.mark.parametrize("inp", _ECRITURES, ids=lambda d: d["op"])
def test_ecrire_une_app_exige_la_beta(seams, inp):
    seams["beta"] = False
    e = _refus(lambda: A._app(CTX, A.AppInput(**inp)))
    assert (e.status, e.code) == (403, "apps_beta_only")
    assert seams["create"] == [] and seams["update"] == []
    assert seams["options"] == [("u1", "beta", 42)]


@pytest.mark.parametrize("inp", _ECRITURES, ids=lambda d: d["op"])
def test_ecrire_une_app_avec_la_beta(seams, inp):
    A._app(CTX, A.AppInput(**inp))


def test_beta_fail_closed(seams, monkeypatch):
    def panne(*a, **k):
        raise RuntimeError("options en panne")
    monkeypatch.setattr(access, "has_option", panne)
    e = _refus(lambda: A._app(CTX, A.AppInput(op="create", project_id=7, title="x", manifest={})))
    assert e.code == "apps_beta_only" and seams["create"] == []


@pytest.mark.parametrize("inp", [dict(op="get", app_id=5), dict(op="revisions", app_id=5),
                                 dict(op="list", project_ids=[7]),
                                 dict(op="validate", project_id=7, manifest={})],
                         ids=lambda d: d["op"])
def test_lire_ne_demande_pas_la_beta(seams, monkeypatch, inp):
    seams["beta"] = False
    monkeypatch.setattr(db.apps, "revisions", lambda i: [])
    A._app(CTX, A.AppInput(**inp))
    assert seams["options"] == []


def test_refus_beta_declare_et_route_sans_jeton_api():
    from oto_mcp.capabilities import registry
    cap = next(c for c in registry.CAPABILITIES if c.key == "me.app")
    assert ("apps_beta_only", 403) in {(e.code, e.status) for e in cap.errors}
    (b,) = cap.rest_bindings()
    assert b.allow_api_token is False and b.allow_issuer_token is False


# ── La restauration rejuge ──────────────────────────────────────────────────

def test_restore_rejuge_le_manifeste(seams):
    """Une révision d'hier peut nommer un tableau que celui qui restaure ne lit pas, ou
    qui a quitté l'org du projet : restaurer, c'est réécrire — refusé pareil."""
    seams["old_manifest"] = {"tables": {"t": {"table": "99"}}}
    e = _refus(lambda: A._app(CTX, A.AppInput(op="restore", app_id=5, revision_id=3)))
    assert (e.status, e.code) == (400, "invalid_manifest")
    assert seams["update"] == []
    seams["old_manifest"] = GOOD
    A._app(CTX, A.AppInput(op="restore", app_id=5, revision_id=3))
    (_, by, fields), = seams["update"]
    assert by == "u1" and fields["manifest"]["tables"]["leads"]["table"] == "12"
    assert fields["manifest"]["agents"]["watch"] == {"id": 10, "run": False, "input": False}


def test_get_inconnue_et_illisible_meme_404(seams):
    e1 = _refus(lambda: A._app(CTX, A.AppInput(op="get", app_id=404)))
    seams["rights"]["read"] = False
    e2 = _refus(lambda: A._app(CTX, A.AppInput(op="get", app_id=5)))
    assert (e1.status, e1.code) == (e2.status, e2.code) == (404, "unknown_app")


def test_update_lecteur_seul_refuse(seams):
    seams["rights"]["write"] = False
    e = _refus(lambda: A._app(CTX, A.AppInput(op="update", app_id=5, title="y")))
    assert (e.status, e.code) == (403, "forbidden")


def test_update_conflit(seams):
    e = _refus(lambda: A._app(CTX, A.AppInput(op="update", app_id=5, title="y", expected_rev="old")))
    assert (e.status, e.code) == (409, "conflict")


def test_update_vignette_effacee_ou_gardee(seams):
    A._app(CTX, A.AppInput(op="update", app_id=5, title="y"))
    A._app(CTX, A.AppInput(op="update", app_id=5, clear_thumbnail=True))
    assert seams["update"][0][2]["thumbnail"] is ...
    assert seams["update"][1][2]["thumbnail"] is None


def test_vignette_cles_inconnues_refusees(seams):
    e = _refus(lambda: A._app(CTX, A.AppInput(op="update", app_id=5, thumbnail={"script": "x"})))
    assert e.code == "invalid_thumbnail"


def test_html_trop_lourd(seams):
    e = _refus(lambda: A._app(CTX, A.AppInput(op="update", app_id=5, html="x" * (A.MAX_HTML + 1))))
    assert e.code == "html_too_large"


def test_list_ne_rend_que_les_projets_lisibles(seams):
    out = A._app(CTX, A.AppInput(op="list", project_ids=[7, 8]))
    assert [a["project_id"] for a in out["apps"]] == [7]


def test_list_exige_des_projets(seams):
    e = _refus(lambda: A._app(CTX, A.AppInput(op="list")))
    assert e.code == "missing_project"


def test_vignette_image_seulement_sur_notre_stockage(seams, monkeypatch):
    from oto_mcp import media_store
    monkeypatch.setattr(media_store, "public_base", lambda: "https://media.example")
    A._app(CTX, A.AppInput(op="update", app_id=5, thumbnail={"image": "https://media.example/a.png"}))
    e = _refus(lambda: A._app(CTX, A.AppInput(op="update", app_id=5,
                                              thumbnail={"image": "https://tiers.example/a.png"})))
    assert e.code == "invalid_thumbnail"
    e = _refus(lambda: A._app(CTX, A.AppInput(op="update", app_id=5,
                                              thumbnail={"image": "https://media.example.tiers.example/a.png"})))
    assert e.code == "invalid_thumbnail"


def test_vignette_image_refusee_sans_stockage_declare(seams, monkeypatch):
    from oto_mcp import media_store

    def _absente():
        raise RuntimeError("OTO_MCP_S3_ENDPOINT")
    monkeypatch.setattr(media_store, "public_base", _absente)
    e = _refus(lambda: A._app(CTX, A.AppInput(op="update", app_id=5,
                                              thumbnail={"image": "https://media.example/a.png"})))
    assert e.code == "invalid_thumbnail"


# ── L'org du projet, et l'org d'un tableau ──────────────────────────────────

def test_org_ou_vit_suit_le_proprietaire_et_le_rangement(monkeypatch):
    from oto_mcp import org_origin
    kinds = {
        "x-org": ownership.ResourceKind(owner_getter=lambda r: ("org", "5"), reparent=None),
        "x-perso": ownership.ResourceKind(owner_getter=lambda r: ("user", "u"), reparent=None,
                                          context_org=lambda r: 8 if r == "1" else None),
        "x-groupe": ownership.ResourceKind(owner_getter=lambda r: ("group", "3"), reparent=None),
        "x-absent": ownership.ResourceKind(owner_getter=lambda r: None, reparent=None),
    }
    monkeypatch.setattr(ownership, "RESOURCE_KINDS", kinds)
    monkeypatch.setattr(org_origin, "group_orgs",
                        lambda owners: {3: 6} if any(o[0] == "group" for o in owners) else {})
    assert ownership.org_ou_vit("x-org", "1") == (5, True)
    assert ownership.org_ou_vit("x-perso", "1") == (8, True)
    assert ownership.org_ou_vit("x-perso", "2") == (None, False)
    assert ownership.org_ou_vit("x-groupe", "1") == (6, True)
    assert ownership.org_ou_vit("x-absent", "1") == (None, False)


def test_org_du_projet_perso_sans_rangement_est_son_org_perso(monkeypatch):
    from oto_mcp import org_store
    monkeypatch.setattr(ownership, "org_ou_vit", lambda t, r: (None, False))
    monkeypatch.setattr(ownership, "owner_of", lambda t, r: ("user", "u9"))
    monkeypatch.setattr(org_store, "get_personal_org", lambda sub: 77 if sub == "u9" else None)
    assert M.org_du_projet(7) == 77
    monkeypatch.setattr(ownership, "owner_of", lambda t, r: None)
    assert M.org_du_projet(7) is None
    monkeypatch.setattr(ownership, "org_ou_vit", lambda t, r: (12, True))
    assert M.org_du_projet(7) == 12


def test_tableau_de_l_org(monkeypatch):
    monkeypatch.setattr(ownership, "org_ou_vit", lambda t, r: {"1": (4, True), "2": (None, False),
                                                               "3": (None, True)}[r])
    assert M.tableau_de_l_org("1", 4) is True
    assert M.tableau_de_l_org("1", 5) is False
    assert M.tableau_de_l_org("1", None) is False
    assert M.tableau_de_l_org("2", 4) is False
    assert M.tableau_de_l_org("3", 4) is False


# ── Ce que le HTML demande hors du manifeste ────────────────────────────────

_TRI = '<script>tulina.rows("leads", {orderBy: "score"})</script>'


def test_ecrire_rend_les_avertissements_du_html(seams):
    """L'app s'écrit quand même ; qui l'écrit apprend tout de suite qu'elle sera
    refusée à l'ouverture."""
    out = A._app(CTX, A.AppInput(op="create", project_id=7, title="x", manifest=GOOD, html=_TRI))
    assert out["app"] and any('"score"' in w for w in out["warnings"])
    assert seams["create"]
    ok = A._app(CTX, A.AppInput(op="create", project_id=7, title="x", manifest=GOOD,
                                html='<script>tulina.rows("leads", {orderBy: "_created_at"})</script>'))
    assert "warnings" not in ok


def test_validate_juge_le_html(seams):
    out = A._app(CTX, A.AppInput(op="validate", project_id=7, manifest=GOOD, html=_TRI))
    assert out["ok"] is True and out["warnings"]


def test_validate_ne_juge_pas_le_html_d_un_manifeste_a_corriger(seams):
    out = A._app(CTX, A.AppInput(op="validate", project_id=7, manifest={"tables": "leads"}, html=_TRI))
    assert out["ok"] is False and "warnings" not in out
