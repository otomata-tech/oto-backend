"""Chaque passage de l'archive du journal laisse sa trace, et son absence est une erreur.

`deploy/archive_tool_calls.py` tourne chaque jour en travail planifié. Le registre
`journal_archives` ne trace qu'un mois archivé, et son `archived_at` vieillit
normalement de 30 à 60 jours : une archive qui ne tournait plus ne se voyait nulle part.
Ce que ces bancs verrouillent (vrai PostgreSQL, DDL réel, Object Storage factice) :

- **la trace** (`journal_archive_passages`) : un passage sans mois éligible est RÉUSSI ;
  un passage qui archive compte ses mois ; un refus `ArchiveIncoherente` est en ÉCHEC,
  avec sa cause, et l'erreur reste relevée ; le verrou pris et le passage à blanc
  n'écrivent rien ;
- **le signal** (`oto_admin_monitoring op=journal_archive`) : à 47 h du dernier succès,
  l'état est rendu ; à 49 h, et quand il n'y a JAMAIS eu de succès, l'erreur nommée
  `journal_archive_late` est levée.
"""
from __future__ import annotations

import importlib.util
import io
import pathlib
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from botocore.exceptions import ClientError

from oto_mcp.capabilities import monitoring
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
from oto_mcp.db import _schema, usage

CTX = ResolvedCtx(sub="admin-sub")
TABLES = ("tool_calls", "journal_archives", "journal_archive_passages")


def _script():
    chemin = pathlib.Path(__file__).resolve().parents[2] / "deploy" / "archive_tool_calls.py"
    spec = importlib.util.spec_from_file_location("archive_tool_calls_passages", chemin)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ddl(table: str) -> str:
    m = re.search(rf"^CREATE TABLE IF NOT EXISTS {table} \(.*?^\);", _schema._SCHEMA,
                  re.S | re.M)
    assert m, f"DDL de `{table}` introuvable dans l'assemblage"
    return m.group(0)


class FauxS3:
    """Ce que le script demande à boto3, avec ses erreurs : `ClientError` au code 404."""

    def __init__(self):
        self.objets: dict[str, bytes] = {}

    def head_object(self, Bucket, Key):
        if Key not in self.objets:
            raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")
        return {"ContentLength": len(self.objets[Key])}

    def upload_file(self, path, Bucket, Key):
        self.objets[Key] = pathlib.Path(path).read_bytes()

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objets[Key])}


@pytest.fixture()
def base(pg_module_dsn, monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    from psycopg.rows import dict_row
    with psycopg.connect(pg_module_dsn, row_factory=dict_row, autocommit=True) as c:
        c.execute(f"DROP TABLE IF EXISTS {', '.join(TABLES)}")
        for t in TABLES:
            c.execute(_ddl(t))
        c.execute("INSERT INTO tool_calls (sub, tool) VALUES ('u1', 'fr_search')")  # aujourd'hui
        monkeypatch.setenv("DATABASE_URL", pg_module_dsn)
        monkeypatch.setenv("OTO_MCP_S3_BUCKET", "seau")

        @contextmanager
        def _connect_test():
            yield c

        monkeypatch.setattr(usage, "_connect", _connect_test)
        yield c


def _lancer(arch, monkeypatch, s3=None, *options):
    monkeypatch.setattr(arch, "_s3_client", lambda: s3 or FauxS3())
    monkeypatch.setattr("sys.argv", ["archive_tool_calls.py", "--pause", "0", *options])
    return arch.main()


def _passages(c):
    return c.execute("SELECT started_at, finished_at, ok, mois_archives, detail "
                     "FROM journal_archive_passages ORDER BY id").fetchall()


def _mois_ancien(c, n=5):
    debut = datetime(2025, 3, 1, tzinfo=timezone.utc)
    for i in range(n):
        c.execute("INSERT INTO tool_calls (created_at, sub, tool) VALUES (%s, 'u1', "
                  "'fr_search')", (debut + timedelta(days=i),))


# ── la trace ──────────────────────────────────────────────────────────────────

def test_un_passage_sans_mois_eligible_est_reussi(base, monkeypatch):
    assert _lancer(_script(), monkeypatch) == 0
    [p] = _passages(base)
    assert p["ok"] is True
    assert p["mois_archives"] == 0
    assert p["detail"] == "aucun mois éligible"
    assert p["started_at"] <= p["finished_at"]


def test_un_passage_qui_archive_compte_ses_mois(base, monkeypatch):
    _mois_ancien(base)
    assert _lancer(_script(), monkeypatch, FauxS3()) == 0
    [p] = _passages(base)
    assert (p["ok"], p["mois_archives"], p["detail"]) == (True, 1, "mois archivés : 2025-03")


def test_sans_table_de_trace_rien_n_est_supprime(base, monkeypatch):
    """Schéma plus ancien que le script : il s'arrête AVANT d'exporter ou de supprimer,
    plutôt que de purger sans trace (l'alerte dirait « en retard » à tort)."""
    _mois_ancien(base)
    base.execute("DROP TABLE journal_archive_passages")
    s3 = FauxS3()
    with pytest.raises(SystemExit, match="journal_archive_passages absente"):
        _lancer(_script(), monkeypatch, s3)
    assert base.execute("SELECT count(*) AS n FROM tool_calls").fetchone()["n"] == 6
    assert base.execute("SELECT count(*) AS n FROM journal_archives").fetchone()["n"] == 0
    assert s3.objets == {}


def test_un_passage_refuse_est_en_echec_et_l_erreur_reste_levee(base, monkeypatch):
    """Inscrit au registre, objet absent : `ArchiveIncoherente`, rien supprimé."""
    _mois_ancien(base)
    base.execute("INSERT INTO journal_archives (mois, cle, lignes) "
                 "VALUES ('2025-03', 'journal/tool_calls/2025-03.csv.gz', 5)")
    arch = _script()
    with pytest.raises(arch.ArchiveIncoherente):
        _lancer(arch, monkeypatch, FauxS3())
    [p] = _passages(base)
    assert p["ok"] is False
    assert p["mois_archives"] == 0
    assert p["detail"].startswith("ArchiveIncoherente: 2025-03 : inscrit au registre")


def test_une_exception_quelconque_est_un_echec(base, monkeypatch):
    arch = _script()

    def _panne(conn, jours):
        raise RuntimeError("base injoignable")

    monkeypatch.setattr(arch, "_months_to_archive", _panne)
    with pytest.raises(RuntimeError):
        _lancer(arch, monkeypatch)
    [p] = _passages(base)
    assert (p["ok"], p["detail"]) == (False, "RuntimeError: base injoignable")


def test_verrou_pris_et_passage_a_blanc_ne_comptent_pas(base, monkeypatch, pg_module_dsn):
    import psycopg
    arch = _script()
    with psycopg.connect(pg_module_dsn, autocommit=True) as autre:
        autre.execute("SELECT pg_advisory_lock(%s)", (arch.LOCK_KEY,))
        assert _lancer(arch, monkeypatch) == 0
        autre.execute("SELECT pg_advisory_unlock(%s)", (arch.LOCK_KEY,))
    assert _lancer(arch, monkeypatch, None, "--dry-run") == 0
    assert _passages(base) == []


# ── le signal ─────────────────────────────────────────────────────────────────

def _noter(c, il_y_a: timedelta, ok: bool, detail: str = "aucun mois éligible"):
    c.execute("INSERT INTO journal_archive_passages "
              "(started_at, finished_at, ok, mois_archives, detail) "
              "VALUES (now() - %s, now() - %s, %s, 0, %s)", (il_y_a, il_y_a, ok, detail))


def _lire():
    return monitoring._monitoring(CTX, monitoring.MonitoringInput(op="journal_archive"))


def test_signal_a_47h_rend_l_etat(base):
    _noter(base, timedelta(hours=47), True)
    etat = _lire()
    assert etat["etat"] == "a_jour"
    assert etat["last_success_at"] is not None
    assert etat["last_failure_at"] is None


def test_signal_a_49h_leve_journal_archive_late(base):
    _noter(base, timedelta(hours=49), True)
    _noter(base, timedelta(hours=2), False, "ArchiveIncoherente: 2025-03 : objet absent")
    with pytest.raises(AuthzDenied) as e:
        _lire()
    assert e.value.code == "journal_archive_late"
    assert "ArchiveIncoherente: 2025-03" in e.value.message
    assert e.value.details["last_success_at"] is not None
    assert e.value.details["last_failure_at"] is not None


def test_signal_jamais_de_passage_leve_journal_archive_late(base):
    with pytest.raises(AuthzDenied) as e:
        _lire()
    assert e.value.code == "journal_archive_late"
    assert "jamais" in e.value.message
    assert e.value.details["last_success_at"] is None


def test_signal_que_des_echecs_leve_journal_archive_late(base):
    _noter(base, timedelta(hours=1), False, "RuntimeError: boom")
    with pytest.raises(AuthzDenied) as e:
        _lire()
    assert e.value.code == "journal_archive_late"
    assert "RuntimeError: boom" in e.value.message


def test_signal_echec_apres_un_succes_recent(base):
    _noter(base, timedelta(hours=20), True)
    _noter(base, timedelta(hours=1), False, "RuntimeError: boom")
    etat = _lire()
    assert etat["etat"] == "echec_recent"
    assert etat["last_failure_detail"] == "RuntimeError: boom"


def test_l_op_ne_lit_aucun_filtre():
    with pytest.raises(AuthzDenied) as e:
        monitoring._monitoring(CTX, monitoring.MonitoringInput(op="journal_archive", days=3))
    assert e.value.code == "param_not_read_by_op"
