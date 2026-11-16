"""Lentille REST scopable (#451) — `db.rest_call_stats`, au ras du SQL.

La console passait `sub`/`org_id` à un `WindowInput(days=…)` : le filtre mourait
avant la requête, et la réponse — celle de TOUTE la plateforme — était indiscernable
de celle d'un compte. Le SQL de la lentille se construit maintenant par morceaux ;
deux requêtes partagent la clause, donc chacune doit porter ses paramètres, et
l'arithmétique placeholders/params est la seule preuve locale qu'aucune n'en a un de
trop (l'erreur ne se voit sinon qu'en prod, sur un `%s` orphelin).

Depuis #1455, totaux, routes et statuts sortent d'UNE requête qui lit la fenêtre une
seule fois (`WITH f`) : un filtre ne peut plus s'appliquer à l'un sans l'autre, par
construction.
"""
from oto_mcp.db import usage


class _Cur:
    def fetchall(self):
        return []


class _Conn:
    """Enregistre CHAQUE requête de la lentille (elle n'en fait qu'une)."""

    def __init__(self, sink):
        self.sink = sink

    def execute(self, sql, params):
        self.sink.append((sql, params))
        return _Cur()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _run(monkeypatch, **kw):
    vues: list = []
    monkeypatch.setattr(usage, "_connect", lambda: _Conn(vues))
    # La borne de durée se juge ailleurs (test_lecture_bornee) : ici, le SQL.
    monkeypatch.setattr(usage, "_agregat", lambda objet, **kw: usage._connect())
    return usage.rest_call_stats(**kw), vues


def test_sans_filtre_la_lentille_reste_plateforme(monkeypatch):
    out, vues = _run(monkeypatch, since_days=7)
    assert len(vues) == 1
    for sql, _ in vues:
        assert "sub = %s" not in sql and "org_id = %s" not in sql
    # Rien n'a été restreint : rien à annoncer (le contrat du dashboard ne bouge pas).
    assert "filters" not in out and "org_id_caveat" not in out


def test_la_requete_unique_porte_le_filtre_et_ses_parametres(monkeypatch):
    """Les totaux, la ventilation par route et celle par statut sortent d'UNE requête
    (#1455) : le filtre posé s'applique aux trois ventilations à la fois."""
    out, vues = _run(monkeypatch, since_days=30, org_id=42, sub="sub-jane")

    assert len(vues) == 1
    assert vues[0][0].count("FROM tool_calls") == 1     # UNE lecture du journal
    for sql, params in vues:
        assert "org_id = %s" in sql and "sub = %s" in sql
        assert sql.count("%s") == len(params), f"placeholders ≠ params : {params}"
        assert list(params) == [30, 42, "sub-jane"]   # l'ordre est celui des clauses
    assert out["filters"] == {"org_id": 42, "sub": "sub-jane"}


def test_route_ajoute_un_prefixe_like_a_la_requete(monkeypatch):
    """oto-dashboard#125 : `by_route` est plafonné à `LIMIT 100` — une route à faible
    volume peut ne jamais y apparaître sans que rien ne le dise. `route` donne un
    compte EXACT (pas de limite) en ajoutant un `LIKE route || '%'` aux TROIS requêtes,
    comme `org_id`/`sub` — sinon totaux et ventilation divergeraient."""
    out, vues = _run(monkeypatch, since_days=30, route="/api/atlassian/oauth/start")

    assert len(vues) == 1
    for sql, params in vues:
        assert "tool LIKE %s" in sql
        assert sql.count("%s") == len(params), f"placeholders ≠ params : {params}"
        assert list(params) == [30, "/api/atlassian/oauth/start%"]
    assert out["filters"] == {"route": "/api/atlassian/oauth/start"}


def test_route_est_un_prefixe_pas_une_egalite():
    """Une valeur complète reste un préfixe d'elle-même : même mécanique pour
    « donne-moi CETTE route » et « donne-moi toutes les routes sous CE segment »."""
    import inspect
    src = inspect.getsource(usage.rest_call_stats)
    assert 'f"{route}%"' in src, "le motif LIKE doit rester un préfixe, pas une égalité"


def test_last_call_at_vient_du_groupe_des_totaux(monkeypatch):
    """oto-dashboard#125 : « appels/erreurs/DERNIER appel » pour une route — le champ
    sort du groupe des TOTAUX (ensemble vide), pas d'un groupe de route ou de statut."""
    class _CurAvecDate:
        def fetchall(self):
            return [
                _g("total", calls=3, errors=1, users=2, last_call_at="2026-09-01T10:00:00"),
                _g("route", route="GET /api/x", calls=3, errors=1),
            ]

    class _ConnAvecDate:
        def execute(self, sql, params):
            assert "MAX(created_at) AS last_call_at" in sql
            return _CurAvecDate()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(usage, "_connect", lambda: _ConnAvecDate())
    # La borne de durée se juge ailleurs (test_lecture_bornee) : ici, le SQL.
    monkeypatch.setattr(usage, "_agregat", lambda objet, **kw: usage._connect())
    out = usage.rest_call_stats(since_days=7)
    assert out["last_call_at"] == "2026-09-01T10:00:00"
    assert (out["total_calls"], out["error_count"], out["active_users"]) == (3, 1, 2)
    assert out["by_route"] == [{"route": "GET /api/x", "calls": 3, "errors": 1,
                                "avg_ms": None, "p95_ms": None}]


def _g(groupe, *, route=None, error=None, calls=0, errors=0, users=None,
       last_call_at=None):
    """Une ligne de la requête unique : son `groupe` (total, route, statut)."""
    return {"groupe": groupe, "route": route, "error": error,
            "calls": calls, "errors": errors, "users": users,
            "last_call_at": last_call_at, "avg_ms": None, "p95_ms": None}


def _run_statuts(monkeypatch, lignes):
    """La lentille avec une ventilation d'erreurs donnée — rend la sortie et le SQL."""
    vues: list = []
    groupes = [_g("statut", error=l["error"], calls=l["calls"], errors=l["calls"])
               for l in lignes]

    class _CurStatuts(_Cur):
        def fetchall(self):
            return groupes

    class _ConnStatuts(_Conn):
        def execute(self, sql, params):
            self.sink.append((sql, params))
            return _CurStatuts()

    monkeypatch.setattr(usage, "_connect", lambda: _ConnStatuts(vues))
    # La borne de durée se juge ailleurs (test_lecture_bornee) : ici, le SQL.
    monkeypatch.setattr(usage, "_agregat", lambda objet, **kw: usage._connect())
    return usage.rest_call_stats(since_days=7, route="POST /api/auth/token"), vues


def test_by_status_distingue_les_4xx_des_5xx(monkeypatch):
    """oto#179 : « ce 500 a-t-il mordu ? » — `error_count` mêle 4xx attendus et
    pannes. Le code HTTP est déjà en base (`error = 'HTTP <code>'`)."""
    out, vues = _run_statuts(monkeypatch, [
        {"error": "HTTP 403", "calls": 40},
        {"error": "HTTP 500", "calls": 3},
        {"error": None, "calls": 1},
    ])
    assert out["by_status"] == [{"status": 403, "calls": 40},
                                {"status": 500, "calls": 3},
                                {"status": None, "calls": 1}]
    (sql, params), = vues
    assert "WHERE NOT ok GROUP BY error" in sql and "tool LIKE %s" in sql
    assert list(params) == [7, "POST /api/auth/token%"]


def test_by_status_leve_sur_une_erreur_hors_contrat(monkeypatch):
    """Une ligne qui ne dit pas `HTTP <code>` n'est rangée nulle part en silence."""
    import pytest
    with pytest.raises(ValueError, match="hors contrat"):
        _run_statuts(monkeypatch, [{"error": "boom", "calls": 1}])


def test_le_scope_par_org_dit_ce_qu_il_laisse_dehors(monkeypatch):
    """`tool_calls.org_id` d'une ligne REST vient d'un EN-TÊTE de consultation
    (best-effort, `RestCallLogger`), pas d'une résolution : une requête sans cet
    en-tête ne porte aucune org et sort du filtre. Sans ce mot, on remplace une
    lecture trop large par un zéro trop étroit — le même défaut, à l'envers."""
    out, _ = _run(monkeypatch, since_days=7, org_id=42)
    assert "en-tête" in out["org_id_caveat"] and "0" in out["org_id_caveat"]

    out, _ = _run(monkeypatch, since_days=7, sub="sub-jane")
    assert "org_id_caveat" not in out          # rien à nuancer sans scope d'org


class _CurLigne:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _ConnLigne:
    """Comme `_Conn`, mais pour `list_rest_calls` : une seule requête, ligne par
    ligne — capture le SQL/params et rend des lignes fixtures."""

    def __init__(self, sink, rows):
        self.sink = sink
        self.rows = rows

    def execute(self, sql, params):
        self.sink.append((sql, params))
        return _CurLigne(self.rows)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_list_rest_calls_sert_view_as_sub_sans_le_deviner(monkeypatch):
    """oto-backend#962 — le pendant `list_tool_calls` pour REST : sert `view_as_sub`
    ligne par ligne, seule lentille qui le peut (`rest_call_stats` n'agrège que)."""
    vues: list = []
    rows = [{"id": 1, "sub": "op-sub", "email": "op@example.com",
             "route": "GET /api/orgs/1", "called_at": "2026-09-18T10:00:00",
             "duration_ms": 42, "ok": True, "error": None, "org_id": 1,
             "view_as_sub": "sub-cible"},
            {"id": 2, "sub": "op-sub", "email": "op@example.com",
             "route": "GET /api/me", "called_at": "2026-09-18T09:00:00",
             "duration_ms": 12, "ok": True, "error": None, "org_id": None,
             "view_as_sub": None}]
    monkeypatch.setattr(usage, "_connect", lambda: _ConnLigne(vues, rows))
    # La borne de durée se juge ailleurs (test_lecture_bornee) : ici, le SQL.
    monkeypatch.setattr(usage, "_agregat", lambda objet, **kw: usage._connect())
    out = usage.list_rest_calls(org_id=1, route="GET /api/orgs")
    assert len(vues) == 1                      # une seule requête, pas deux
    sql, params = vues[0]
    assert "l.view_as_sub" in sql
    assert "l.org_id = %s" in sql and "l.tool LIKE %s" in sql
    assert 1 in params and "GET /api/orgs%" in params
    assert out[0]["view_as_sub"] == "sub-cible"
    assert out[1]["view_as_sub"] is None       # absent au journal = None, jamais deviné


def test_list_rest_calls_plafonne_limit_et_days(monkeypatch):
    vues: list = []
    monkeypatch.setattr(usage, "_connect", lambda: _ConnLigne(vues, []))
    # La borne de durée se juge ailleurs (test_lecture_bornee) : ici, le SQL.
    monkeypatch.setattr(usage, "_agregat", lambda objet, **kw: usage._connect())
    usage.list_rest_calls(limit=99999, days=9999)
    sql, params = vues[0]
    assert params[-1] == 200                   # limit plafonné, même borne que la console
    assert params[0] == 365                    # days plafonné, même borne que rest_call_stats


def test_list_rest_calls_filtre_pour_de_vrai(live):
    """Le filtre `route` contre le VRAI journal : `tool` vaut `MÉTHODE /route`, donc le
    préfixe porte la méthode. Un stub qui capture le SQL ne prouve pas qu'une ligne
    passe ou non — ici, une route voisine, une autre org et un geste sémantique
    (`data_write`, même `kind`) doivent rester dehors."""
    for tool, org in (("GET /api/orgs/:id", 1), ("GET /api/orgs/:id", 2),
                      ("GET /api/me", 1), ("data_write", 1)):
        usage.insert_tool_call({"kind": "rest", "tool": tool, "sub": "op-sub",
                                "org_id": org, "ok": True})
    lignes = usage.list_rest_calls(org_id=1, route="GET /api/orgs")
    assert [(r["route"], r["org_id"]) for r in lignes] == [("GET /api/orgs/:id", 1)]
    # Sans filtre : les deux routes de l'org, jamais le geste sémantique.
    assert sorted(r["route"] for r in usage.list_rest_calls(org_id=1)) == [
        "GET /api/me", "GET /api/orgs/:id"]
    # La route SANS méthode n'est le préfixe de rien : zéro, pas « tout ».
    assert usage.list_rest_calls(route="/api/orgs") == []


def _ref_rest_stats(days):
    """La lentille telle qu'elle lisait le journal en TROIS requêtes, avant #1455."""
    w = (f"kind = 'rest' AND {usage._REST_ROUTE_SHAPE} "
         "AND created_at >= NOW() - make_interval(days => %s)")
    with usage._connect() as conn:
        t = conn.execute(f"SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE NOT ok) AS errors, "
                         f"COUNT(DISTINCT sub) AS users, MAX(created_at) AS last_call_at "
                         f"FROM tool_calls WHERE {w}", (days,)).fetchone()
        routes = conn.execute(
            f"""SELECT tool AS route, COUNT(*) AS calls,
                       COUNT(*) FILTER (WHERE NOT ok) AS errors,
                       ROUND(AVG(duration_ms))::int AS avg_ms,
                       ROUND(percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms))::int
                           AS p95_ms
                  FROM tool_calls WHERE {w} GROUP BY tool""", (days,)).fetchall()
        erreurs = conn.execute(f"SELECT error, COUNT(*) AS calls FROM tool_calls "
                               f"WHERE {w} AND NOT ok GROUP BY error", (days,)).fetchall()
    return t, routes, erreurs


def test_la_requete_unique_rend_ce_que_rendaient_les_trois(live):
    """#1455, contre le VRAI journal : une lecture de la fenêtre — la même réponse que
    les trois requêtes d'avant, y compris l'échec sans statut (NULL), distinct des
    succès dont `error` est aussi NULL, et le compte distinct sans le sub NULL."""
    lignes = [("GET /api/orgs/:id", "s1", True, None, 120),
              ("GET /api/orgs/:id", "s2", False, "HTTP 403", 80),
              ("GET /api/orgs/:id", "s2", True, None, None),
              ("POST /api/files", "s1", False, "HTTP 500", 900),
              ("POST /api/files", None, False, None, 40),
              ("GET /api/me", "s3", True, None, 10),
              ("data_write", "s1", False, "boom", 5)]   # geste sémantique : dehors
    for tool, sub, ok, err, ms in lignes:
        usage.insert_tool_call({"kind": "rest", "tool": tool, "sub": sub, "ok": ok,
                                "error": err, "duration_ms": ms})
    out = usage.rest_call_stats(since_days=7)
    t, routes, erreurs = _ref_rest_stats(7)
    assert (out["total_calls"], out["error_count"], out["active_users"],
            out["last_call_at"]) == (t["total"], t["errors"], t["users"], t["last_call_at"])
    assert sorted(out["by_route"], key=lambda r: r["route"]) == sorted(
        (dict(r) for r in routes), key=lambda r: r["route"])
    assert sorted(out["by_status"], key=lambda r: (r["status"] or 0)) == sorted(
        ({"status": usage._rest_status(r["error"]), "calls": r["calls"]} for r in erreurs),
        key=lambda r: (r["status"] or 0))
    assert {r["status"] for r in out["by_status"]} == {403, 500, None}
