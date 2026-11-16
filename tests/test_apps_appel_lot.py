"""Un LOT de lectures d'app (`POST /api/me/apps/{id}/call`, `action: "batch"`).

L'écran d'une app s'ouvre sur huit à quinze lectures ; chacune prenait une des 8 places
de la route pour tout le processus et rejouait l'app, son partage et son tableau. Le lot
les sert en une requête. Ce qu'il doit tenir, sur la base réelle :

1. chaque lecture rend EXACTEMENT ce que son appel seul rend — lignes, groupes, refus ;
2. le refus d'une lecture est SA réponse, les autres sont servies ;
3. un geste (ou une action qui n'est pas une lecture) n'entre pas dans un lot ;
4. la forme du lot est bornée (10 lectures, des `id` uniques) ;
5. un seul emprunt au pool, un chemin qui ne fait que lire ; l'app (la cible du lot)
   jugée une fois, le tableau de CHAQUE lecture jugé par elle, comme un appel seul ;
   une lecture coupée par `statement_timeout` est SA réponse ;
6. au débit par jeton, un lot de N lectures coûte N appels — par la vraie garde et la
   vraie route.
"""
from __future__ import annotations

import re
import types
import uuid

import psycopg
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from oto_mcp.capabilities import apps_appel as C
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx

_ECRITURE = re.compile(
    r"^\s*(INSERT|UPDATE|DELETE|MERGE|UPSERT|CREATE|ALTER|DROP|TRUNCATE)\b|\bFOR\s+UPDATE\b",
    re.IGNORECASE)


@pytest.fixture
def monde(live):
    """Une org, son auteur et un lecteur membres ; un tableau de l'org à 30 lignes, un
    tableau personnel du lecteur (hors de l'org) ; une app de l'org qui déclare le
    premier sous deux champs (`t`), en entier (`tout`), et le second (`dehors`)."""
    from oto_mcp import db, org_store
    from oto_mcp.datastore.core import make_store
    u = uuid.uuid4().hex[:8]
    auteur, lecteur = f"auteur_{u}", f"lecteur_{u}"
    for sub in (auteur, lecteur):
        db.upsert_user(sub, email=f"{sub}@example.test")
    org = int(org_store.create_org(f"o_{u}"))
    for sub in (auteur, lecteur):
        org_store.add_org_member(org, sub)
        org_store.set_active_org(sub, org)
    ns = db.create_datastore("org", str(org), f"t_{u}")
    store = make_store(auteur)
    store.set_schema(str(ns), {"key": "nom", "fields": [
        {"key": "nom", "type": "text"}, {"key": "score", "type": "number"},
        {"key": "etat", "type": "text"}, {"key": "secret", "type": "text"}]})
    store.write_rows(str(ns), [{"nom": f"n{i}", "score": i, "etat": "ok" if i % 3 else "ko",
                                "secret": "x"} for i in range(30)], key="nom")
    perso = db.create_datastore("user", lecteur, f"p_{u}")
    app = db.apps.create(("org", str(org)), org, title="Cockpit", by=auteur,
                         manifest={"tables": {
                             "t": {"table": str(ns), "fields": ["nom", "score", "etat"],
                                   "set": {"etat": ["ok", "ko"]}, "add": ["nom"]},
                             "tout": {"table": str(ns), "fields": None, "set": {}, "add": []},
                             "dehors": {"table": str(perso), "fields": None, "set": {},
                                        "add": []}},
                             "agents": {}, "files": True},
                         html="", thumbnail=None)
    return {"lecteur": lecteur, "auteur": auteur, "org": org, "app": int(app["id"])}


def _ctx(m) -> ResolvedCtx:
    return ResolvedCtx(sub=m["lecteur"], org_id=m["org"])


def _lot(m, reads, **kw) -> list[dict]:
    return C._call(_ctx(m), C.AppCallInput(app_id=m["app"], action="batch", reads=reads,
                                           **kw))["results"]


def _seul(m, read: dict) -> dict:
    """Ce qu'un appel seul rend pour cette lecture — ou son refus, sous la forme d'une
    réponse de lot."""
    corps = {k: read[k] for k in ("action", "table", "options") if k in read}
    try:
        return {"ok": True, **C._call(_ctx(m), C.AppCallInput(app_id=m["app"], **corps))}
    except AuthzDenied as d:
        return {"ok": False, "status": d.status, "code": d.code, "detail": d.message or None}


# Des lectures servies et refusées, mêlées : chacune, seule, rend ce qu'elle rend.
LECTURES = [
    {"id": "page", "action": "read_rows", "table": "t",
     "options": {"limit": 7, "order_by": "score", "order_dir": "asc"}},
    {"id": "filtre", "action": "read_rows", "table": "t",
     "options": {"filter": {"etat": "ko"}, "limit": 50}},
    {"id": "somme", "action": "read_aggregate", "table": "t",
     "options": {"metrics": [{"op": "sum", "field": "score", "label": "s"}]}},
    {"id": "par_etat", "action": "read_aggregate", "table": "t",
     "options": {"group_by": "etat"}},
    {"id": "cache", "action": "read_rows", "table": "t", "options": {"filter": {"secret": "x"}}},
    {"id": "inconnue", "action": "read_rows", "table": "absente"},
    {"id": "dehors", "action": "read_rows", "table": "dehors"},
    {"id": "options", "action": "read_rows", "table": "t", "options": {"limit": "dix"}},
    {"id": "recherche", "action": "read_rows", "table": "t", "options": {"q": "n1"}},
    {"id": "tout", "action": "read_rows", "table": "tout", "options": {"q": "n1", "limit": 3}},
]


def test_chaque_lecture_rend_ce_que_rend_son_appel_seul(monde):
    attendus = [_seul(monde, r) for r in LECTURES]
    assert [a["ok"] for a in attendus] == [True, True, True, True, False, False, False,
                                          False, False, True], "le banc ne mêle plus servi et refusé"
    rendus = _lot(monde, LECTURES)
    assert [r["id"] for r in rendus] == [r["id"] for r in LECTURES]
    for lu, attendu in zip(rendus, attendus):
        assert {k: v for k, v in lu.items() if k != "id"} == attendu, lu["id"]


def test_le_refus_d_une_lecture_ne_coule_pas_les_autres(monde):
    rendus = {r["id"]: r for r in _lot(monde, [
        {"id": "a", "action": "read_rows", "table": "absente"},
        {"id": "b", "action": "read_rows", "table": "t", "options": {"limit": 3}},
        {"id": "c", "action": "read_rows", "table": "t", "options": {"filter": {"secret": "x"}}},
        {"id": "d", "action": "read_aggregate", "table": "t"},
    ])}
    assert rendus["a"]["ok"] is False and rendus["a"]["code"] == "undeclared_table"
    assert rendus["a"]["status"] == 400
    assert rendus["c"]["ok"] is False and rendus["c"]["code"] == "undeclared_field"
    assert rendus["b"]["ok"] is True and len(rendus["b"]["rows"]) == 3
    assert rendus["b"]["total"] == 30
    assert all("secret" not in r for r in rendus["b"]["rows"])
    assert rendus["d"]["groups"][0]["count"] == 30


@pytest.mark.parametrize("action", ["write_cell", "add_row", "start_agent", "upload_file",
                                    "list_files", "file_url", "agent_runs", "run_result",
                                    "batch"])
def test_un_geste_ou_une_autre_action_n_entre_pas_dans_un_lot(monde, action):
    rendus = _lot(monde, [{"id": "g", "action": action, "table": "t"},
                          {"id": "l", "action": "read_rows", "table": "t"}])
    assert rendus[0] == {"id": "g", "ok": False, "status": 400, "code": "batch_reads_only",
                         "detail": rendus[0]["detail"]}
    assert rendus[1]["ok"] is True


def test_une_lecture_mal_formee_est_refusee_dans_sa_reponse(monde):
    rendus = _lot(monde, [
        {"id": "champ", "action": "read_rows", "table": "t", "consent": "x"},
        {"id": "action", "action": "lire", "table": "t"},
        {"id": "sans_action", "table": "t"},
        {"id": "options", "action": "read_rows", "table": "t", "options": None},
        {"id": "bon", "action": "read_rows", "table": "t", "options": {"limit": 1}},
    ])
    assert [r.get("code") for r in rendus] == ["unknown_fields", "invalid_input",
                                               "invalid_input", "invalid_input", None]
    assert rendus[-1]["ok"] is True and len(rendus[-1]["rows"]) == 1


@pytest.mark.parametrize("reads, kw, code", [
    ([{"id": str(i), "action": "read_rows", "table": "t"} for i in range(11)], {},
     "batch_too_large"),
    ([], {}, "invalid_batch"),
    (None, {}, "invalid_batch"),
    ([{"action": "read_rows", "table": "t"}], {}, "invalid_batch"),
    ([{"id": 1, "action": "read_rows", "table": "t"}], {}, "invalid_batch"),
    ([{"id": "x" * 65, "action": "read_rows", "table": "t"}], {}, "invalid_batch"),
    ([{"id": "a", "action": "read_rows", "table": "t"},
      {"id": "a", "action": "read_aggregate", "table": "t"}], {}, "invalid_batch"),
    ([{"id": "a", "action": "read_rows", "table": "t"}], {"table": "t"}, "invalid_batch"),
    ([{"id": "a", "action": "read_rows", "table": "t"}], {"options": {"limit": 1}},
     "invalid_batch"),
    ([{"id": "a", "action": "read_rows", "table": "t"}], {"consent": "x"}, "invalid_batch"),
])
def test_la_forme_du_lot_est_bornee(monde, reads, kw, code):
    with pytest.raises(AuthzDenied) as e:
        C._call(_ctx(monde), C.AppCallInput(app_id=monde["app"], action="batch", reads=reads,
                                            **kw))
    assert (e.value.status, e.value.code) == (400, code)


def test_dix_lectures_passent(monde):
    rendus = _lot(monde, [{"id": str(i), "action": "read_rows", "table": "t",
                           "options": {"limit": 1, "offset": i}} for i in range(10)])
    assert all(r["ok"] and len(r["rows"]) == 1 for r in rendus)
    assert len({r["rows"][0]["_id"] for r in rendus}) == 10


def test_reads_hors_d_un_lot_est_refuse(monde):
    with pytest.raises(AuthzDenied) as e:
        C._call(_ctx(monde), C.AppCallInput(app_id=monde["app"], action="read_rows",
                                            table="t", reads=[]))
    assert e.value.code == "invalid_batch"


def test_ce_qui_se_juge_pour_tout_le_lot_refuse_tout_le_lot(monde):
    lecture = [{"id": "a", "action": "read_rows", "table": "t"}]
    with pytest.raises(AuthzDenied) as e:
        C._call(_ctx(monde), C.AppCallInput(app_id=10 ** 9, action="batch", reads=lecture))
    assert e.value.code == "unknown_app"
    with pytest.raises(AuthzDenied) as e:
        _lot(monde, lecture, draft=True)
    # Pas de brouillon : refusé pour tout le lot, comme pour chaque appel.
    assert e.value.code == "no_draft"


def test_un_lot_n_a_pas_de_consentement(monde, monkeypatch):
    from oto_mcp import client_trace, config, geste
    monkeypatch.setattr(config, "cors_origins", lambda: {"https://hote.test"})
    jeton_trace = client_trace.set_current(ip="127.0.0.1", user_agent="t",
                                           origin="https://hote.test", fetch_dest="empty")
    jeton_geste = geste.poser(geste.CONSOLE, monde["lecteur"])
    try:
        with pytest.raises(AuthzDenied) as e:
            C._consent(ResolvedCtx(sub=monde["lecteur"], org_id=monde["org"], channel="rest"),
                       C.AppCallInput(app_id=monde["app"], action="batch",
                                      reads=[{"id": "a", "action": "read_rows", "table": "t"}]))
        # Un geste ne voyage pas avec des lectures : refusé, pas ignoré.
        with pytest.raises(AuthzDenied) as e2:
            C._consent(ResolvedCtx(sub=monde["lecteur"], org_id=monde["org"], channel="rest"),
                       C.AppCallInput(app_id=monde["app"], action="add_row", table="t",
                                      values={"nom": "x"}, reads=[]))
    finally:
        geste.retirer(jeton_geste)
        client_trace.reset(jeton_trace)
    assert e.value.code == "consent_not_needed"
    assert e2.value.code == "invalid_batch"


def test_un_seul_emprunt_au_pool_par_lot(monde, monkeypatch):
    from oto_mcp.db import _conn as dbconn
    _lot(monde, LECTURES)                         # caches de processus chauffés
    pool = dbconn._get_pool()
    emprunts = []
    reel = pool.connection
    monkeypatch.setattr(pool, "connection",
                        lambda *a, **k: emprunts.append(1) or reel(*a, **k))
    _lot(monde, LECTURES)
    assert len(emprunts) == 1, f"{len(emprunts)} emprunts au pool pour un lot"


def test_le_chemin_d_un_lot_ne_fait_que_lire(monde, monkeypatch):
    vues: list[str] = []
    for classe in (psycopg.Connection, psycopg.Cursor):
        reel = classe.execute

        def _releve(self, query, *a, _reel=reel, **k):
            vues.append(query if isinstance(query, str) else repr(query))
            return _reel(self, query, *a, **k)
        monkeypatch.setattr(classe, "execute", _releve)
    _lot(monde, LECTURES)
    assert vues, "le relevé ne voit rien : il ne regarde pas le bon chemin"
    ecritures = [q for q in vues if _ECRITURE.search(q)]
    assert not ecritures, f"écriture sur le chemin d'un lot : {ecritures[:3]}"


def test_l_app_est_jugee_une_fois_et_chaque_lecture_juge_son_tableau(monde, monkeypatch):
    vus = {"app": 0, "table": []}
    app_pour, table = C._app_pour, C._table
    monkeypatch.setattr(C, "_app_pour", lambda *a, **k: vus.__setitem__(
        "app", vus["app"] + 1) or app_pour(*a, **k))
    monkeypatch.setattr(C, "_table", lambda app, alias, *a: vus["table"].append(alias)
                        or table(app, alias, *a))
    rendus = _lot(monde, [{"id": str(i), "action": a, "table": tb}
                          for i, (a, tb) in enumerate([("read_rows", "t"), ("read_aggregate", "t"),
                                                       ("read_rows", "absente"),
                                                       ("read_rows", "absente"),
                                                       ("read_rows", "t")])])
    assert vus["app"] == 1
    # Aucun jugement de tableau partagé entre lectures : chacune passe les contrôles
    # d'un appel seul (le tableau, son org, l'auteur), même deux fois le même tableau.
    assert vus["table"] == ["t", "t", "absente", "absente", "t"]
    assert [r["ok"] for r in rendus] == [True, True, False, False, True]
    assert rendus[2]["code"] == rendus[3]["code"] == "undeclared_table"


def test_une_lecture_coupee_par_la_borne_sql_ne_coule_pas_le_lot(monde, monkeypatch):
    """`statement_timeout` coupe une lecture (`QueryCanceled`) : SA réponse est un 503
    `read_timeout`, et les lectures suivantes sont servies sur le même emprunt."""
    from oto_mcp.capabilities.datastore import rows as R

    def _coupe(*a, **k):
        raise psycopg.errors.QueryCanceled("canceling statement due to statement timeout")
    monkeypatch.setattr(R, "_aggregate", _coupe)
    rendus = _lot(monde, [{"id": "a", "action": "read_aggregate", "table": "t"},
                          {"id": "b", "action": "read_rows", "table": "t",
                           "options": {"limit": 2}}])
    assert rendus[0]["ok"] is False and rendus[0]["status"] == 503
    assert rendus[0]["code"] == C.READ_TIMEOUT
    assert rendus[1]["ok"] is True and len(rendus[1]["rows"]) == 2


# ── Le débit : par la vraie garde et la vraie route ─────────────────────────────

class _Verifier:
    def __init__(self, sub):
        self.sub = sub

    async def verify_token(self, token):
        return types.SimpleNamespace(claims={"sub": self.sub})


@pytest.fixture
def rest(monde):
    from oto_mcp.api import routes as api_routes
    from oto_mcp.api.routes_lourdes import GardeRoutesLourdes, RouteLourde
    route = RouteLourde("POST", "/api/me/apps/{app_id}/call", par_minute=12, concurrence=8,
                        pourquoi="banc")
    garde = GardeRoutesLourdes(Starlette(routes=list(api_routes.make_routes(
        _Verifier(monde["lecteur"])))), routes=(route,))
    c = TestClient(garde)
    c.headers.update({"Authorization": "Bearer jwt-de-test"})
    c.garde, c.chemin = garde, f"/api/me/apps/{monde['app']}/call"
    return c


def _debite(c) -> int:
    return sum(len(v) for v in c.garde._c.appels.values())


def test_un_lot_de_n_lectures_coute_n_appels_au_debit(rest):
    dix = [{"id": str(i), "action": "read_rows", "table": "t", "options": {"limit": 1}}
           for i in range(10)]
    r = rest.post(rest.chemin, json={"action": "batch", "reads": dix})
    assert r.status_code == 200, r.text
    assert len(r.json()["results"]) == 10 and all(x["ok"] for x in r.json()["results"])
    assert _debite(rest) == 10
    # 10 + 3 > 12 : refusé ENTIER, rien de plus n'est débité.
    r = rest.post(rest.chemin, json={"action": "batch", "reads": dix[:3]})
    assert r.status_code == 429, r.text
    assert r.json()["error"] == "rate_limited"
    assert 1 <= int(r.headers["retry-after"]) <= 60
    assert r.json()["details"]["limit_per_minute"] == 12
    # La requête refusée par la capacité a passé la garde : un appel, pas plus.
    assert _debite(rest) == 11
    r = rest.post(rest.chemin, json={"action": "read_rows", "table": "t"})
    assert r.status_code == 200, r.text
    assert _debite(rest) == 12
    # Puis la garde elle-même refuse, avant toute lecture.
    r = rest.post(rest.chemin, json={"action": "read_rows", "table": "t"})
    assert r.status_code == 429 and r.json()["error"] == "rate_limited"


def test_un_lot_par_la_route_rend_ce_que_rendent_ses_appels(rest):
    lot = rest.post(rest.chemin, json={"action": "batch", "reads": LECTURES[:6]})
    assert lot.status_code == 200, lot.text
    for lu, read in zip(lot.json()["results"], LECTURES[:6]):
        seul = rest.post(rest.chemin, json={k: read[k] for k in ("action", "table", "options")
                                            if k in read})
        if lu["ok"]:
            assert seul.status_code == 200
            assert {k: v for k, v in lu.items() if k not in ("id", "ok")} == {
                k: v for k, v in seul.json().items() if v is not None}
        else:
            assert (lu["status"], lu["code"], lu["detail"]) == (
                seul.status_code, seul.json()["error"], seul.json().get("detail"))


def test_hors_d_une_route_lourde_rien_n_est_debite():
    from oto_mcp.api import routes_lourdes
    assert routes_lourdes.debiter_en_plus(9) is None


def test_le_debit_d_un_lot_traverse_les_gardes_du_vrai_montage():
    """Sur le serveur, `ViewAs` et `Subdomain` s'intercalent entre la garde et le
    handler (`server.build_root_app`), et la capacité tourne dans un thread : la
    fenêtre posée par la garde doit y parvenir, sinon un lot ne coûterait qu'un appel."""
    from starlette.concurrency import run_in_threadpool
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from oto_mcp import subdomain_org
    from oto_mcp.api import routes as api_routes
    from oto_mcp.api import routes_lourdes as rl

    async def _handler(request):
        refus = await run_in_threadpool(rl.debiter_en_plus, 4)
        return JSONResponse({"refus": refus})

    route = rl.RouteLourde("POST", "/api/me/apps/{app_id}/call", par_minute=8,
                           concurrence=8, pourquoi="banc")
    interne = Starlette(routes=[Route("/api/me/apps/{app_id}/call", _handler,
                                      methods=["POST"])])
    pile = subdomain_org.SubdomainOrgMiddleware(
        api_routes.ViewAsMiddleware(interne, verifier=_Verifier("x")))
    garde = rl.GardeRoutesLourdes(pile, routes=(route,))
    c = TestClient(garde)
    c.headers.update({"Authorization": "Bearer jwt-de-test"})
    assert c.post("/api/me/apps/1/call").json() == {"refus": None}
    assert sum(len(v) for v in garde._c.appels.values()) == 5
    refus = c.post("/api/me/apps/1/call").json()["refus"]
    assert refus is not None and refus[1] == 8
    assert sum(len(v) for v in garde._c.appels.values()) == 6
