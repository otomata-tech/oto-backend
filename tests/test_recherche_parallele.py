"""La recherche interroge ses sources EN PARALLÈLE, à concurrence bornée.

Sans base : les sources SQL sont doublées par des fonctions qui dorment ou se donnent
rendez-vous. Ce qui est gardé :

1. deux sources lentes tournent EN MÊME TEMPS (un rendez-vous à deux ne s'atteint pas
   en série) ;
2. la fusion ne dépend pas de qui finit le premier : même réponse qu'en série ;
3. le vecteur de la requête est attendu PENDANT les sources lexicales, pas avant ;
   sans vecteur, aucune source sémantique n'est jouée ;
4. la concurrence suit la taille du pool, et retombe à un sous une portée de connexion
   partagée ;
5. une source qui lève fait lever la recherche, avec son exception ;
6. sur la capacité, l'embedding et le lexical se recouvrent.
"""
from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from oto_mcp import db, search as S

_SOURCES_PID = ("search_docs_fts", "search_project_briefs", "search_files_meta",
                "search_file_contents")
_SEMANTIQUES = ("search_docs_semantic", "search_briefs_semantic", "search_guides_semantic")


def _hit(i: int) -> dict:
    return {"id": i, "project_id": 1, "title": f"P{i}", "name": f"P{i}", "filename": f"f{i}",
            "headline": "<b>x</b>"}


@pytest.fixture
def sources(monkeypatch):
    """Toutes les sources vides et instantanées ; le banc remplace celles qu'il éprouve."""
    for n in _SOURCES_PID:
        monkeypatch.setattr(db, n, lambda q, pids, limit: [])
    monkeypatch.setattr(db, "search_procedures_fts", lambda q, org, limit: [])
    monkeypatch.setattr(db, "search_guides_fts", lambda q, org, sub, limit: [])
    for n in _SEMANTIQUES:
        monkeypatch.setattr(db, n, lambda *a, **k: [])
    monkeypatch.setattr(S.ownership, "accessible_project_ids", lambda *a, **k: [1])
    monkeypatch.setattr(S.ownership, "perso_de_la_liste", lambda *a: False)
    monkeypatch.setattr(S, "_accessible_namespaces", lambda *a: [])
    monkeypatch.setattr(S, "_stamp_origins", lambda *a, **k: None)
    monkeypatch.setattr(db, "project_labels", lambda ids: {})
    monkeypatch.setattr(db, "taille_du_pool", lambda: 24)
    return monkeypatch


def test_deux_sources_lentes_tournent_en_meme_temps(sources):
    rdv = threading.Barrier(2, timeout=5)

    def _attend(q, pids, limit):
        rdv.wait()              # en série, la première attendrait la seconde pour toujours
        return []
    sources.setattr(db, "search_docs_fts", _attend)
    sources.setattr(db, "search_file_contents", _attend)
    S.search("u", 7, "compte rendu", kinds=["page", "fichier"])
    assert not rdv.broken


def test_la_fusion_ne_depend_pas_de_qui_finit_le_premier(sources):
    """La page (source lente) est fusionnée AVANT le fichier (source rapide), comme en
    série : à score égal, le RRF garde l'ordre des sources."""
    def _lente(q, pids, limit):
        time.sleep(0.2)
        return [_hit(10), _hit(11)]
    sources.setattr(db, "search_docs_fts", _lente)
    sources.setattr(db, "search_file_contents", lambda q, pids, limit: [_hit(20)])
    parallele = S.search("u", 7, "compte rendu")["hits"]
    sources.setattr(db, "taille_du_pool", lambda: 1)       # concurrence 1 : en série
    assert S.concurrence() == 1
    serie = S.search("u", 7, "compte rendu")["hits"]
    assert [(h["kind"], h["ref"]) for h in parallele] == [
        ("page", 10), ("fichier", 20), ("page", 11)]
    assert parallele == serie


def test_le_vecteur_est_attendu_pendant_le_lexical(sources):
    lexical_parti = threading.Event()

    def _lexicale(q, pids, limit):
        lexical_parti.set()
        return [_hit(10)]
    semantiques = []
    sources.setattr(db, "search_docs_fts", _lexicale)
    sources.setattr(db, "search_docs_semantic",
                    lambda lit, pids, limit: semantiques.append(lit) or [_hit(30)])

    def _vecteur():
        # Le vecteur n'arrive qu'une fois le lexical parti : s'il était attendu AVANT,
        # le lexical ne partirait jamais et l'attente expirerait.
        assert lexical_parti.wait(timeout=5), "le lexical attendait le vecteur"
        return [0.5, 0.25]
    hits = S.search("u", 7, "compte rendu", kinds=["page"], query_embedding=_vecteur)["hits"]
    assert semantiques == ["[0.5,0.25]"]
    assert {h["matched_by"] for h in hits} == {"lexical", "semantic"}


def test_sans_vecteur_aucune_source_semantique(sources):
    def _interdite(*a, **k):
        raise AssertionError("source sémantique jouée sans vecteur")
    for n in _SEMANTIQUES:
        sources.setattr(db, n, _interdite)
    S.search("u", 7, "compte rendu", query_embedding=lambda: None)
    S.search("u", 7, "compte rendu", query_embedding=None)


@pytest.mark.parametrize("pool, attendu", [(1, 1), (8, 1), (16, 2), (24, 3), (32, 4),
                                           (200, 4)])
def test_la_concurrence_suit_la_taille_du_pool(sources, pool, attendu):
    sources.setattr(db, "taille_du_pool", lambda: pool)
    assert S.concurrence() == attendu


def test_une_portee_de_connexion_partagee_joue_les_sources_en_serie(sources):
    """`reuse_connection()` n'emprunte qu'à la première requête : l'entrer ne touche pas
    la base, et dedans, ses lectures passent toutes par UNE connexion."""
    assert S.concurrence() > 1
    with db.reuse_connection():
        assert S.concurrence() == 1


def test_une_source_qui_leve_fait_lever_la_recherche(sources):
    def _panne(q, pids, limit):
        raise RuntimeError("source en panne")
    sources.setattr(db, "search_file_contents", _panne)
    with pytest.raises(RuntimeError, match="source en panne"):
        S.search("u", 7, "compte rendu")


@pytest.mark.asyncio
async def test_la_capacite_recouvre_l_embedding_et_le_lexical(sources):
    from oto_mcp import embeddings
    from oto_mcp.capabilities import search as cap

    def _lente(q, pids, limit):
        time.sleep(0.4)
        return [_hit(10)]
    sources.setattr(db, "search_docs_fts", _lente)

    async def _vecteur(q):
        await asyncio.sleep(0.4)
        return [0.1, 0.2]
    sources.setattr(embeddings, "embed_query", _vecteur)
    sources.setattr(cap, "_preparer", lambda ctx, inp: ([], []))
    debut = time.perf_counter()
    rep = await cap._search(SimpleNamespace(sub="u", org_id=7),
                            cap.SearchInput(q="compte rendu", kinds=["page"]))
    duree = time.perf_counter() - debut
    assert [h["ref"] for h in rep["hits"]] == [10]
    assert duree < 0.7, f"{duree:.2f} s : l'embedding (0,4 s) et le lexical (0,4 s) en série"
