"""`oto-mcp maintenance index-concurrents <révision>` : le geste manuel du §5.1, versionné.

Né le 09/10/2026 : la révision 0049 lève `ConstructionManuelleRequise` sur toute
`tool_calls` de plus de 100 000 lignes, et le lanceur d'une instance cible ne lance que
`oto-mcp …` — rien dans l'arbre ne posait ces index. La commande construit, un par un, les
index d'une révision, vérifie chacun, et NE retire JAMAIS un index invalide (décision
d'Alexis : un index cassé reste un geste humain) : elle nomme le `DROP` à jouer.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from oto_mcp import maintenance
from oto_mcp.db import index_concurrent as ic
from oto_mcp.db.index_releve import OUVERTURES, REVISION_OUVERTURES

RACINE = Path(__file__).resolve().parents[2]


class _Conn:
    """Une connexion factice : `indisvalid` par index, et le journal des ordres joués."""

    def __init__(self, etats: dict[str, bool | None], *, casse_apres: set[str] = frozenset()):
        self.etats = dict(etats)
        self.casse_apres = casse_apres
        self.joues: list[str] = []

    def execute(self, sql: str):
        self.joues.append(sql)
        for i in OUVERTURES:
            if sql == i.ddl_concurrent:
                self.etats[i.nom] = i.nom not in self.casse_apres
            if sql == i.sql_validite:
                valeur = self.etats.get(i.nom)
                return _Res(None if valeur is None else {"indisvalid": valeur})
        return _Res(None)


class _Res:
    def __init__(self, ligne):
        self.ligne = ligne

    def fetchone(self):
        return self.ligne


def _construits(conn: _Conn) -> list[str]:
    return [i.nom for i in OUVERTURES if i.ddl_concurrent in conn.joues]


def test_absents_construits_un_par_un_puis_verifies():
    conn = _Conn({})
    faits = ic.poser_a_la_main(conn, REVISION_OUVERTURES, dire=lambda _: None)
    assert _construits(conn) == [i.nom for i in OUVERTURES]
    assert all("construit et valide" in f for f in faits)
    # Hors de toute borne de durée, attente de verrou de quelques minutes (§5.1).
    assert conn.joues[:2] == ["SET statement_timeout = 0", ic.ATTENTE_MAX]


def test_le_numero_seul_designe_la_revision():
    conn = _Conn({})
    ic.poser_a_la_main(conn, "0049", dire=lambda _: None)
    assert _construits(conn) == [i.nom for i in OUVERTURES]


def test_deja_valides_rien_n_est_construit():
    conn = _Conn({i.nom: True for i in OUVERTURES})
    faits = ic.poser_a_la_main(conn, "0049", dire=lambda _: None)
    assert _construits(conn) == []
    assert all("déjà posé" in f for f in faits)


def test_un_index_invalide_refuse_nomme_le_drop_et_arrete_tout():
    premier, second, troisieme = OUVERTURES
    conn = _Conn({premier.nom: True, second.nom: False})
    with pytest.raises(ic.IndexInvalide) as e:
        ic.poser_a_la_main(conn, "0049", dire=lambda _: None)
    assert second.ddl_retrait in str(e.value)
    # Le DROP est NOMMÉ, jamais joué ; l'index suivant n'est pas construit.
    assert not any(sql.startswith("DROP") for sql in conn.joues)
    assert troisieme.ddl_concurrent not in conn.joues


def test_construit_mais_invalide_apres_refuse():
    premier = OUVERTURES[0]
    conn = _Conn({}, casse_apres={premier.nom})
    with pytest.raises(ic.IndexInvalide):
        ic.poser_a_la_main(conn, "0049", dire=lambda _: None)
    assert _construits(conn) == [premier.nom]


def test_revision_inconnue_refuse_en_listant_celles_qui_en_posent():
    with pytest.raises(ic.RevisionSansIndex) as e:
        ic.index_de_revision("0042")
    assert REVISION_OUVERTURES in str(e.value)


def test_la_commande_rend_le_code_du_refus(capsys):
    assert maintenance.main(["index-concurrents", "0042"]) == maintenance.INDEX_REFUS
    assert "REFUS" in capsys.readouterr().err


def test_le_texte_du_geste_cite_la_commande():
    texte = str(ic.ConstructionManuelleRequise(OUVERTURES[0], ic.CONSTRUCTION_MAX_LIGNES))
    assert f"oto-mcp maintenance index-concurrents {REVISION_OUVERTURES}" in texte
    assert "oto-mcp migrer upgrade head" in texte and "§5.1" in texte


def test_le_registre_couvre_chaque_index_declare():
    """Un `IndexConcurrent(nom=…)` déclaré dans le code et absent de `declares()`, la
    commande ne saurait pas le poser."""
    motif = re.compile(r'IndexConcurrent\(\s*nom="([^"]+)"')
    declares_dans_le_code = set()
    for chemin in (RACINE / "oto_mcp").rglob("*.py"):
        declares_dans_le_code |= set(motif.findall(chemin.read_text(encoding="utf-8")))
    assert declares_dans_le_code
    assert {i.nom for i in ic.declares()} == declares_dans_le_code


def test_un_vrai_passage_sur_une_base(live):
    """Le SQL réel, hors transaction : les trois index de 0049 retirés sont reconstruits
    et valides ; rejouée, la commande constate et ne construit rien."""
    from oto_mcp.db._conn import _connect, _connect_autocommit

    with _connect_autocommit(bornee=False) as conn:
        for i in OUVERTURES:
            conn.execute(i.ddl_retrait)
    assert maintenance.main(["index-concurrents", "0049"]) == maintenance.INDEX_POSES
    with _connect() as conn:
        scalaire = ic.scalaire_de(conn)
        assert all(scalaire(i.sql_validite) is True for i in OUVERTURES)

    vus: list[str] = []
    with _connect_autocommit(bornee=False) as conn:
        ic.poser_a_la_main(conn, "0049", dire=vus.append)
    assert len(vus) == len(OUVERTURES) and all("déjà posé" in v for v in vus)
