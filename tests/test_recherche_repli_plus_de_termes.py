"""Le repli d'une saisie longue : les contenus qui portent le PLUS de termes.

Contre un vrai PostgreSQL : le repli EST du SQL (fréquences lues dans le GIN, termes
cités en tsquery, comptage sur le vecteur de classement), et une source jointe (le
texte des fichiers) n'a pas les mêmes noms de colonnes qu'une source simple.

Ce qui est gardé ici :

1. quand le ET de tous les termes ne trouve rien, sortent les contenus qui en portent
   le PLUS — et eux seuls : un contenu qui ne porte qu'un terme fréquent, même répété
   partout, n'en fait pas partie ;
2. un terme FRÉQUENT (au-delà de `CANDIDATS_MAX`) ne pilote pas la sélection, et le
   contenu qui porte le plus de termes sort quand même ;
3. les trois sources de prose qui ont un vrai volume (pages, lignes, texte des
   fichiers) suivent le même repli ;
4. une saisie de mots vides seulement, ou de caractères à citer, ne lève pas ;
5. (sans base) la sous-chaîne de la saisie entière ne sert qu'aux saisies courtes, et le
   repli passe par la connexion du ET.
"""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

HUIT = "remboursement forfait séances avoir impact CA board Monday"


@pytest.fixture
def projet(live):
    from oto_mcp import db
    p = db.create_project("org", "1", "Projet " + uuid.uuid4().hex[:6])
    return int(p["id"] if isinstance(p, dict) else p)


def _pages(projet: int) -> dict[str, int]:
    from oto_mcp import db
    ids = {}
    ids["six"] = db.create_doc(projet, "Synthèse", body_md=(
        "Le remboursement du forfait de dix séances, un avoir émis : impact sur le CA."))
    ids["cinq"] = db.create_doc(projet, "Note", body_md=(
        "Remboursement d'un forfait, séances reportées, impact sur le CA."))
    ids["deux"] = db.create_doc(projet, "Brève", body_md="Le board se réunit Monday.")
    for i in range(6):
        ids[f"ca{i}"] = db.create_doc(projet, f"CA {i}", body_md="CA CA CA, le CA du mois.")
    return ids


def test_le_repli_rend_les_contenus_qui_portent_le_plus_de_termes(projet):
    from oto_mcp import db
    ids = _pages(projet)
    hits = db.search_docs_fts(HUIT, [projet], limit=20)
    assert [h["id"] for h in hits] == [ids["six"]], (
        "six termes sur huit : seul ce contenu sort, ni celui qui en porte cinq ni ceux "
        "qui ne portent que « CA »")
    assert "<b>" in hits[0]["headline"], "le passage surligne les termes portés"


def test_un_terme_frequent_ne_pilote_pas_la_selection(projet, monkeypatch):
    """« ca » au-delà du seuil : les pilotes sont les termes rares, et le contenu à six
    termes sort quand même — c'est la garantie du repli, pas une chance."""
    from oto_mcp.db import search as dbs
    ids = _pages(projet)
    monkeypatch.setattr(dbs, "CANDIDATS_MAX", 4)
    hits = dbs.search_docs_fts(HUIT, [projet], limit=20)
    assert [h["id"] for h in hits] == [ids["six"]]


def test_le_et_qui_trouve_ne_passe_pas_par_le_repli(projet):
    from oto_mcp import db
    ids = _pages(projet)
    hits = db.search_docs_fts("remboursement forfait impact CA", [projet], limit=20)
    assert {h["id"] for h in hits} == {ids["six"], ids["cinq"]}


def test_les_lignes_de_tableau_suivent_le_meme_repli(live):
    from oto_mcp import db
    ns = db.create_datastore("org", "1", "suivi-" + uuid.uuid4().hex[:6])
    db.datastore_insert_row(ns, "six", {"ca": 10, "note": (
        "remboursement du forfait, séances, avoir, impact")})
    for i in range(5):
        db.datastore_insert_row(ns, f"r{i}", {"ca": i, "note": "rien à signaler"})
    hits = db.search_datastore_rows_fts(HUIT, [ns], limit=20)
    assert [h["row_id"] for h in hits] == ["six"]


def test_le_texte_des_fichiers_suit_le_meme_repli(projet):
    """La source JOINTE (`project_file_texts t JOIN project_files f`) : les alias de
    table doivent tenir dans les deux requêtes du repli."""
    from oto_mcp import db
    fichiers = {}
    for nom, texte in (("six.pdf", "remboursement forfait séances avoir impact CA"),
                       ("ca.pdf", "CA CA CA CA")):
        f = db.add_project_file(projet, f"s3/{uuid.uuid4().hex}", nom)
        db.save_extracted_text(int(f["id"]), status="ok", text=texte)
        fichiers[nom] = int(f["id"])
    hits = db.search_file_contents(HUIT, [projet], limit=20)
    assert [h["id"] for h in hits] == [fichiers["six.pdf"]]


@pytest.mark.parametrize("q", ["le la les des du de", "l'impact d'o'neil \\ x'y"])
def test_une_saisie_vide_de_sens_ou_a_citer_ne_leve_pas(projet, q):
    _pages(projet)
    from oto_mcp import db
    db.search_docs_fts(q, [projet], limit=20)


class _Connexion:
    """Relève le SQL joué ; toute requête rend zéro ligne."""

    def __init__(self, sql: list):
        self._sql = sql

    def execute(self, sql, params=()):
        self._sql.append(sql)
        return SimpleNamespace(fetchall=lambda: [])


@pytest.mark.parametrize("q, sous_chaine", [("syl", True), ("compte rendu mensuel", True),
                                            ("compte rendu mensuel du board", False)])
def test_la_sous_chaine_ne_sert_qu_aux_saisies_courtes(monkeypatch, q, sous_chaine):
    from oto_mcp.db import search as dbs
    sql, emprunts = [], []

    @contextmanager
    def _connect():
        emprunts.append(1)
        yield _Connexion(sql)
    monkeypatch.setattr(dbs, "_connect", _connect)
    assert dbs.search_docs_fts(q, [1]) == []
    assert ("ILIKE" in sql[0]) is sous_chaine
    if len(q.split()) > 1:
        assert len(sql) == 2, "le ET vide passe au repli (fréquences des termes)"
        assert not any("ILIKE" in s for s in sql[1:]), "le repli ne cherche pas de sous-chaîne"
    assert emprunts == [1], "ET et repli : une seule connexion"
