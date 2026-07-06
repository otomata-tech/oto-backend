"""Guides d'usage (oto-backend#111) : store fichiers + tool oto_guide."""
from oto_mcp import guide_store as G


# ── parsing front-matter ──

def test_parse_with_front_matter():
    meta, body = G._parse("---\ntitle: T\ndescription: D\n---\nle corps")
    assert meta == {"title": "T", "description": "D"} and body == "le corps"


def test_parse_without_front_matter():
    meta, body = G._parse("juste du texte")
    assert meta == {} and body == "juste du texte"


# ── store sur le vrai dossier guides/ (bulk-load.md livré) ──

def test_list_includes_bulk_load():
    slugs = {g["slug"] for g in G.list_guides()}
    assert "bulk-load" in slugs
    g = next(g for g in G.list_guides() if g["slug"] == "bulk-load")
    assert g["title"] and g["description"]        # front-matter lu


def test_read_returns_body_without_front_matter():
    g = G.read_guide("bulk-load")
    assert g and "sous-agent" in g["body_md"]
    assert not g["body_md"].startswith("---")     # front-matter retiré


def test_read_unknown_is_none():
    assert G.read_guide("nope-inexistant") is None


def test_slug_is_traversal_safe():
    assert G.read_guide("../secrets") is None      # slug strict → pas de path traversal
    assert G.read_guide("a/b") is None
    assert G._path_for("../etc/passwd") is None


def test_index_lists_guides():
    idx = G.guides_index_md()
    assert "bulk-load" in idx and "oto_guide" in idx


# ── enregistrement du tool ──

def test_tool_registers_on_fastmcp():
    from fastmcp import FastMCP
    from oto_mcp.tools import guide
    mcp = FastMCP("probe")
    guide.register(mcp)   # ne lève pas ; l'index est ajouté par le middleware, pas ici
