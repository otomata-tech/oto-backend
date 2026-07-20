"""Unités de la recherche transverse (lot 3 Ship 1) : fusion RRF, matchers en
mémoire (tableaux/connecteurs, repli d'accents), garde du headline, erreurs de
la capacité. Logique pure — sources db stubées."""
import asyncio
import pytest

from oto_mcp import search as S
from oto_mcp.capabilities import search as CAP
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx


def _stub_empty(monkeypatch, **over):
    """Toutes les sources vides sauf `over` (nom db → rows)."""
    names = ("search_docs_fts", "search_project_briefs", "search_procedures_fts",
             "search_guides_fts", "search_files_meta")
    sigs = {"search_procedures_fts": lambda q, org, limit: [],
            "search_guides_fts": lambda q, org, sub, limit: []}
    for n in names:
        rows = over.get(n, [])
        if n in sigs and n not in over:
            monkeypatch.setattr(S.db, n, sigs[n])
        elif n == "search_procedures_fts":
            monkeypatch.setattr(S.db, n, lambda q, org, limit, _r=rows: list(_r))
        elif n == "search_guides_fts":
            monkeypatch.setattr(S.db, n, lambda q, org, sub, limit, _r=rows: list(_r))
        else:
            monkeypatch.setattr(S.db, n, lambda q, pids, limit, _r=rows: list(_r))
    monkeypatch.setattr(S.ownership, "accessible_project_ids", lambda *a, **k: [1])
    monkeypatch.setattr(S.ownership, "active_org_principals", lambda *a: [])
    monkeypatch.setattr(S.db, "list_datastore_namespaces_for_owners",
                        lambda owners: over.get("tableaux", []))
    monkeypatch.setattr(S.db, "list_datastore_namespaces_granted_to",
                        lambda *a: [])
    monkeypatch.setattr(S.db, "project_names", lambda ids: {1: "Projet X"})


def test_rrf_interleaves_sources(monkeypatch):
    # rang 1 de chaque source AVANT rang 2 d'une autre (fusion par rang, pas score).
    _stub_empty(monkeypatch,
                search_docs_fts=[
                    {"id": 10, "project_id": 1, "title": "Page A", "headline": "<b>x</b>"},
                    {"id": 11, "project_id": 1, "title": "Page B", "headline": "<b>x</b>"}],
                search_procedures_fts=[
                    {"slug": "proc-a", "title": "Proc A", "description": "", "headline": None}])
    out = S.search("u1", 7, "x y")
    kinds = [(h["kind"], h["ref"]) for h in out["hits"]]
    assert kinds.index(("procedure", "proc-a")) < kinds.index(("page", 11))


def test_project_names_attached(monkeypatch):
    _stub_empty(monkeypatch, search_docs_fts=[
        {"id": 10, "project_id": 1, "title": "Page A", "headline": "<b>x</b>"}])
    out = S.search("u1", 7, "xx")
    assert out["hits"][0]["project_name"] == "Projet X"


def test_headline_without_highlight_dropped(monkeypatch):
    # Match venu du folding seul → ts_headline brut sans <b> → pas de passage affiché.
    _stub_empty(monkeypatch, search_docs_fts=[
        {"id": 10, "project_id": 1, "title": "Décideur", "headline": "texte sans marque"}])
    out = S.search("u1", 7, "decideur")
    assert out["hits"][0]["passage"] is None


def test_zero_hits_carries_hint(monkeypatch):
    _stub_empty(monkeypatch)
    out = S.search("u1", 7, "zzz introuvable")
    assert out["hits"] == [] and "reformule" in out["hint"]


def test_kinds_filters_sources(monkeypatch):
    called = []
    _stub_empty(monkeypatch)
    monkeypatch.setattr(S.db, "search_docs_fts",
                        lambda q, pids, limit: called.append("docs") or [])
    monkeypatch.setattr(S.db, "search_procedures_fts",
                        lambda q, org, limit: called.append("proc") or [])
    S.search("u1", 7, "xx", kinds=["procedure"])
    assert called == ["proc"]


def test_match_tableaux_ranking():
    rows = [
        {"id": 1, "namespace": "prospects", "schema": {}},
        {"id": 2, "namespace": "vieux-prospects-2024", "schema": {}},
        {"id": 3, "namespace": "clients", "schema": {"fields": [{"label": "Prospects chauds"}]}},
        {"id": 4, "namespace": "autre", "schema": {}},
    ]
    import unittest.mock as m
    with m.patch.object(S.ownership, "active_org_principals", return_value=[]), \
         m.patch.object(S.db, "list_datastore_namespaces_for_owners", return_value=rows), \
         m.patch.object(S.db, "list_datastore_namespaces_granted_to", return_value=[]):
        out = S._match_tableaux("Prospects", "u1", 7)
    assert [h["ref"] for h in out] == [1, 2, 3]   # exact > partiel > label ; 4 exclu


def test_match_connectors_accent_fold_and_rank():
    catalog = [
        {"name": "serper", "label": "Serper", "help": "recherche web"},
        {"name": "sirene", "label": "INSEE SIRENE", "help": "données entreprise FR"},
    ]
    out = S._match_connectors("donnees entreprise", catalog)
    assert [h["ref"] for h in out] == ["sirene"]
    out2 = S._match_connectors("serper", catalog)
    assert out2[0]["ref"] == "serper"


# ── capacité : erreurs ───────────────────────────────────────────────────────

def test_cap_no_org():
    with pytest.raises(AuthzDenied) as e:
        asyncio.run(CAP._search(ResolvedCtx(sub="u1", org_id=None), CAP.SearchInput(q="xx")))
    assert e.value.code == "no_active_org"


def test_cap_query_too_short():
    with pytest.raises(AuthzDenied) as e:
        asyncio.run(CAP._search(ResolvedCtx(sub="u1", org_id=7), CAP.SearchInput(q="a")))
    assert e.value.code == "query_too_short"


def test_cap_project_scope_requires_project():
    with pytest.raises(AuthzDenied) as e:
        asyncio.run(CAP._search(ResolvedCtx(sub="u1", org_id=7),
                    CAP.SearchInput(q="xx", scope="project")))
    assert e.value.code == "project_required"


def test_cap_foreign_project_neutral_refusal(monkeypatch):
    monkeypatch.setattr(CAP.ownership, "visible_in_org", lambda *a: False)
    with pytest.raises(AuthzDenied) as e:
        asyncio.run(CAP._search(ResolvedCtx(sub="u1", org_id=7),
                    CAP.SearchInput(q="xx", scope="project", project=99)))
    assert e.value.code == "unknown_project"


def test_cap_kinds_csv_and_validation():
    inp = CAP.SearchInput(q="xx", kinds="page,tableau")
    assert inp.kinds == ["page", "tableau"]
    with pytest.raises(Exception):
        CAP.SearchInput(q="xx", kinds="page,licorne")
    assert CAP.SearchInput(q="xx", limit=500).limit == 50   # cap


def test_doc_op_search_reroutes_to_single_path(monkeypatch):
    # DÉPRÉCIATION (Ship 1) : oto_doc(op=search) délègue au chemin unique.
    import oto_mcp.search as S2
    from oto_mcp.capabilities import docs as D
    monkeypatch.setattr(D, "_can", lambda sub, pid, want: True)
    monkeypatch.setattr(S2, "search", lambda sub, org, q, **k: {
        "hits": [{"kind": "page", "ref": 5, "project_id": 3, "title": "T",
                  "passage": "<b>x</b>", "updated_at": "2026-01-01"}]})
    out = D._doc(ResolvedCtx(sub="u1", org_id=7),
                 D.DocInput(op="search", project_id=3, query="x"))
    assert out["deprecated"]
    assert out["results"][0]["id"] == 5 and out["results"][0]["snippet"] == "<b>x</b>"
