"""L'archive du journal se reprend sans jamais réécrire une archive, et purge par l'index (#1197).

`deploy/archive_tool_calls.py` exporte au froid puis supprime les mois sortis de la
rétention. Deux défauts, corrigés ici et verrouillés par ces bancs (vrai PostgreSQL, DDL
réel, Object Storage factice qui répond comme boto3) :

- **la reprise détruisait l'archive** : un passage interrompu pendant la suppression
  laissait un reste ; le passage suivant RÉÉCRIVAIT l'objet du mois avec ce seul reste et
  l'inscription avec son compte — les lignes déjà supprimées n'étaient plus nulle part.
  Désormais l'objet n'est jamais écrasé, une reprise ne fait que finir la suppression
  après avoir prouvé que chaque ligne restante est dans l'archive, et tout état
  incohérent lève `ArchiveIncoherente` sans rien supprimer ;
- **la suppression ne passait pas à l'échelle** : son prédicat `to_char(date_trunc(...))`
  ne servait aucun index ; elle avance maintenant par plage de `created_at`.
"""
from __future__ import annotations

import gzip
import importlib.util
import io
import pathlib
import re
import csv
from datetime import datetime, timedelta, timezone

import pytest
from botocore.exceptions import ClientError

from oto_mcp.db import _schema

MOIS = "2025-03"
CLE = f"journal/tool_calls/{MOIS}.csv.gz"
ORDINAIRES = 30


def _script():
    chemin = pathlib.Path(__file__).resolve().parents[2] / "deploy" / "archive_tool_calls.py"
    spec = importlib.util.spec_from_file_location("archive_tool_calls_1197", chemin)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ddl_reel() -> list[str]:
    table = re.search(r"^CREATE TABLE IF NOT EXISTS tool_calls \(.*?^\);", _schema._SCHEMA,
                      re.S | re.M).group(0)
    index = re.search(r"CREATE INDEX IF NOT EXISTS idx_tool_calls_created_at [^;]*;",
                      _schema._SCHEMA).group(0)
    registre = re.search(r"^CREATE TABLE IF NOT EXISTS journal_archives \(.*?^\);",
                         _schema._SCHEMA, re.S | re.M).group(0)
    return [table, index, registre]


class FauxS3:
    """Ce que le script demande à boto3, avec ses erreurs : `ClientError` au code 404."""

    def __init__(self):
        self.objets: dict[str, bytes] = {}
        self.ecritures: list[str] = []
        self.refus_head = False

    def head_object(self, Bucket, Key):
        if self.refus_head:
            raise ClientError({"Error": {"Code": "403", "Message": "Forbidden"}}, "HeadObject")
        if Key not in self.objets:
            raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")
        return {"ContentLength": len(self.objets[Key])}

    def upload_file(self, path, Bucket, Key):
        self.ecritures.append(Key)
        self.objets[Key] = pathlib.Path(path).read_bytes()

    def get_object(self, Bucket, Key):
        if Key not in self.objets:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "absent"}}, "GetObject")
        return {"Body": io.BytesIO(self.objets[Key])}


def _lire(objet: bytes) -> tuple[list[str], list[list[str]]]:
    lignes = list(csv.reader(io.StringIO(gzip.decompress(objet).decode())))
    return lignes[0], lignes[1:]


@pytest.fixture()
def base(pg_module_dsn, monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    from psycopg.rows import dict_row
    with psycopg.connect(pg_module_dsn, row_factory=dict_row, autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS journal_archives, tool_calls")
        for ddl in _ddl_reel():
            c.execute(ddl)
        debut = datetime(2025, 3, 1, tzinfo=timezone.utc)
        for i in range(ORDINAIRES):
            c.execute("INSERT INTO tool_calls (created_at, sub, tool, args) VALUES (%s, 'u1', "
                      "'fr_search', %s)", (debut + timedelta(hours=i * 20),
                                           '{"q": "a\\nb", "n": %d}' % i))
        for tool in ("run_start", "run_finish"):
            c.execute("INSERT INTO tool_calls (created_at, sub, tool) VALUES (%s, 'u1', %s)",
                      (debut + timedelta(days=2), tool))
        c.execute("INSERT INTO tool_calls (sub, tool) VALUES ('u1', 'fr_search')")  # aujourd'hui
        monkeypatch.setenv("DATABASE_URL", pg_module_dsn)
        monkeypatch.setenv("OTO_MCP_S3_BUCKET", "seau")
        yield c


@pytest.fixture()
def s3():
    return FauxS3()


def _lancer(arch, s3, monkeypatch, *options):
    monkeypatch.setattr(arch, "_s3_client", lambda: s3)
    monkeypatch.setattr("sys.argv", ["archive_tool_calls.py", "--pause", "0", *options])
    return arch.main()


def _ordinaires_du_mois(c) -> list[int]:
    return [r["id"] for r in c.execute(
        "SELECT id FROM tool_calls WHERE created_at < '2025-04-01' "
        "AND tool NOT IN ('run_start', 'run_finish') ORDER BY id").fetchall()]


def _registre(c):
    return c.execute("SELECT mois, cle, lignes, archived_at FROM journal_archives").fetchall()


class _Interrompu(Exception):
    pass


def _interrompre_apres_le_premier_lot(arch, monkeypatch):
    """La suppression est tuée après son premier lot (délai du service, crash)."""
    monkeypatch.setattr(arch, "DELETE_BATCH", 7)

    class _Temps:
        @staticmethod
        def sleep(_s):
            raise _Interrompu()

    monkeypatch.setattr(arch, "time", _Temps)


# ── Le cas nominal ne change pas : même objet, même format, même registre ──────────────

def test_nominal_exporte_inscrit_et_purge_le_mois_sans_toucher_aux_faits(base, s3, monkeypatch):
    arch = _script()
    ids = _ordinaires_du_mois(base)
    colonnes = [r["column_name"] for r in base.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'tool_calls' "
        "ORDER BY ordinal_position").fetchall()]
    monkeypatch.setattr(arch, "DELETE_BATCH", 7)
    assert _lancer(arch, s3, monkeypatch) == 0
    assert s3.ecritures == [CLE]
    entete, lignes = _lire(s3.objets[CLE])
    assert entete == colonnes
    assert sorted(int(l[entete.index("id")]) for l in lignes) == ids
    assert [(r["mois"], r["cle"], r["lignes"]) for r in _registre(base)] == [(MOIS, CLE, ORDINAIRES)]
    assert _ordinaires_du_mois(base) == []
    restes = base.execute("SELECT tool FROM tool_calls ORDER BY id").fetchall()
    assert sorted(r["tool"] for r in restes) == ["fr_search", "run_finish", "run_start"]


def test_un_passage_sans_mois_eligible_ne_fait_rien(base, s3, monkeypatch):
    """Le travail tourne chaque jour : le lendemain d'une purge complète, rien ne bouge."""
    arch = _script()
    assert _lancer(arch, s3, monkeypatch) == 0
    objet, registre = s3.objets[CLE], _registre(base)
    assert _lancer(arch, s3, monkeypatch) == 0
    assert s3.ecritures == [CLE] and s3.objets[CLE] == objet and _registre(base) == registre


# ── La reprise : elle finit la suppression, elle ne réexporte rien ─────────────────────

def test_reprise_apres_interruption_au_milieu_du_delete(base, s3, monkeypatch):
    arch = _script()
    ids = _ordinaires_du_mois(base)
    _interrompre_apres_le_premier_lot(arch, monkeypatch)
    with pytest.raises(_Interrompu):
        _lancer(arch, s3, monkeypatch)
    assert len(_ordinaires_du_mois(base)) == ORDINAIRES - 7
    objet, registre = s3.objets[CLE], _registre(base)

    arch = _script()  # le passage du lendemain
    assert _lancer(arch, s3, monkeypatch) == 0
    assert s3.ecritures == [CLE], "la reprise a réécrit l'archive"
    assert s3.objets[CLE] == objet
    assert _registre(base) == registre  # même compte, même date de première inscription
    assert _ordinaires_du_mois(base) == []
    entete, lignes = _lire(s3.objets[CLE])
    assert sorted(int(l[entete.index("id")]) for l in lignes) == ids  # rien n'est perdu


def test_un_objet_depose_sans_inscription_est_adopte_s_il_porte_exactement_le_mois(
        base, s3, monkeypatch):
    """Passage coupé entre dépôt et inscription, ou `--export-only` : l'objet couvre
    chaque ligne encore en base, au compte près — il est inscrit, pas réécrit."""
    arch = _script()
    assert _lancer(arch, s3, monkeypatch, "--export-only") == 0
    assert _registre(base) == [] and len(_ordinaires_du_mois(base)) == ORDINAIRES
    objet = s3.objets[CLE]
    assert _lancer(arch, s3, monkeypatch) == 0
    assert s3.ecritures == [CLE] and s3.objets[CLE] == objet
    assert [(r["cle"], r["lignes"]) for r in _registre(base)] == [(CLE, ORDINAIRES)]
    assert _ordinaires_du_mois(base) == []


# ── Les incohérences : une erreur nommée, et rien de supprimé ni d'écrasé ──────────────

def _objet_csv(ids: list[int]) -> bytes:
    tampon = io.StringIO()
    ecrivain = csv.writer(tampon)
    ecrivain.writerow(["id", "created_at", "tool"])
    for i in ids:
        ecrivain.writerow([i, "2025-03-01", "fr_search"])
    return gzip.compress(tampon.getvalue().encode())


def test_un_objet_present_sans_inscription_qui_differe_leve(base, s3, monkeypatch):
    arch = _script()
    etranger = _objet_csv(_ordinaires_du_mois(base)[:5])
    s3.objets[CLE] = etranger
    with pytest.raises(arch.ArchiveIncoherente, match="sans inscription"):
        _lancer(arch, s3, monkeypatch)
    assert s3.ecritures == [] and s3.objets[CLE] == etranger
    assert _registre(base) == [] and len(_ordinaires_du_mois(base)) == ORDINAIRES


def test_un_compte_different_de_l_inscription_leve(base, s3, monkeypatch):
    arch = _script()
    _interrompre_apres_le_premier_lot(arch, monkeypatch)
    with pytest.raises(_Interrompu):
        _lancer(arch, s3, monkeypatch)
    base.execute("UPDATE journal_archives SET lignes = lignes + 1")
    arch = _script()
    with pytest.raises(arch.ArchiveIncoherente, match="le registre en inscrit 31"):
        _lancer(arch, s3, monkeypatch)
    assert s3.ecritures == [CLE] and len(_ordinaires_du_mois(base)) == ORDINAIRES - 7


def test_un_mois_inscrit_dont_l_objet_a_disparu_leve(base, s3, monkeypatch):
    arch = _script()
    _interrompre_apres_le_premier_lot(arch, monkeypatch)
    with pytest.raises(_Interrompu):
        _lancer(arch, s3, monkeypatch)
    del s3.objets[CLE]
    arch = _script()
    with pytest.raises(arch.ArchiveIncoherente, match="ABSENT"):
        _lancer(arch, s3, monkeypatch)
    assert s3.ecritures == [CLE] and CLE not in s3.objets  # pas réexporté
    assert len(_ordinaires_du_mois(base)) == ORDINAIRES - 7


def test_une_ligne_restante_absente_de_l_archive_leve(base, s3, monkeypatch):
    """Une ligne entrée dans le mois après l'export : la supprimer la perdrait."""
    arch = _script()
    _interrompre_apres_le_premier_lot(arch, monkeypatch)
    with pytest.raises(_Interrompu):
        _lancer(arch, s3, monkeypatch)
    base.execute("INSERT INTO tool_calls (created_at, sub, tool) "
                 "VALUES ('2025-03-15', 'u1', 'fr_search')")
    arch = _script()
    with pytest.raises(arch.ArchiveIncoherente, match="ne sont PAS dans"):
        _lancer(arch, s3, monkeypatch)
    assert len(_ordinaires_du_mois(base)) == ORDINAIRES - 7 + 1


def test_l_export_refuse_d_ecraser_un_objet(base, s3):
    arch = _script()
    s3.objets[CLE] = b"existant"
    with pytest.raises(arch.ArchiveIncoherente, match="jamais écrasée"):
        arch._export_month(base, s3, "seau", MOIS)
    assert s3.objets[CLE] == b"existant" and s3.ecritures == []


def test_un_refus_d_acces_n_est_pas_une_absence(base, s3, monkeypatch):
    """Prendre un 403 pour « absent » ferait réexporter, donc écraser."""
    arch = _script()
    s3.refus_head = True
    with pytest.raises(ClientError):
        _lancer(arch, s3, monkeypatch)
    assert s3.ecritures == [] and len(_ordinaires_du_mois(base)) == ORDINAIRES


# ── La suppression se sert de l'index, sur le même découpage de mois que le compte ─────

def test_le_lot_et_l_export_se_servent_de_l_index_created_at(base):
    arch = _script()
    debut, fin = arch._bornes(base, MOIS)
    params = {"facts": list(arch.RUN_FACTS), "curseur": debut, "debut": debut, "fin": fin,
              "lot": arch.DELETE_BATCH}
    base.execute("SET enable_seqscan = off")
    try:
        for requete in (arch._SQL_LOT, f"SELECT * FROM tool_calls WHERE {arch._DU_MOIS}"):
            base.execute("BEGIN")
            plan = "\n".join(r["QUERY PLAN"] for r in base.execute(
                "EXPLAIN (COSTS OFF) " + requete, params).fetchall())
            base.execute("ROLLBACK")
            assert "idx_tool_calls_created_at" in plan, plan
            assert re.search(r"Index Cond: \(\(created_at >=", plan), plan
    finally:
        base.execute("RESET enable_seqscan")


def test_la_plage_du_mois_est_le_mois_compte_quel_que_soit_le_fuseau(base):
    """Les bornes viennent de la base, dans le fuseau de la session — comme le
    `date_trunc` qui compte les mois : aux limites, les deux voient les mêmes lignes."""
    arch = _script()
    base.execute("SET TIME ZONE 'Europe/Paris'")
    try:
        for instant in ("2025-03-31 22:30+00", "2025-02-28 23:30+00"):  # 1er à 00:30, Paris
            base.execute("INSERT INTO tool_calls (created_at, sub, tool) VALUES (%s, 'u1', 'x')",
                         (instant,))
        comptes = dict(arch._months_to_archive(base, 90))
        for mois, lignes in comptes.items():
            debut, fin = arch._bornes(base, mois)
            dans_la_plage = base.execute(
                f"SELECT count(*) AS n FROM tool_calls WHERE {arch._DU_MOIS}",
                {"facts": list(arch.RUN_FACTS), "debut": debut, "fin": fin}).fetchone()["n"]
            assert dans_la_plage == lignes, mois
        assert comptes["2025-03"] == ORDINAIRES + 1 and comptes["2025-04"] == 1
    finally:
        base.execute("RESET TIME ZONE")
