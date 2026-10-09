"""Un run archivé garde ses bornes, et sa page DIT que son contenu est archivé (#665).

Arbitrage d'Alexis du 23/09/2026, option B. L'archive du journal (`deploy/
archive_tool_calls.py`) exporte au froid puis supprime les mois entiers au-delà de la
rétention, en EXEMPTANT `run_start`/`run_finish` : passé 90 jours, un run garde ses
bornes et perd son corps. Sans rien de plus, sa page servait deux lignes sous une issue
« done » — la page vide de #289 revenue à une autre borne.

Ce que ces tests verrouillent :
- **le registre** : l'archive inscrit le mois dans `journal_archives` APRÈS la relecture
  et AVANT la suppression, et ne supprime RIEN si l'inscription échoue ;
- **la lecture** (vrai PostgreSQL, DDL réel) : la page d'un run dont un mois est inscrit
  porte `content_archived` avec sa date et sa phrase ; un run sans corps mais dont aucun
  mois n'est inscrit n'est PAS dit archivé (lu, jamais déduit) ; le scope d'org tient ;
- **les deux faces** servent le champ (vue d'org et vue plateforme).
"""
from __future__ import annotations

import importlib.util
import pathlib
import re
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

from oto_mcp.db import _schema, usage


def _real_ddl(table: str) -> str:
    m = re.search(rf"^CREATE TABLE IF NOT EXISTS {table} \(.*?^\);",
                  _schema._SCHEMA, re.S | re.M)
    assert m, f"DDL de `{table}` introuvable dans _schema.py"
    return m.group(0)


@pytest.fixture()
def conn(pg_module_dsn, monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    from psycopg.rows import dict_row
    with psycopg.connect(pg_module_dsn, row_factory=dict_row, autocommit=True) as c:
        for t in ("journal_archives", "tool_calls"):
            c.execute(f"DROP TABLE IF EXISTS {t}")
            c.execute(_real_ddl(t))

        @contextmanager
        def _connect_test():
            yield c

        monkeypatch.setattr(usage, "_connect", _connect_test)
        yield c


def _fait(conn, run_id, tool, quand, org=7):
    conn.execute("INSERT INTO tool_calls (created_at, sub, tool, run_id, org_id) "
                 "VALUES (%s, 'u1', %s, %s, %s)", (quand, tool, run_id, org))


def _run_de_juin(conn, run_id="r-juin", *, clos=True, org=7):
    """Un run ouvert et clos en juin 2026 ; son corps a été archivé (supprimé)."""
    _fait(conn, run_id, "run_start", datetime(2026, 6, 10, 9, tzinfo=timezone.utc), org)
    if clos:
        _fait(conn, run_id, "run_finish", datetime(2026, 6, 10, 11, tzinfo=timezone.utc), org)


def _inscrire(conn, mois, quand):
    conn.execute("INSERT INTO journal_archives (mois, cle, lignes, archived_at) "
                 "VALUES (%s, %s, 42, %s)", (mois, f"journal/tool_calls/{mois}.csv.gz", quand))


def test_un_run_dont_le_mois_est_archive_le_DIT_avec_sa_date(conn):
    _run_de_juin(conn)
    _inscrire(conn, "2026-06", datetime(2026, 10, 3, 4, 45, tzinfo=timezone.utc))
    etat = usage.run_content_archived("r-juin")
    assert etat["months"] == ["2026-06"]
    assert etat["message"].startswith("Contenu archivé le 03/10/2026")
    assert "bornes" in etat["message"]


def test_un_run_sans_corps_mais_non_inscrit_nest_pas_dit_archive(conn):
    """Lu, jamais déduit : un run vide entre ses bornes n'est pas un run archivé."""
    _run_de_juin(conn)
    _inscrire(conn, "2026-05", datetime(2026, 9, 3, tzinfo=timezone.utc))
    assert usage.run_content_archived("r-juin") is None


def test_un_run_reste_ouvert_consulte_les_mois_jusqua_aujourdhui(conn):
    """Sans `run_finish`, le corps a pu continuer après la dernière ligne restante."""
    _run_de_juin(conn, "r-ouvert", clos=False)
    _inscrire(conn, "2026-07", datetime(2026, 11, 3, tzinfo=timezone.utc))
    assert usage.run_content_archived("r-ouvert")["months"] == ["2026-07"]


def test_le_scope_dorg_tient(conn):
    _run_de_juin(conn, org=7)
    _inscrire(conn, "2026-06", datetime(2026, 10, 3, tzinfo=timezone.utc))
    assert usage.run_content_archived("r-juin", org_id=8) is None
    assert usage.run_content_archived("r-juin", org_id=7) is not None


def test_les_deux_faces_servent_content_archived(conn):
    from oto_mcp.capabilities import org_monitoring, usage as cap_usage
    _run_de_juin(conn)
    _inscrire(conn, "2026-06", datetime(2026, 10, 3, tzinfo=timezone.utc))
    org = org_monitoring._run(None, org_monitoring.OrgRunInput(org_id=7, run_id="r-juin"))
    plateforme = cap_usage._run(None, cap_usage.RunInput(run_id="r-juin"))
    for page in (org, plateforme):
        assert [c["tool"] for c in page["calls"]] == ["run_start", "run_finish"]
        assert page["content_archived"]["months"] == ["2026-06"]


# ── L'archive inscrit AVANT de supprimer, et ne supprime rien sans inscription ──

def _script():
    chemin = pathlib.Path(__file__).resolve().parents[1] / "deploy" / "archive_tool_calls.py"
    spec = importlib.util.spec_from_file_location("archive_tool_calls_665", chemin)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_une_inscription_ne_se_reecrit_jamais(conn):
    """#1197 : la réécrire avec le compte d'une reprise effaçait la trace des lignes déjà
    supprimées. La seconde inscription lève, la première reste entière."""
    arch = _script()
    arch._record_archive(conn, "2026-06", "journal/tool_calls/2026-06.csv.gz", 10)
    premiere = conn.execute("SELECT archived_at FROM journal_archives").fetchone()["archived_at"]
    with pytest.raises(arch.ArchiveIncoherente, match="déjà inscrit"):
        arch._record_archive(conn, "2026-06", "journal/tool_calls/2026-06.csv.gz", 3)
    row = conn.execute("SELECT archived_at, lignes FROM journal_archives").fetchone()
    assert row["archived_at"] == premiere and row["lignes"] == 10


class _ConnVerrou:
    """Connexion factice : ne sert que le verrou consultatif de `main`."""
    def execute(self, sql, *a):
        class _R:
            def fetchone(self_inner):
                return (True,)
        return _R()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.mark.parametrize("inscription_ok", [True, False])
def test_main_inscrit_avant_de_supprimer_et_rien_sans_inscription(monkeypatch, inscription_ok):
    arch = _script()
    journal = []
    monkeypatch.setenv("OTO_MCP_S3_BUCKET", "b")
    monkeypatch.setenv("DATABASE_URL", "postgresql://factice")
    monkeypatch.setattr(arch.psycopg, "connect", lambda *a, **k: _ConnVerrou())
    monkeypatch.setattr(arch, "_s3_client", lambda: object())
    monkeypatch.setattr(arch, "_months_to_archive", lambda c, r: [("2026-06", 5)])
    monkeypatch.setattr(arch, "_inscription", lambda c, m: None)
    monkeypatch.setattr(arch, "_objet_present", lambda s, b, k: None)
    monkeypatch.setattr(arch, "_export_month", lambda c, s, b, m: f"k/{m}")
    monkeypatch.setattr(arch, "_verify_archive", lambda s, b, k, n: journal.append("relu"))

    def _record(c, m, k, n):
        if not inscription_ok:
            raise RuntimeError('relation "journal_archives" does not exist')
        journal.append("inscrit")

    monkeypatch.setattr(arch, "_record_archive", _record)
    monkeypatch.setattr(arch, "_delete_month", lambda c, m, p: journal.append("supprimé") or 5)
    monkeypatch.setattr("sys.argv", ["archive_tool_calls.py"])
    if inscription_ok:
        assert arch.main() == 0
        assert journal == ["relu", "inscrit", "supprimé"]
    else:
        with pytest.raises(RuntimeError):
            arch.main()
        assert journal == ["relu"]
