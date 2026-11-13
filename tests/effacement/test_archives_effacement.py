"""Les archives froides du journal, réécrites sans l'org effacée (`effacement.archives`).

Un seau en mémoire porte deux mois archivés, au format de `deploy/archive_tool_calls.py`
(CSV gzip, en-tête, `NULL` = champ vide). Le registre `journal_archives` est le vrai.
"""
from __future__ import annotations

import csv
import gzip
import io

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg.rows import dict_row  # noqa: E402

from oto_mcp.effacement import archives  # noqa: E402

ORG, PARTANT = 42, "compte-qui-part"


class _Absent(Exception):
    response = {"Error": {"Code": "404"}}


class Seau:
    def __init__(self):
        self.objets: dict[str, bytes] = {}

    def get_object(self, Bucket, Key):  # noqa: N803
        return {"Body": io.BytesIO(self.objets[Key])}

    def head_object(self, Bucket, Key):  # noqa: N803
        if Key not in self.objets:
            raise _Absent()
        return {"ContentLength": len(self.objets[Key])}

    def upload_file(self, chemin, Bucket, Key):  # noqa: N803
        with open(chemin, "rb") as f:
            self.objets[Key] = f.read()

    def delete_object(self, Bucket, Key):  # noqa: N803
        self.objets.pop(Key, None)


def _archive(lignes: list[list[str]]) -> bytes:
    tampon = io.StringIO()
    w = csv.writer(tampon)
    w.writerow(["id", "created_at", "sub", "org_id", "tool", "args"])
    w.writerows(lignes)
    return gzip.compress(tampon.getvalue().encode())


def _relire(seau: Seau, cle: str) -> list[list[str]]:
    with gzip.open(io.BytesIO(seau.objets[cle]), "rt", newline="") as f:
        return list(csv.reader(f))[1:]


@pytest.fixture
def conn(live, pg_module_dsn):
    with psycopg.connect(pg_module_dsn, autocommit=True, row_factory=dict_row) as c:
        c.execute("DELETE FROM journal_archives")
        yield c


def test_les_lignes_de_l_org_et_des_partants_sortent_le_reste_demeure(conn):
    seau = Seau()
    seau.objets["journal/tool_calls/2026-08.csv.gz"] = _archive([
        ["1", "t", "x", str(ORG), "oto_doc", '{"a": "secret de l\'org"}'],
        ["2", "t", PARTANT, "", "oto_whoami", "{}"],
        ["3", "t", "voisin", "7", "oto_doc", "{}"],
        ["4", "t", PARTANT, "7", "oto_doc", "{}"],   # dans une AUTRE org : reste
    ])
    seau.objets["journal/tool_calls/2026-09.csv.gz"] = _archive([["5", "t", "voisin", "7", "x", "{}"]])
    conn.execute("INSERT INTO journal_archives (mois, cle, lignes) VALUES "
                 "('2026-08', 'journal/tool_calls/2026-08.csv.gz', 4), "
                 "('2026-09', 'journal/tool_calls/2026-09.csv.gz', 1)")
    out = archives.reecrire(conn, seau, "seau", id_effacement=9, org_id=ORG,
                            partants=(PARTANT,), depuis_mois="2026-01")
    assert out == {"mois_lus": 2, "mois_reecrits": 1, "lignes_retirees": 2}
    neuve = "journal/tool_calls/2026-08.effacement-9.csv.gz"
    assert "journal/tool_calls/2026-08.csv.gz" not in seau.objets
    assert [r[0] for r in _relire(seau, neuve)] == ["3", "4"]
    reg = {r["mois"]: r for r in conn.execute("SELECT mois, cle, lignes FROM journal_archives")}
    assert (reg["2026-08"]["cle"], reg["2026-08"]["lignes"]) == (neuve, 2)
    # Le mois sans rien de l'org n'est pas touché.
    assert reg["2026-09"]["cle"] == "journal/tool_calls/2026-09.csv.gz"
    # Rejouer ne refait rien.
    assert archives.reecrire(conn, seau, "seau", id_effacement=9, org_id=ORG,
                             partants=(PARTANT,), depuis_mois="2026-01")["mois_reecrits"] == 0


def test_les_lignes_gardees_sont_recopiees_octet_pour_octet(conn):
    """NULL (champ vide) et chaîne vide (`""`) ne se confondent pas, et un champ sur
    plusieurs lignes reste entier : la ligne gardée est celle de la source, telle quelle."""
    seau = Seau()
    garde = 'g1,t,voisin,7,oto_doc,"multi\nligne ""guillemets"""\r\n' \
            'g2,t,,7,"",\r\n'
    brut = ("id,created_at,sub,org_id,tool,args\r\n" + garde +
            f'x1,t,x,{ORG},oto_doc,"secret"\r\n').encode()
    seau.objets["journal/tool_calls/2026-08.csv.gz"] = gzip.compress(brut)
    conn.execute("INSERT INTO journal_archives (mois, cle, lignes) VALUES "
                 "('2026-08', 'journal/tool_calls/2026-08.csv.gz', 3)")
    archives.reecrire(conn, seau, "seau", id_effacement=9, org_id=ORG, partants=(),
                      depuis_mois="2026-01")
    neuf = gzip.decompress(seau.objets["journal/tool_calls/2026-08.effacement-9.csv.gz"])
    assert neuf.decode() == "id,created_at,sub,org_id,tool,args\r\n" + garde


def test_un_compte_qui_ne_tombe_pas_juste_ne_reecrit_rien(conn):
    seau = Seau()
    seau.objets["journal/tool_calls/2026-08.csv.gz"] = _archive([["1", "t", "x", str(ORG), "t", "{}"]])
    conn.execute("INSERT INTO journal_archives (mois, cle, lignes) VALUES "
                 "('2026-08', 'journal/tool_calls/2026-08.csv.gz', 3)")
    with pytest.raises(archives.ArchiveNonReecrite):
        archives.reecrire(conn, seau, "seau", id_effacement=9, org_id=ORG, partants=(),
                          depuis_mois="2026-01")
    assert list(seau.objets) == ["journal/tool_calls/2026-08.csv.gz"]
