"""Lint KB (oto/#6 B1)."""
from oto_mcp import doc_lint


DOCS = [
    {"id": 1, "title": "Marché A", "body_md": "contenu suffisant ici.", "updated_at": "2026-07-20 10:00:00"},
    {"id": 2, "title": "Marché A", "body_md": "autre contenu long.", "updated_at": "2026-07-21 10:00:00"},  # doublon titre
    {"id": 3, "title": "Vide", "body_md": "  ", "updated_at": "2026-07-22 10:00:00"},                       # vide
    {"id": 4, "title": "Vieux", "body_md": "du contenu ancien mais présent.", "updated_at": "2026-01-01 10:00:00"},  # stale
]


def test_flags_stale_empty_and_duplicates():
    out = doc_lint.lint_docs(DOCS, stale_before="2026-06-01 00:00:00")
    assert [d["id"] for d in out["stale"]] == [4]
    assert [d["id"] for d in out["empty"]] == [3]
    dups = out["duplicate_titles"]
    assert len(dups) == 1 and set(dups[0]["ids"]) == {1, 2}
    assert out["count"] == 3


def test_no_stale_check_without_cutoff():
    out = doc_lint.lint_docs(DOCS)          # pas de borne → aucun stale
    assert out["stale"] == []


def test_duplicate_is_case_and_space_insensitive():
    docs = [{"id": 1, "title": "Le  Marché", "body_md": "xxxxxxxxxx"},
            {"id": 2, "title": "le marché", "body_md": "yyyyyyyyyy"}]
    out = doc_lint.lint_docs(docs)
    assert len(out["duplicate_titles"]) == 1 and set(out["duplicate_titles"][0]["ids"]) == {1, 2}


def test_clean_project_is_empty():
    docs = [{"id": 1, "title": "Unique", "body_md": "assez de contenu ici.", "updated_at": "2026-07-22 10:00:00"}]
    out = doc_lint.lint_docs(docs, stale_before="2026-01-01 00:00:00")
    assert out["count"] == 0
