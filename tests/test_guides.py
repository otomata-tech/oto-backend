"""Guides d'usage (oto-backend#111, tout-DB 2026-07-16) : seeds fichiers + tool oto_guide.

Les fichiers `guides/*.md` ne sont plus la surface de lecture : ils sont les SEEDS
du boot (`seed_platform_guides`, idempotent). Ici on prouve le parsing, la lecture
des seeds livrés, et le seed idempotent (DO NOTHING simulé).
"""
from oto_mcp import guide_store as G


# ── parsing front-matter ──

def test_parse_with_front_matter():
    meta, body = G._parse("---\ntitle: T\ndescription: D\n---\nle corps")
    assert meta == {"title": "T", "description": "D"} and body == "le corps"


def test_parse_without_front_matter():
    meta, body = G._parse("juste du texte")
    assert meta == {} and body == "juste du texte"


# ── seeds sur le vrai dossier guides/ (bulk-load.md, mcp-apps.md livrés) ──

def test_file_seeds_include_shipped_guides():
    by_slug = {g["slug"]: g for g in G.list_file_guides()}
    assert "bulk-load" in by_slug and "mcp-apps" in by_slug
    g = by_slug["bulk-load"]
    assert g["title"] and g["description"]            # front-matter lu
    assert "sous-agent" in g["body_md"]
    assert not g["body_md"].startswith("---")         # front-matter retiré


def test_seed_platform_guides_never_overwrites(monkeypatch):
    seeded = []

    def fake_seed(scope, owner, slug, body_md, title="", description=""):
        seeded.append((scope, owner, slug))

    import oto_mcp.db as db
    monkeypatch.setattr(db, "seed_guide_db", fake_seed)
    G.seed_platform_guides()
    # tous les fichiers passent par le seed DO-NOTHING, scope/owner plateforme
    assert ("platform", G.PLATFORM_OWNER, "bulk-load") in seeded
    assert ("platform", G.PLATFORM_OWNER, "mcp-apps") in seeded


def test_index_lists_guides(monkeypatch):
    import oto_mcp.db as db
    monkeypatch.setattr(db, "list_guides_db",
                        lambda scope, owner: [{"slug": "bulk-load", "title": "T",
                                               "description": "D"}]
                        if scope == "platform" else [])
    idx = G.guides_index_md()
    assert "bulk-load" in idx and "oto_guide" in idx


# ── enregistrement du tool ──

def test_tool_registers_on_fastmcp():
    from fastmcp import FastMCP
    from oto_mcp.tools import guide
    mcp = FastMCP("probe")
    guide.register(mcp)   # ne lève pas ; l'index est ajouté par le middleware, pas ici
