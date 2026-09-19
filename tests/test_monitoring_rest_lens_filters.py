"""Lentille REST scopable (#451) — `db.rest_call_stats`, au ras du SQL.

La console passait `sub`/`org_id` à un `WindowInput(days=…)` : le filtre mourait
avant la requête, et la réponse — celle de TOUTE la plateforme — était indiscernable
de celle d'un compte. Le SQL de la lentille se construit maintenant par morceaux ;
deux requêtes partagent la clause, donc chacune doit porter ses paramètres, et
l'arithmétique placeholders/params est la seule preuve locale qu'aucune n'en a un de
trop (l'erreur ne se voit sinon qu'en prod, sur un `%s` orphelin).
"""
from oto_mcp.db import usage


class _Cur:
    def fetchone(self):
        return {"total": 0, "errors": 0, "users": 0}

    def fetchall(self):
        return []


class _Conn:
    """Enregistre CHAQUE requête de la lentille (elle en fait deux)."""

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
    return usage.rest_call_stats(**kw), vues


def test_sans_filtre_la_lentille_reste_plateforme(monkeypatch):
    out, vues = _run(monkeypatch, since_days=7)
    assert len(vues) == 2
    for sql, _ in vues:
        assert "sub = %s" not in sql and "org_id = %s" not in sql
    # Rien n'a été restreint : rien à annoncer (le contrat du dashboard ne bouge pas).
    assert "filters" not in out and "org_id_caveat" not in out


def test_les_deux_requetes_portent_le_filtre_et_ses_parametres(monkeypatch):
    """Les totaux et la ventilation par route sont DEUX requêtes : un filtre posé
    sur une seule rendrait un total d'un compte et des routes de la plateforme —
    incohérence muette, plus trompeuse que l'absence de filtre."""
    out, vues = _run(monkeypatch, since_days=30, org_id=42, sub="sub-jane")

    assert len(vues) == 2
    for sql, params in vues:
        assert "org_id = %s" in sql and "sub = %s" in sql
        assert sql.count("%s") == len(params), f"placeholders ≠ params : {params}"
        assert list(params) == [30, 42, "sub-jane"]   # l'ordre est celui des clauses
    assert out["filters"] == {"org_id": 42, "sub": "sub-jane"}


def test_route_ajoute_un_prefixe_like_aux_deux_requetes(monkeypatch):
    """oto-dashboard#125 : `by_route` est plafonné à `LIMIT 100` — une route à faible
    volume peut ne jamais y apparaître sans que rien ne le dise. `route` donne un
    compte EXACT (pas de limite) en ajoutant un `LIKE route || '%'` aux DEUX requêtes,
    comme `org_id`/`sub` — sinon totaux et ventilation divergeraient."""
    out, vues = _run(monkeypatch, since_days=30, route="/api/atlassian/oauth/start")

    assert len(vues) == 2
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


def test_last_call_at_vient_du_max_de_la_requete_de_totaux(monkeypatch):
    """oto-dashboard#125 : « appels/erreurs/DERNIER appel » pour une route — le champ
    doit sortir de `totals`, pas être recalculé ailleurs (une deuxième requête pourrait
    diverger de la fenêtre/du filtre de la première)."""
    class _CurAvecDate:
        def fetchone(self):
            return {"total": 3, "errors": 1, "users": 2, "last_call_at": "2026-09-01T10:00:00"}

        def fetchall(self):
            return []

    class _ConnAvecDate:
        def execute(self, sql, params):
            if "GROUP BY tool" not in sql:      # la requête de TOTAUX, pas by_route
                assert "MAX(created_at) AS last_call_at" in sql
            return _CurAvecDate()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(usage, "_connect", lambda: _ConnAvecDate())
    out = usage.rest_call_stats(since_days=7)
    assert out["last_call_at"] == "2026-09-01T10:00:00"


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
    out = usage.list_rest_calls(org_id=1, route="/api/orgs")
    assert len(vues) == 1                      # une seule requête, pas deux
    sql, params = vues[0]
    assert "l.view_as_sub" in sql
    assert "l.org_id = %s" in sql and "l.tool LIKE %s" in sql
    assert 1 in params and "/api/orgs%" in params
    assert out[0]["view_as_sub"] == "sub-cible"
    assert out[1]["view_as_sub"] is None       # absent au journal = None, jamais deviné


def test_list_rest_calls_plafonne_limit_et_days(monkeypatch):
    vues: list = []
    monkeypatch.setattr(usage, "_connect", lambda: _ConnLigne(vues, []))
    usage.list_rest_calls(limit=99999, days=9999)
    sql, params = vues[0]
    assert params[-1] == 1000                  # limit plafonné, même borne que list_tool_calls
    assert params[0] == 365                    # days plafonné, même borne que rest_call_stats
