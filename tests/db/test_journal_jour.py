"""Les totaux du journal par jour UTC (#1147) : consolider, contre un VRAI PostgreSQL.

Ce qui se juge ici, c'est le STOCKAGE : un jour consolidé porte exactement ce que le
journal de ce jour contient (sommes, valeurs, distincts), le rejouer ne change rien, le
jour courant est refusé, et la maintenance comme le rattrapage avancent sans trou. La
lecture (agrégat + journal direct) se juge dans `test_journal_jour_lecteurs.py`.
"""
from __future__ import annotations

import os
import pathlib
import sys

import psycopg
import pytest
from psycopg.rows import dict_row

from oto_mcp.db import journal_jour

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from scripts import rattraper_journal_jour  # noqa: E402

pytestmark = pytest.mark.usefixtures("live")


def _c():
    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row, autocommit=True)


def _vider():
    with _c() as c:
        c.execute("DELETE FROM tool_calls")
        c.execute("DELETE FROM journal_jours_consolides")


def _appel(c, jours: int, heure: str = "10:00", **kw):
    """Un appel il y a `jours` jours UTC, à `heure` UTC."""
    cols = {"kind": "mcp", "sub": "u1", "tool": "outil_a", "org_id": 1, "ok": True,
            "duration_ms": 100, "result_size": None, "quantity": None,
            "key_mode": None, "args": None, **kw}
    c.execute(
        f"""INSERT INTO tool_calls (created_at, {', '.join(cols)})
            VALUES (((now() AT TIME ZONE 'UTC')::date - %s + %s::time) AT TIME ZONE 'UTC',
                    {', '.join(['%s'] * len(cols))})""",
        [jours, heure] + [psycopg.types.json.Jsonb(v) if k == "args" and v is not None
                          else v for k, v in cols.items()])


def _jour(c, jours: int) -> str:
    return c.execute("SELECT to_char((now() AT TIME ZONE 'UTC')::date - %s, 'YYYY-MM-DD') AS j",
                     (jours,)).fetchone()["j"]


def test_un_jour_consolide_porte_exactement_son_journal():
    _vider()
    with _c() as c:
        _appel(c, 2, "00:00", duration_ms=10, result_size=5, quantity=3, key_mode="org",
               args={"enrichment_id": "job-1", "_client": {"name": "client-x"}})
        _appel(c, 2, "12:00", duration_ms=30, result_size=None,
               args={"enrichment_id": "job-1"})
        _appel(c, 2, "23:59:59.999", duration_ms=None, ok=False)
        _appel(c, 2, "08:00", kind="connector", tool="fournisseur", ok=False, sub="u2")
        _appel(c, 2, "09:00", kind="rest", tool="GET /api/x")          # pas agrégé
        _appel(c, 1, "00:00")                                           # jour suivant
        _appel(c, 3, "23:59:59.999")                                    # jour précédent
        jour = _jour(c, 2)
    r = journal_jour.consolider_jour(jour)
    assert r["lignes"] == 4 and r["jobs"] == 2
    with _c() as c:
        reg = c.execute("SELECT lignes FROM journal_jours_consolides WHERE jour = %s",
                        (jour,)).fetchone()
        assert reg["lignes"] == 4
        tot = c.execute(
            """SELECT kind, tool, ok, key_mode, client_name, appels, quantite, duree_n,
                      duree_somme, durees, taille_n, taille_somme, tailles
                 FROM journal_totaux_jour WHERE jour = %s
                ORDER BY kind, ok, key_mode NULLS LAST, client_name NULLS LAST""",
            (jour,)).fetchall()
        assert [(t["kind"], t["ok"], t["key_mode"], t["client_name"], t["appels"],
                 t["quantite"], t["duree_n"], t["duree_somme"], sorted(t["durees"]),
                 t["taille_n"], t["taille_somme"], t["tailles"]) for t in tot] == [
            ("connector", False, None, None, 1, 1, 1, 100, [100], 0, 0, []),
            ("mcp", False, None, None, 1, 1, 0, 0, [], 0, 0, []),
            ("mcp", True, "org", "client-x", 1, 3, 1, 10, [10], 1, 5, [5]),
            ("mcp", True, None, None, 1, 1, 1, 30, [30], 0, 0, []),
        ]
        jobs = c.execute("SELECT org_id, tool, key_mode, job_id FROM journal_jobs_jour "
                         "WHERE jour = %s ORDER BY key_mode NULLS LAST", (jour,)).fetchall()
        # Le même job relevé sous deux modes de clé : deux clés, une par mode.
        assert [(j["tool"], j["key_mode"], j["job_id"]) for j in jobs] == [
            ("outil_a", "org", "job-1"), ("outil_a", None, "job-1")]


def test_rejouer_un_jour_ne_change_rien_et_suit_le_journal():
    _vider()
    with _c() as c:
        _appel(c, 2)
        jour = _jour(c, 2)
    journal_jour.consolider_jour(jour)
    journal_jour.consolider_jour(jour)
    with _c() as c:
        assert c.execute("SELECT count(*) AS n FROM journal_totaux_jour").fetchone()["n"] == 1
        _appel(c, 2, "11:00")
    journal_jour.consolider_jour(jour)
    with _c() as c:
        assert c.execute("SELECT sum(appels) AS n FROM journal_totaux_jour").fetchone()["n"] == 2


def test_un_jour_sans_appel_est_consolide_quand_meme():
    _vider()
    with _c() as c:
        jour = _jour(c, 4)
    assert journal_jour.consolider_jour(jour)["lignes"] == 0
    with _c() as c:
        assert c.execute("SELECT count(*) AS n FROM journal_jours_consolides "
                         "WHERE jour = %s", (jour,)).fetchone()["n"] == 1


@pytest.mark.parametrize("jours", [0, -1])
def test_le_jour_courant_ne_se_consolide_pas(jours):
    _vider()
    with _c() as c:
        jour = _jour(c, jours)
    with pytest.raises(journal_jour.JourNonClos):
        journal_jour.consolider_jour(jour)
    with _c() as c:
        assert c.execute("SELECT count(*) AS n FROM journal_jours_consolides").fetchone()["n"] == 0


def test_une_borne_depassee_n_ecrit_rien():
    """Le jour reste dans son état précédent : la transaction annulée n'a rien retiré.
    Une autre session tient le verrou de consolidation ; la borne coupe l'attente."""
    _vider()
    with _c() as c:
        _appel(c, 2)
        jour = _jour(c, 2)
    journal_jour.consolider_jour(jour)
    with _c() as c:
        _appel(c, 2, "11:00")
        with _c() as tient:
            tient.execute(f"SELECT pg_advisory_lock({journal_jour._VERROU})")
            with pytest.raises(psycopg.errors.QueryCanceled):
                journal_jour.consolider_jour(jour, duree_max_ms=200)
            tient.execute(f"SELECT pg_advisory_unlock({journal_jour._VERROU})")
        assert c.execute("SELECT sum(appels) AS n FROM journal_totaux_jour").fetchone()["n"] == 1
        assert c.execute("SELECT count(*) AS n FROM journal_jours_consolides").fetchone()["n"] == 1


def test_le_rattrapage_garde_la_couverture_contigue():
    _vider()
    with _c() as c:
        for j in (6, 5, 4, 3, 2, 1):
            _appel(c, j)
        hier, j3 = _jour(c, 1), _jour(c, 3)
        j6, j5, j4, j2 = _jour(c, 6), _jour(c, 5), _jour(c, 4), _jour(c, 2)
    # Registre vide : du plus récent au plus ancien.
    assert journal_jour.jours_a_consolider() == [hier, j2, j3, j4, j5, j6]
    journal_jour.consolider_jour(j3)
    # Après le dernier en avançant, puis avant le premier en reculant.
    assert journal_jour.jours_a_consolider() == [j2, hier, j4, j5, j6]
    assert journal_jour.jours_a_consolider(du=j4, au=j5) == []   # bornes inversées
    assert journal_jour.jours_a_consolider(du=j5) == [j2, hier, j4, j5]
    assert journal_jour.jours_a_consolider(refaire=True) == [hier, j2, j3, j4, j5, j6]


def test_la_maintenance_prend_la_veille_puis_les_jours_manques():
    _vider()
    with _c() as c:
        for j in range(1, 12):
            _appel(c, j)
        hier = _jour(c, 1)
        j10, j3 = _jour(c, 10), _jour(c, 3)
    # Registre vide : la veille seule — l'historique est l'affaire du rattrapage.
    assert journal_jour.maintenance(dry_run=True) == {"a_consolider": [hier], "au_dela": 0}
    journal_jour.consolider_jour(j10)
    plan = journal_jour.maintenance(dry_run=True)
    assert len(plan["a_consolider"]) == journal_jour.MAINTENANCE_JOURS_MAX
    assert plan["a_consolider"][-1] == j3 and plan["au_dela"] == 2
    fait = journal_jour.maintenance()
    assert [r["jour"] for r in fait["consolides"]] == plan["a_consolider"]
    e = journal_jour.etat()
    assert (e["premier"], e["dernier"], e["trous"]) == (j10, j3, 0)


def test_le_script_est_a_blanc_par_defaut_puis_reprend(capsys):
    _vider()
    with _c() as c:
        for j in (3, 2, 1):
            _appel(c, j)
    assert rattraper_journal_jour.main([]) == 0
    assert "À BLANC" in capsys.readouterr().out
    assert journal_jour.etat()["jours"] == 0
    assert rattraper_journal_jour.main(["--appliquer", "--pause", "0"]) == 0
    e = journal_jour.etat()
    assert e["jours"] == 3 and e["trous"] == 0
    capsys.readouterr()
    assert rattraper_journal_jour.main(["--appliquer", "--pause", "0"]) == 0
    assert "0 jour(s) à consolider" in capsys.readouterr().out
