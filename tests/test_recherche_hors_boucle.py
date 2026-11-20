"""`me.search` ne tient plus la boucle pendant son SQL (gels de prod, 30/09/2026).

Le hang watch de la prod a pris 17 fois la boucle dans `db.search_docs_fts` : la
capacité est `async def` (pour l'embedding de la requête) et appelait la recherche
synchrone NÛMENT. Même banc que le lot 1 (`test_lot1_sql_hors_boucle.py`) : on observe
la boucle, pas le source — pendant une FTS qui dort 0,5 s, une tâche bat toutes les
10 ms ; boucle tenue, le compteur n'avance pas.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

_LECTURE_S = 0.5
_BATTEMENTS_MIN = 20          # 0,5 s / 10 ms = 50 attendus, boucle libre ; on exige 20


async def _battements_pendant(coro):
    ticks = 0

    async def _battement():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    tache = asyncio.create_task(_battement())
    await asyncio.sleep(0)
    try:
        rep = await coro
    finally:
        tache.cancel()
    return rep, ticks


def _lente(valeur):
    def _lecture(*_a, **_k):
        time.sleep(_LECTURE_S)               # le SQL synchrone
        return valeur
    return _lecture


@pytest.fixture
def recherche(monkeypatch):
    """`me.search` réduit aux pages d'un projet : la FTS des pages (lente), rien d'autre."""
    from oto_mcp import embeddings, ownership, search as search_mod
    from oto_mcp.capabilities import search as cap
    from oto_mcp.capabilities.connectors import selection

    fts = []

    def _fts(q, pids, *, limit=20):
        fts.append((q, pids))
        return _lente([{"id": 7, "project_id": 1, "title": "Compte rendu",
                        "description": None, "headline": "le <b>compte</b> rendu",
                        "updated_at": None}])()

    monkeypatch.setattr(ownership, "visible_in_org", lambda *a: True)
    monkeypatch.setattr(selection, "_visible_catalog", lambda ctx: [])
    monkeypatch.setattr(search_mod.db, "search_docs_fts", _fts)
    monkeypatch.setattr(search_mod.db, "project_labels", lambda ids: {})
    monkeypatch.setattr(search_mod.db, "sources_presentes", lambda pids: False)
    monkeypatch.setattr(search_mod, "_stamp_origins", lambda *a, **k: None)

    async def _sans_vecteur(q):
        return None
    monkeypatch.setattr(embeddings, "embed_query", _sans_vecteur)
    return cap, fts


@pytest.mark.asyncio
async def test_la_fts_des_pages_ne_gele_pas_la_boucle(recherche):
    cap, fts = recherche
    ctx = SimpleNamespace(sub="sub-x", org_id=1)
    inp = cap.SearchInput(q="compte rendu", scope="project", project=1, kinds=["page"])
    rep, ticks = await _battements_pendant(cap._search(ctx, inp))
    assert fts == [("compte rendu", [1])], "la FTS n'a pas été jouée : la garde serait inerte"
    assert [h["ref"] for h in rep["hits"]] == [7]
    assert ticks >= _BATTEMENTS_MIN, (
        f"la boucle n'a battu que {ticks} fois pendant une FTS de {_LECTURE_S} s "
        f"(≥ {_BATTEMENTS_MIN} attendus) : la recherche tourne dans la boucle — le "
        "serveur est mono-loop, tout gèle (docs/event-loop-perf.md)")


@pytest.mark.asyncio
async def test_le_projet_invisible_est_refuse_depuis_le_thread(recherche, monkeypatch):
    """Le refus neutre, levé dans le thread, remonte tel quel à l'appelant."""
    from oto_mcp import ownership
    from oto_mcp.capabilities._types import AuthzDenied
    cap, fts = recherche
    monkeypatch.setattr(ownership, "visible_in_org", lambda *a: False)
    inp = cap.SearchInput(q="compte rendu", scope="project", project=9, kinds=["page"])
    with pytest.raises(AuthzDenied) as e:
        await cap._search(SimpleNamespace(sub="sub-x", org_id=1), inp)
    assert e.value.code == "unknown_project" and fts == []
