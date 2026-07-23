"""Recherche sémantique (lot 3 V2) — fusion RRF lexical+sémantique (dédup + somme
des rangs), worker d'indexation idempotent, dégradation gracieuse sans embedding.
DB stubée ; l'API Mistral n'est jamais appelée."""
import pytest

from oto_mcp import search as S, embed_worker as W, embeddings as E


def _stub_sources(monkeypatch, *, lexical=(), semantic=()):
    monkeypatch.setattr(S.ownership, "accessible_project_ids", lambda *a, **k: [1])
    monkeypatch.setattr(S.db, "search_docs_fts",
                        lambda q, pids, limit: [dict(r) for r in lexical])
    monkeypatch.setattr(S.db, "search_docs_semantic",
                        lambda ql, pids, limit: [dict(r) for r in semantic])
    for n in ("search_project_briefs", "search_procedures_fts", "search_guides_fts",
              "search_files_meta", "search_briefs_semantic", "search_guides_semantic"):
        monkeypatch.setattr(S.db, n, lambda *a, **k: [])
    monkeypatch.setattr(S.ownership, "active_org_principals", lambda *a: [])
    monkeypatch.setattr(S.db, "list_datastore_namespaces_for_owners", lambda o: [])
    monkeypatch.setattr(S.db, "list_datastore_namespaces_granted_to", lambda *a: [])
    monkeypatch.setattr(S.db, "project_names", lambda ids: {})


def _page(id, title, headline=None):
    return {"id": id, "project_id": 1, "title": title, "headline": headline}


def test_page_found_by_both_fuses_and_ranks_first(monkeypatch):
    # Page 10 : rang 1 lexical ET rang 1 sémantique → somme → devant la page 11 (lexical seul).
    _stub_sources(monkeypatch,
                  lexical=[_page(10, "A", "<b>x</b>"), _page(11, "B", "<b>x</b>")],
                  semantic=[_page(10, "A")])
    out = S.search("u1", 7, "requête", query_embedding=[0.1] * 3)
    ids = [h["ref"] for h in out["hits"]]
    assert ids[0] == 10                       # cumulé lexical+sémantique
    assert ids.count(10) == 1                 # dédupliqué (pas 2 entrées)
    # le passage lexical est conservé sur la ligne fusionnée
    assert next(h for h in out["hits"] if h["ref"] == 10)["passage"] == "<b>x</b>"


def test_semantic_only_page_appears(monkeypatch):
    # Une page trouvée UNIQUEMENT par le sens (aucun mot exact) remonte quand même.
    _stub_sources(monkeypatch, lexical=[], semantic=[_page(20, "Dirigeant")])
    out = S.search("u1", 7, "décideur", query_embedding=[0.2] * 3)
    assert [h["ref"] for h in out["hits"]] == [20]
    assert out["hits"][0]["matched_by"] == "semantic"


def test_no_embedding_is_lexical_only(monkeypatch):
    called = {"sem": False}
    _stub_sources(monkeypatch, lexical=[_page(10, "A", "<b>x</b>")])
    monkeypatch.setattr(S.db, "search_docs_semantic",
                        lambda *a, **k: called.update(sem=True) or [])
    out = S.search("u1", 7, "x", query_embedding=None)     # pas de vecteur
    assert called["sem"] is False and [h["ref"] for h in out["hits"]] == [10]


# ── worker d'indexation ──────────────────────────────────────────────────────

def test_worker_skips_unchanged_sha(monkeypatch):
    rows = [{"id": 1, "text": "contenu"}, {"id": 2, "text": "autre"}]
    monkeypatch.setattr(W.db, "list_dirty_docs", lambda n: rows)
    # doc 1 : sha identique → dé-marqué sans ré-embed ; doc 2 : nouveau → embed.
    monkeypatch.setattr(W.db, "get_doc_embedding_sha",
                        lambda did: W._sha("contenu") if did == 1 else None)
    cleared, upserts, embedded = [], [], []
    monkeypatch.setattr(W.db, "clear_embed_dirty", lambda did: cleared.append(did))
    monkeypatch.setattr(W.db, "upsert_doc_embedding",
                        lambda did, sha, lit, model: upserts.append(did))
    monkeypatch.setattr(W.embeddings, "embed_texts",
                        lambda texts: embedded.append(texts) or [[0.0] * 3])
    n = W._index_batch()
    assert cleared == [1]                      # inchangé → juste dé-marqué
    assert embedded == [["autre"]]             # seul le doc 2 embed
    assert upserts == [2] and n == 2


def test_worker_network_error_leaves_dirty(monkeypatch):
    monkeypatch.setattr(W.db, "list_dirty_docs", lambda n: [{"id": 1, "text": "x"}])
    monkeypatch.setattr(W.db, "get_doc_embedding_sha", lambda did: None)
    def _boom(texts):
        raise RuntimeError("API 503")
    monkeypatch.setattr(W.embeddings, "embed_texts", _boom)
    monkeypatch.setattr(W.db, "upsert_doc_embedding", lambda *a: pytest.fail("ne doit pas upsert"))
    assert W._index_batch() == 0               # rien fait → la page reste dirty


def test_to_pg_literal():
    assert E.to_pg([1.0, 2.5, -3.0]).startswith("[1.0,2.5,")
