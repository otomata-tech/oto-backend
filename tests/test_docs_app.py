"""`oto_doc_app` (MCP App) — lecture/parcours rendu des pages d'un projet.

Même patron que test_datastore_app_v2 : prefab_ui STUBBÉ par des composants
enregistreurs, db/ownership/access stubbés (pas de DB), et on exerce la vraie
closure. On prouve : l'arbre indente les enfants sous leur parent (DFS), la vue
page rend le markdown, le défaut sans args résout la KB de l'org active, et un
projet non lisible rend une carte message (jamais une fuite).
"""
import sys
import types

import pytest


# ── stub prefab_ui.components : composants enregistreurs (pile de contexte) ─────
_STACK: list = []


class _Node:
    def __init__(self, kind, text=None, **attrs):
        self.kind = kind
        self.text = text
        self.attrs = attrs
        self.children: list = []
        if _STACK:
            _STACK[-1].children.append(self)

    def __enter__(self):
        _STACK.append(self)
        return self

    def __exit__(self, *exc):
        _STACK.pop()
        return False

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()

    def texts(self):
        return [n.text for n in self.walk()
                if n.kind in ("Heading", "Text", "Markdown") and n.text]

    def tables(self):
        return [n for n in self.walk() if n.kind == "DataTable"]


def _install_prefab_stub():
    mod = types.ModuleType("prefab_ui")
    comp = types.ModuleType("prefab_ui.components")

    def Card(**k):
        return _Node("Card", **k)

    def Column(**k):
        return _Node("Column", **k)

    def Heading(text=None, **k):
        return _Node("Heading", text=text, **k)

    def Text(text=None, **k):
        return _Node("Text", text=text, **k)

    def Markdown(text=None, **k):
        return _Node("Markdown", text=text, **k)

    class DataTableColumn:
        def __init__(self, key=None, header=None, **k):
            self.key, self.header = key, header

    def DataTable(columns=None, rows=None, **k):
        return _Node("DataTable", columns=columns or [], rows=rows or [], **k)

    comp.Card, comp.Column, comp.Heading = Card, Column, Heading
    comp.Text, comp.Markdown = Text, Markdown
    comp.DataTable, comp.DataTableColumn = DataTable, DataTableColumn
    mod.components = comp
    sys.modules["prefab_ui"] = mod
    sys.modules["prefab_ui.components"] = comp


DOCS = [
    {"id": 1, "project_id": 7, "parent_id": None, "title": "Racine", "kind": "doc",
     "updated_at": "2026-07-01 10:00:00", "body_md": "# Racine", "public_token": None},
    {"id": 2, "project_id": 7, "parent_id": 1, "title": "Enfant", "kind": "note",
     "updated_at": "2026-07-02 10:00:00", "body_md": "corps **gras**", "public_token": None},
    {"id": 3, "project_id": 7, "parent_id": None, "title": "Autre racine", "kind": "doc",
     "updated_at": "2026-07-03 10:00:00", "body_md": "", "public_token": None},
]


@pytest.fixture
def doc_app(monkeypatch):
    _install_prefab_stub()
    _STACK.clear()
    sys.modules.pop("oto_mcp.tools.docs_app", None)
    import oto_mcp.tools.docs_app as da

    captured = {}

    class _FakeMcp:
        def tool(self, *a, **k):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    monkeypatch.setattr(da.access, "current_user_sub_or_raise", lambda: "sub1")
    monkeypatch.setattr(da.access, "current_org", lambda sub: 42)
    monkeypatch.setattr(da.ownership, "can_access",
                        lambda sub, rt, rid, want: rid == "7")
    monkeypatch.setattr(da.db, "get_project_by_id",
                        lambda pid: {"id": pid, "name": "KB test"} if pid == 7 else None)
    monkeypatch.setattr(da.db, "list_projects_for_owners",
                        lambda owners: [{"id": 7, "name": "Base de connaissance"}])
    monkeypatch.setattr(da.db, "list_docs_for_project",
                        lambda pid: list(DOCS) if pid == 7 else [])
    monkeypatch.setattr(da.db, "get_doc_by_id",
                        lambda did: next((d for d in DOCS if d["id"] == did), None))
    monkeypatch.setattr(da.db, "search_docs_in_project",
                        lambda pid, q, **k: [{"id": 2, "title": "Enfant", "kind": "note",
                                              "snippet": "corps <b>gras</b>"}])
    da.register(_FakeMcp())
    assert "oto_doc_app" in captured, "oto_doc_app doit s'enregistrer avec le stub prefab_ui"
    return captured["oto_doc_app"]


def test_tree_indents_children_under_parent(doc_app):
    card = doc_app(project_id=7)
    tables = card.tables()
    assert len(tables) == 1
    pages = [r["page"] for r in tables[0].attrs["rows"]]
    # DFS : l'enfant suit sa racine, indenté ; l'autre racine vient après.
    assert pages[0] == "Racine"
    assert pages[1].endswith("└ Enfant") and pages[1] != "└ Enfant"
    assert pages[2] == "Autre racine"


def test_page_view_renders_markdown(doc_app):
    card = doc_app(doc_id=2)
    kinds = [n.kind for n in card.walk()]
    assert "Markdown" in kinds
    md = next(n for n in card.walk() if n.kind == "Markdown")
    assert md.text == "corps **gras**"
    assert "Enfant" in card.texts()[0]


def test_default_resolves_active_org_kb(doc_app):
    card = doc_app()
    assert card.texts()[0] == "KB test"      # la KB (projet 7) a été résolue
    assert len(card.tables()) == 1


def test_unreadable_project_yields_message_card(doc_app):
    card = doc_app(project_id=99)
    assert "Projet introuvable" in card.texts()
    assert card.tables() == []


def test_search_strips_headline_markup(doc_app):
    card = doc_app(project_id=7, query="gras")
    rows = card.tables()[0].attrs["rows"]
    assert rows[0]["extrait"] == "corps gras"
