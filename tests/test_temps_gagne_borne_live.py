"""Le temps gagné de la page Agents (`runner.jobs op=summary`), compté PAR LA BASE.

Le 13/11/2026, `op=summary` décodait en Python chaque travail conclu de la fenêtre et
chaque appel du journal de chacun — des centaines de milliers de lignes sur une org
chargée, 1,4 à 2 s de CPU sous le GIL : toutes les requêtes du processus attendaient.
`db.formes_des_runs_conclus` fait le compte en SQL et ne rend que des FORMES de runs.

Deux preuves, contre un vrai PostgreSQL :
- **parité** : sur un journal qui porte chaque cas des règles (outil préfixé, `op`
  absente, vide ou en capitales, enregistrement revu le même jour ou le lendemain, clé
  vide, appel refusé, deux travaux d'un même run, travail sans run ou sans appel `mcp`,
  autre org, hors fenêtre, travail en échec, clé d'agent `"05"`), le compte de la base
  égale celui de la référence en mémoire (`time_saved.for_agent`), nourrie des lignes
  brutes lues ici ;
- **borne** : ce que Python reçoit — requêtes et lignes — ne grandit pas avec
  l'historique (100 runs ou 2 000, mêmes lignes rendues).
"""
from __future__ import annotations

import json
import random
from contextlib import contextmanager

import pytest

ORG, AUTRE_ORG = 1, 2

#: Un registre d'effets synthétique : ce que les outils servis DÉCLARENT.
EFFETS = {
    "serper_search": "read", "gmail_act": "act", "whatsapp_act": "act",
    "hubspot_object": None, "data_write": None, "data_claim_next": None,
    "oto_doc": None, "web_preview": "simulation",
}


@pytest.fixture
def effets(monkeypatch):
    from oto_mcp import tool_registry
    monkeypatch.setattr(tool_registry, "_EFFETS", dict(EFFETS))


@pytest.fixture
def base(live):
    import psycopg
    from oto_mcp.db._conn import _database_url
    with psycopg.connect(_database_url(), autocommit=True) as c:
        c.execute("DELETE FROM runner_jobs")
        c.execute("DELETE FROM tool_calls")
        c.execute("DELETE FROM runs")
        yield c


def _poser(c, travaux, appels):
    """`travaux` : (org, payload, statut, minutes_avant, run_id) ;
    `appels` : (run_id, outil, args, ok, minutes_avant, kind)."""
    runs = sorted({t[4] for t in travaux if t[4]} | {a[0] for a in appels})
    with c.cursor() as cur:
        cur.executemany("INSERT INTO runs (run_id, label) VALUES (%s, 'banc')",
                        [(r,) for r in runs])
        cur.executemany(
            "INSERT INTO runner_jobs (org_id, kind, status, due_at, attempts, max_attempts, "
            "payload, run_id) VALUES (%s, 'start', %s, NOW() - %s * interval '1 minute', "
            "1, 3, %s::jsonb, %s)",
            [(o, st, m, json.dumps(p), r) for o, p, st, m, r in travaux])
        cur.executemany(
            "INSERT INTO tool_calls (server, kind, tool, args, ok, run_id, org_id, created_at) "
            "VALUES ('oto', %s, %s, %s::jsonb, %s, %s, 1, NOW() - %s * interval '1 minute')",
            [(k, t, json.dumps(a) if a is not None else None, ok, r, m)
             for r, t, a, ok, m, k in appels])


def _reference(agents, jours, minutes):
    """La règle telle que `time_saved.for_agent` la tient, nourrie des lignes BRUTES :
    les travaux conclus de la fenêtre dans l'ordre où ils sont tombés dus, et tous
    leurs appels `mcp` dans l'ordre du journal."""
    from oto_mcp import time_saved
    from oto_mcp.db import runner_jobs as RJ
    from oto_mcp.db._conn import _connect
    cle = "COALESCE(" + ", ".join(f"args->>'{k}'" for k in RJ.CLES_D_ENREGISTREMENT) + ")"
    with _connect() as c:
        travaux = c.execute(
            f"SELECT payload->>'trigger_id' AS t, run_id FROM runner_jobs "
            f"WHERE org_id = %s AND status = 'done' AND {RJ.PREDICAT_DECLENCHEUR} "
            f"AND due_at >= NOW() - make_interval(days => %s) ORDER BY due_at, id",
            (ORG, jours)).fetchall()
        appels = c.execute(
            f"SELECT run_id, tool, args->>'op' AS op, ok, {cle} AS cle, "
            f"to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD') AS jour FROM tool_calls "
            f"WHERE kind = 'mcp' AND run_id IS NOT NULL ORDER BY run_id, created_at, id").fetchall()
    par_run: dict = {}
    for a in appels:
        par_run.setdefault(a["run_id"], []).append(time_saved.Call(
            a["tool"], a["op"], a["ok"], a["cle"], a["jour"]))
    runs: dict = {}
    sans: dict = {}
    for r in travaux:
        t, run_id = r["t"], r["run_id"]
        if int(t) not in agents:
            continue
        if par_run.get(run_id or ""):
            runs.setdefault(int(t), []).append(par_run[run_id])
        else:
            sans[int(t)] = sans.get(int(t), 0) + 1
    return {t: time_saved.for_agent(rs, minutes) for t, rs in runs.items()}, sans


def _par_la_base(agents, jours, minutes):
    from oto_mcp import time_saved
    from oto_mcp.db import runner_jobs as RJ
    formes, sans = RJ.formes_des_runs_conclus(ORG, jours, agents, time_saved.classe)
    par: dict = {}
    for f in formes:
        par.setdefault(f["trigger_id"], []).append(time_saved.Forme(
            f["actions"], f["records"], f["research"], f["runs"]))
    return ({t: time_saved.for_shapes(fs, minutes) for t, fs in par.items()},
            {r["trigger_id"]: r["runs"] for r in sans})


def _servi(res):
    """Ce que la page reçoit : arrondi au dixième, comme `_resume` le sert."""
    total, tally = res
    return round(total, 1), {k: (v["count"], round(v["minutes"], 1)) for k, v in tally.items()}


def _comparer(res_ref, res_base):
    (ref, sans_ref), (base, sans_base) = res_ref, res_base
    assert sans_base == sans_ref
    for t in set(ref) | set(base):
        assert _servi(base.get(t, (0.0, {}))) == _servi(ref.get(t, (0.0, {}))), t


def test_les_cas_des_regles_comptes_par_la_base_egalent_la_reference(base, effets):
    from oto_mcp import time_saved
    j = lambda d: d * 24 * 60  # noqa: E731 — d jours plus tôt, en minutes
    a5, a7 = {"trigger_id": "5"}, {"trigger_id": 7}
    _poser(base, [
        (ORG, a5, "done", j(3), "r1"),
        (ORG, a5, "done", j(3) - 30, "r2"),        # revoit p1 le même jour
        (ORG, a5, "done", j(1), "r3"),             # revoit p1 un autre jour
        (ORG, a5, "done", j(1) - 10, "r1"),        # reprise : même run, second travail
        (ORG, a5, "done", j(1) - 20, None),        # sans run : non compté, dit
        (ORG, a5, "done", j(1) - 25, "r4"),        # run sans appel mcp : idem
        (ORG, a5, "failed", j(1) - 26, "r5"),      # en échec : hors compte
        (ORG, a5, "done", j(40), "r6"),            # hors fenêtre
        (ORG, {"trigger_id": "05"}, "done", j(2), "r7"),   # le même agent, écrit autrement
        (ORG, a7, "done", j(2), "r8"),             # un rapport : chercher puis poster
        (ORG, {"trigger_id": "x"}, "done", j(2), "r9"),    # clé qui n'est pas un agent
        (AUTRE_ORG, a5, "done", j(2), "r10"),      # autre org
        (ORG, {"trigger_id": "9"}, "done", j(2), "r11"),   # agent non demandé
    ], [
        ("r1", "data_write", {"id": "p1"}, True, j(3) - 1, "mcp"),
        ("r1", "acme_data_write", {"id": "p1"}, True, j(3) - 2, "mcp"),  # préfixé : même marque
        ("r1", "data_write", {"id": ""}, True, j(3) - 3, "mcp"),         # clé vide : compte
        ("r1", "data_write", {"id": "p2"}, False, j(3) - 4, "mcp"),      # refusé
        ("r1", "serper_search", {"q": "a"}, True, j(3) - 5, "mcp"),
        ("r1", "gmail_act", {"op": "SEND", "email": "e@x.test"}, True, j(3) - 6, "mcp"),
        ("r1", "gmail_act", {"op": ""}, True, j(3) - 7, "mcp"),          # op vide : action
        ("r1", "gmail_act", None, True, j(3) - 8, "mcp"),                # op absente : action
        ("r1", "gmail_act", {"op": "archive"}, True, j(3) - 9, "mcp"),   # ne compte pas
        ("r1", "oto_doc", {"op": "get"}, True, j(3) - 10, "mcp"),        # tenue
        ("r1", "web_preview", {"op": "get"}, True, j(3) - 11, "mcp"),    # simulation lue
        ("r2", "data_write", {"id": "p1"}, True, j(3) - 31, "mcp"),
        ("r2", "serper_search", None, True, j(3) - 32, "mcp"),
        ("r3", "data_write", {"record_id": "p1"}, True, j(1) - 1, "mcp"),
        ("r3", "hubspot_object", {"op": "update", "id": "c1"}, True, j(1) - 2, "mcp"),
        ("r4", "serper_search", None, True, j(1) - 26, "rest"),
        ("r5", "data_write", {"id": "z"}, True, j(1) - 27, "mcp"),
        ("r6", "data_write", {"id": "z"}, True, j(40) - 1, "mcp"),
        ("r7", "whatsapp_act", {"op": "send"}, True, j(2) - 1, "mcp"),
        ("r7", "data_write", {"id": "p1"}, True, j(2) - 2, "mcp"),
        ("r8", "serper_search", None, True, j(2) - 1, "mcp"),
        ("r8", "whatsapp_act", {"op": "send"}, True, j(2) - 2, "mcp"),
        ("r9", "data_write", {"id": "q"}, True, j(2) - 1, "mcp"),
        ("r10", "data_write", {"id": "q"}, True, j(2) - 1, "mcp"),
        ("r11", "data_write", {"id": "q"}, True, j(2) - 1, "mcp"),
    ])
    for surcharges in (None, {"record": 0.3, "research": 1.7, "report": 7.1}):
        m = time_saved.standards(surcharges)
        ref = _reference({5, 7}, 28, m)
        assert ref[0] and ref[1] == {5: 2}, "le banc doit porter des runs comptés et non comptés"
        _comparer(ref, _par_la_base([5, 7], 28, m))


def test_un_journal_aleatoire_compte_pareil_par_la_base(base, effets):
    from oto_mcp import time_saved
    rnd = random.Random(1311)
    outils = [("data_write", None), ("acme_data_write", None), ("serper_search", None),
              ("gmail_act", "send"), ("gmail_act", "Send"), ("gmail_act", "archive"),
              ("gmail_act", "search"), ("whatsapp_act", "send"), ("hubspot_object", "update"),
              ("data_claim_next", None), ("web_preview", "get"), ("demo-x_serper_search", None)]
    travaux, appels = [], []
    for i in range(400):
        run = f"r{i % 350}"                        # des reprises : runs partagés
        travaux.append((ORG, {"trigger_id": str(1 + i % 4)}, "done",
                        rnd.randint(1, 20 * 24 * 60), run if i % 37 else None))
        for _ in range(rnd.randint(0, 6)):
            t, op = rnd.choice(outils)
            args = {"op": op} if op else {}
            if rnd.random() < 0.6:
                args[rnd.choice(["id", "email", "url"])] = rnd.choice(["", "a", "b", "c", "d"])
            appels.append((run, t, args, rnd.random() > 0.1, rnd.randint(1, 20 * 24 * 60),
                           "mcp" if rnd.random() > 0.05 else "rest"))
    _poser(base, travaux, appels)
    m = time_saved.standards({"action": 3.3})
    _comparer(_reference({1, 2, 3, 4}, 28, m), _par_la_base([1, 2, 3, 4], 28, m))


@contextmanager
def _compteur(RJ, monkeypatch):
    """Les requêtes de la lecture et les lignes qu'elle rend à Python."""
    vu = {"requetes": 0, "lignes": 0}
    vrai = RJ._connect

    @contextmanager
    def _connect():
        with vrai() as conn:
            class _Espion:
                def execute(self, sql, params=None):
                    rows = conn.execute(sql, params).fetchall()
                    vu["requetes"] += 1
                    vu["lignes"] += len(rows)
                    return type("R", (), {"fetchall": lambda _s: rows})()
            yield _Espion()
    monkeypatch.setattr(RJ, "_connect", _connect)
    yield vu
    monkeypatch.setattr(RJ, "_connect", vrai)


def test_ce_que_python_recoit_ne_grandit_pas_avec_l_historique(base, effets, monkeypatch):
    """Dix fois plus de runs au même rythme : mêmes requêtes, mêmes lignes rendues —
    c'était un travail et une quinzaine d'appels décodés par run."""
    from oto_mcp import time_saved
    from oto_mcp.db import runner_jobs as RJ

    def _historique(n):
        base.execute("DELETE FROM runner_jobs")
        base.execute("DELETE FROM tool_calls")
        base.execute("DELETE FROM runs")
        travaux, appels = [], []
        for i in range(n):
            run = f"r{i}"
            m = 1 + i * (20 * 24 * 60) // n
            travaux.append((ORG, {"trigger_id": str(1 + i % 3)}, "done", m, run))
            appels += [(run, "serper_search", {"q": "x" * 200}, True, m, "mcp")] * 10
            appels += [(run, "data_write", {"id": f"p{i}"}, True, m, "mcp"),
                       (run, "gmail_act", {"op": "send"}, True, m, "mcp"),
                       (run, "oto_doc", {"op": "get"}, True, m, "mcp")]
        _poser(base, travaux, appels)
        with _compteur(RJ, monkeypatch) as vu:
            formes, _sans = RJ.formes_des_runs_conclus(ORG, 28, [1, 2, 3], time_saved.classe)
        return vu, sum(f["runs"] for f in formes)

    petit, n_petit = _historique(100)
    grand, n_grand = _historique(2000)
    assert (n_petit, n_grand) == (100, 2000), "chaque run est compté"
    assert grand == petit, (petit, grand)
    assert grand["requetes"] == 3 and grand["lignes"] <= 10


def test_sans_agent_vu_rien_n_est_lu(base, monkeypatch):
    from oto_mcp import time_saved
    from oto_mcp.db import runner_jobs as RJ
    with _compteur(RJ, monkeypatch) as vu:
        assert RJ.formes_des_runs_conclus(ORG, 28, [], time_saved.classe) == ([], [])
    assert vu["requetes"] == 0
