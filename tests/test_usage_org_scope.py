"""Anti-fuite cross-org des projections d'usage.

`list_tool_calls(org_id=…)` doit ajouter un filtre `l.org_id = %s` — sinon un user
voit ses appels de TOUTES ses orgs sous l'org chargée (même classe que e030f5c, mais
sur tool_calls). On mocke `_connect` pour capturer le SQL/params sans vraie DB.

Depuis la descente des lentilles au niveau org (`oto_org_monitoring`), la même
exigence porte sur TOUTES les projections rendues à un org_admin : chacune doit
produire sa clause de scope, et la produire **paramétrée** (jamais interpolée). Une
clause oubliée ne casse rien de visible — elle affiche des chiffres de la plateforme
sous l'écran d'une org. D'où un test par projection.
"""
from oto_mcp.db import usage


class _FakeCur:
    def __init__(self, sink):
        self._sink = sink

    def fetchall(self):
        return []


class _FakeConn:
    def __init__(self, sink):
        self._sink = sink

    def execute(self, sql, params):
        self._sink["sql"] = sql
        self._sink["params"] = params
        return _FakeCur(self._sink)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _wire(monkeypatch):
    sink: dict = {}
    monkeypatch.setattr(usage, "_connect", lambda: _FakeConn(sink))
    # La borne de durée se juge ailleurs (test_lecture_bornee) : ici, le SQL.
    monkeypatch.setattr(usage, "_agregat", lambda objet, **kw: usage._connect())
    _decoupe_fixe(monkeypatch)
    return sink


def _decoupe_fixe(monkeypatch):
    """Les lentilles lues sur les totaux par jour (#1147) découpent leur fenêtre sur la
    base ; ici, un découpage FIXE qui porte des jours consolidés ET du direct, pour que
    le SQL des deux faces (totaux `t.`, journal `l.`) soit écrit et lu."""
    from oto_mcp.db import journal_jour

    def decouper(conn, objet, *, prefixe, **fenetre):
        return journal_jour.Decoupe(
            "2026-10-01", "2026-10-07",
            f"(now() - make_interval(days => %({prefixe}_jours)s))", None, False,
            {f"{prefixe}_jours": fenetre.get("jours"), f"{prefixe}_a0": "2026-10-01",
             f"{prefixe}_a1": "2026-10-07"})
    monkeypatch.setattr(journal_jour, "decouper", decouper)


def test_org_id_adds_scope_clause(monkeypatch):
    sink = _wire(monkeypatch)
    usage.list_tool_calls(sub="u1", org_id=35, limit=50)
    assert "l.org_id = %s" in sink["sql"]
    # sub puis org_id puis limit (ordre d'append des clauses)
    assert sink["params"][0] == "u1"
    assert 35 in sink["params"]


def test_without_org_id_no_scope_clause(monkeypatch):
    sink = _wire(monkeypatch)
    usage.list_tool_calls(sub="u1", limit=50)
    # rétro-compat : admin monitoring non scopé — pas de FILTRE org (la colonne
    # projetée `l.org_id` dans le SELECT est un axe de corrélation, pas un scope).
    assert "l.org_id = %s" not in sink["sql"]
    assert tuple(sink["params"]) == ("u1", 50)


# ── lentilles descendues au niveau org (oto_org_monitoring) ─────────────────
#
# Chaque cas : SANS org_id la projection reste plateforme-wide (contrat admin
# inchangé) ; AVEC, elle porte sa clause de scope et 35 est un PARAMÈTRE lié.

def test_connector_failure_stats_scope(monkeypatch):
    sink = _wire(monkeypatch)
    usage.connector_failure_stats(since_days=7)
    assert "org_id = %(jj_org_id)s" not in sink["sql"]
    usage.connector_failure_stats(since_days=7, org_id=35)
    # Le scope sur les DEUX faces : les totaux des jours consolidés et le journal direct.
    assert "t.org_id = %(jj_org_id)s" in sink["sql"]
    assert "l.org_id = %(jj_org_id)s" in sink["sql"]
    assert sink["params"]["jj_org_id"] == 35


def test_list_runs_scope(monkeypatch):
    sink = _wire(monkeypatch)
    usage.list_runs(100)
    assert "d.org_id = %s" not in sink["sql"]
    assert tuple(sink["params"]) == (100, 100)
    usage.list_runs(100, org_id=35)
    assert "d.org_id = %s" in sink["sql"]
    # org_id AVANT les deux limit (le choix de la page, puis le SELECT) — une
    # inversion ferait un LIMIT 35.
    assert tuple(sink["params"]) == (35, 100, 100)


def test_list_runs_ne_groupe_plus_tout_le_journal(monkeypatch):
    """infra#9 : la page se choisit parmi les OUVERTURES avant toute reconstruction, et
    le compte d'appels est un LATERAL par run de la page — plus un `GROUP BY run_id` de
    tout le journal, toutes orgs confondues, à chaque affichage de la liste."""
    sink = _wire(monkeypatch)
    usage.list_runs(50, org_id=35)
    sql = " ".join(sink["sql"].split())
    assert "GROUP BY" not in sql
    assert ("s.id IN (SELECT d.id FROM tool_calls d WHERE d.tool = 'run_start' "
            "AND d.run_id IS NOT NULL AND d.org_id = %s "
            "ORDER BY d.created_at DESC, d.id DESC LIMIT %s)") in sql
    assert "WHERE t.run_id = j.run_id" in sql


def test_get_run_scope(monkeypatch):
    sink = _wire(monkeypatch)
    usage.get_run("r1")
    assert "org_id = %s" not in sink["sql"]
    assert tuple(sink["params"]) == ("r1",)
    usage.get_run("r1", org_id=35)
    assert "org_id = %s" in sink["sql"]
    assert tuple(sink["params"]) == ("r1", 35)


def test_signal_aggregates_scope(monkeypatch):
    sink = _wire(monkeypatch)
    for fn, signal in ((usage.aggregate_gaps, "gap"),
                       (usage.aggregate_tool_feedback, "tool_feedback")):
        fn(30)
        assert "s.org_id = %s" not in sink["sql"]
        # le signal reste LIÉ, jamais interpolé dans le SQL
        assert f"'{signal}'" not in sink["sql"]
        assert tuple(sink["params"]) == (signal, 30)
        fn(30, org_id=35)
        assert "s.org_id = %s" in sink["sql"]
        assert tuple(sink["params"]) == (signal, 30, 35)


def test_org_adoption_starts_from_members_not_from_calls(monkeypatch):
    """L'ordre des tables EST le contrat : partir d'`org_members` est ce qui rend un
    membre à 0 appel visible. Partir de `tool_calls` le ferait disparaître — or c'est
    précisément lui que l'org_admin cherche."""
    sink = _wire(monkeypatch)
    out = usage.org_adoption(35, active_window_days=30)
    sql = sink["sql"]
    assert "FROM org_members m" in sql and "LEFT JOIN a ON a.sub = m.sub" in sql
    # L'activité raccrochée SOUS cette org, sur les deux sources (fenêtre, historique)
    # et les deux faces de chacune (totaux, journal direct).
    for pref in ("fen", "tout"):
        assert f"t.org_id = %({pref}_org_id)s" in sql
        assert f"l.org_id = %({pref}_org_id)s" in sql
        assert sink["params"][f"{pref}_org_id"] == 35
    assert sink["params"]["fen_jours"] == 30 and sink["params"]["org_id"] == 35
    # aucune ligne → une org sans membre rend des compteurs à 0, pas une erreur
    assert out["total_members"] == 0 and out["active"] == 0 and out["truncated"] is False


def test_org_adoption_counts_cover_the_whole_population(monkeypatch):
    """La liste nominative est plafonnée ; les COMPTEURS, non. Un plafond qui rognerait
    aussi les agrégats afficherait « 500 membres » à une org qui en a 900."""
    monkeypatch.setattr(usage, "_ADOPTION_LIST_CAP", 2)
    rows = [{"sub": f"u{i}", "calls": i % 2, "connector_failures": 1 if i == 0 else 0}
            for i in range(5)]
    monkeypatch.setattr(usage, "_connect", lambda: _FakeConnRows(rows))
    # La borne de durée se juge ailleurs (test_lecture_bornee) : ici, le SQL.
    monkeypatch.setattr(usage, "_agregat", lambda objet, **kw: usage._connect())
    _decoupe_fixe(monkeypatch)
    out = usage.org_adoption(35)
    assert out["total_members"] == 5
    assert out["active"] == 2 and out["never_active"] == 3
    assert out["blocked_by_connector"] == 1
    assert out["truncated"] is True and len(out["members"]) == 2


class _FakeConnRows:
    """Connexion factice qui rend des lignes (≠ `_FakeConn`, qui ne capture que le SQL)."""

    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql, params):
        return type("_Cur", (), {"fetchall": lambda _self: self._rows})()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False
