"""Les sources brutes d'un projet (`docs.kind='source'`) traitées à part des pages de
travail (`doc`, `note`), sur base RÉELLE et par les chemins servis :

- `oto_search` : `page` ne parcourt plus les sources ; `source` est un genre à la
  demande ; sans lui, une réponse dont le périmètre en porte le DIT (`sources_skipped`) ;
- `oto_doc op=list` : l'arbre de travail par défaut, qui compte les sources laissées
  (`sources_hidden`) et garde une source ancêtre d'une page de travail ; `kind="source"`
  liste les sources seules, les plus récentes d'abord, par pages, avec leur total ;
- les index partiels sont posés au démarrage d'une base neuve, et la recherche des pages
  de travail les emprunte.

Le défaut de la recherche (genres omis) et la validation des genres sont tenus par
`tests/test_search_unit.py`, sources doublées.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from oto_mcp import db, search as S
from oto_mcp.db import search as DS
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
from oto_mcp.capabilities.docs import core as D

MOI = "u-sources-a-part"
MOT = f"verse{uuid.uuid4().hex[:8]}"
CTX = ResolvedCtx(sub=MOI, org_id=None)


def _doc(**kw):
    return D._doc(CTX, D.DocInput(**kw))


def _dater(doc_id: int, iso: str) -> None:
    with db._connect() as conn:
        conn.execute("UPDATE docs SET created_at = %s WHERE id = %s", (iso, doc_id))


@pytest.fixture(scope="module")
def projet(live):
    db.upsert_user(MOI, email=f"{MOI}@exemple.test", name=MOI)
    pid = int(db.create_project("user", MOI, f"Mission {MOT}", created_by=MOI))
    page = db.create_doc(pid, f"Synthèse {MOT}", body_md=f"Ce que retient {MOT}",
                         created_by=MOI)
    note = db.create_doc(pid, f"Note {MOT}", body_md=f"Relevé {MOT}", kind="note",
                         created_by=MOI)
    sources = [db.create_doc(pid, f"Appel {i}", body_md=f"Transcription {MOT} n°{i}",
                             kind="source", created_by=MOI) for i in range(5)]
    for i, sid in enumerate(sources):
        _dater(sid, f"2026-11-0{i + 1}T10:00:00+00:00")
    # Une page de travail rangée SOUS une source : l'arbre la garde avec son parent.
    parent = db.create_doc(pid, "Import d'origine", body_md="brut", kind="source",
                           created_by=MOI)
    _dater(parent, "2026-10-01T10:00:00+00:00")
    enfant = db.create_doc(pid, f"Analyse {MOT}", parent_id=parent,
                           body_md=f"Lecture de {MOT}", created_by=MOI)
    return {"pid": pid, "page": page, "note": note, "sources": sources,
            "parent": parent, "enfant": enfant}


def _cherche(p, kinds):
    return S.search(MOI, 0, MOT, scope="project", project_id=p["pid"], kinds=kinds)


def test_page_ne_parcourt_plus_les_sources_et_le_dit(projet):
    out = _cherche(projet, ["page"])
    assert {h["kind"] for h in out["hits"]} == {"page"}
    assert {h["ref"] for h in out["hits"]} == {projet["page"], projet["note"],
                                                projet["enfant"]}
    assert out["sources_skipped"] is True
    assert "kinds=['source']" in out["hint_sources"]


def test_source_a_la_demande_seulement(projet):
    out = _cherche(projet, ["source"])
    assert {h["kind"] for h in out["hits"]} == {"source"}
    assert {h["ref"] for h in out["hits"]} == set(projet["sources"])
    assert all(h["project_id"] == projet["pid"] for h in out["hits"])
    assert "sources_skipped" not in out and "hint_sources" not in out


def test_page_et_source_ensemble(projet):
    out = _cherche(projet, ["page", "source"])
    par_genre = {}
    for h in out["hits"]:
        par_genre.setdefault(h["kind"], set()).add(h["ref"])
    assert par_genre["source"] == set(projet["sources"])
    assert projet["page"] in par_genre["page"]
    assert "sources_skipped" not in out


def test_sans_source_dans_le_perimetre_aucun_signal(live):
    pid = int(db.create_project("user", MOI, f"Sans source {MOT}", created_by=MOI))
    db.create_doc(pid, f"Seule {MOT}", body_md=MOT, created_by=MOI)
    out = S.search(MOI, 0, MOT, scope="project", project_id=pid, kinds=["page"])
    assert out["count"] == 1 and "sources_skipped" not in out
    assert db.sources_presentes([pid]) is False


def test_une_source_archivee_ne_compte_pas(live):
    pid = int(db.create_project("user", MOI, f"Archivée {MOT}", created_by=MOI))
    sid = db.create_doc(pid, "Ancienne", body_md=MOT, kind="source", created_by=MOI)
    assert db.sources_presentes([pid]) is True
    _doc(op="archive", doc_id=sid)
    assert db.sources_presentes([pid]) is False


def test_le_semantique_des_pages_ecarte_les_sources(projet):
    """Le kNN filtre le genre comme le lexical (même vecteur pour toutes les pages)."""
    vec = "[" + ",".join(["0.1"] * 1024) + "]"
    with db._connect() as conn:
        for did in (projet["page"], *projet["sources"]):
            conn.execute(
                "INSERT INTO doc_embeddings (doc_id, content_sha, embedding, model) "
                "VALUES (%s, 'x', %s::halfvec, 'test') ON CONFLICT (doc_id) DO NOTHING",
                (did, vec))
    trouvees = {r["id"] for r in db.search_docs_semantic(vec, [projet["pid"]], limit=50)}
    assert projet["page"] in trouvees
    assert not trouvees & set(projet["sources"])


def test_liste_par_defaut_l_arbre_de_travail(projet):
    out = _doc(op="list", project_id=projet["pid"])
    ids = {d["id"] for d in out["docs"]}
    assert {projet["page"], projet["note"], projet["enfant"]} <= ids
    assert not ids & set(projet["sources"])
    # La source parente d'une page de travail reste : l'arbre est entier.
    assert projet["parent"] in ids
    assert out["sources_hidden"] == len(projet["sources"])


def test_liste_d_un_genre_de_travail(projet):
    out = _doc(op="list", project_id=projet["pid"], kind="note")
    assert [d["id"] for d in out["docs"]] == [projet["note"]]
    assert "sources_hidden" not in out


def test_liste_des_sources_paginee_plus_recentes_d_abord(projet):
    attendu = [projet["sources"][i] for i in (4, 3, 2, 1, 0)] + [projet["parent"]]
    vus, cursor, pages = [], None, 0
    while True:
        out = _doc(op="list", project_id=projet["pid"], kind="source", limit=2,
                   **({"cursor": cursor} if cursor else {}))
        pages += 1
        assert out["kind"] == "source" and out["total"] == len(attendu)
        assert all("body_md" not in d for d in out["docs"]), "l'index, pas les corps"
        vus += [d["id"] for d in out["docs"]]
        cursor = out["next_cursor"]
        if cursor is None:
            break
    assert vus == attendu and pages == 3


def test_liste_des_sources_sur_une_periode(projet):
    out = _doc(op="list", project_id=projet["pid"], kind="source",
               since="2026-11-02", until="2026-11-04T00:00:00Z")
    assert [d["id"] for d in out["docs"]] == [projet["sources"][2], projet["sources"][1]]
    assert out["total"] == 2 and out["next_cursor"] is None


def test_liste_des_sources_refus_nommes(projet):
    with pytest.raises(AuthzDenied) as e:
        _doc(op="list", project_id=projet["pid"], kind="source", cursor="abime")
    assert e.value.code == "invalid_cursor"
    with pytest.raises(AuthzDenied) as e:
        _doc(op="list", project_id=projet["pid"], kind="source", since="hier")
    assert e.value.code == "invalid_period"
    with pytest.raises(AuthzDenied) as e:
        _doc(op="list", project_id=projet["pid"], kind="source", limit=500)
    assert e.value.code == "invalid_limit"
    # La pagination n'appartient qu'à la liste des sources.
    for kw in ({"limit": 5}, {"cursor": "x"}, {"since": "2026-11-01"}):
        with pytest.raises(AuthzDenied) as e:
            _doc(op="list", project_id=projet["pid"], **kw)
        assert e.value.code == "unsupported_pagination"


def test_index_partiels_poses_et_empruntes(projet):
    noms = ("idx_docs_travail_fts", "idx_docs_travail_trgm", "idx_docs_travail_projet",
            "idx_docs_sources_recentes")
    with db._connect() as conn:
        valides = {r["relname"] for r in conn.execute(
            "SELECT c.relname FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE c.relname = ANY(%s) AND i.indisvalid", (list(noms),)).fetchall()}
        assert valides == set(noms)
        # Le filtre plein texte des pages de TRAVAIL, sans autre prédicat indexable :
        # le planificateur doit pouvoir le servir par le GIN partiel (prédicat impliqué).
        conn.execute("SET LOCAL enable_seqscan = off")
        plan = "\n".join(r["QUERY PLAN"] for r in conn.execute(
            "EXPLAIN SELECT id FROM docs WHERE "
            f"{DS._vec(DS.DOCS_TEXT)} @@ plainto_tsquery('french', %s) "
            f"AND archived_at IS NULL AND {DS.PAGES_DE_TRAVAIL}", (MOT,)).fetchall())
    assert "idx_docs_travail_fts" in plan, plan
