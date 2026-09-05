"""Contenu actif → blocs/rang frais, aucune recopie vers un ancien modèle."""
import pytest

from oto_mcp.db import guides, nodes, search


def _vector_is_current(conn, table, key):
    row = conn.execute(
        f"SELECT search_vec IS NOT NULL AND search_vec = {search._vec(search.RANKED_SOURCES[table])} AS fresh "
        f"FROM {table} WHERE id = %s", (key,),
    ).fetchone()
    assert row["fresh"] is True


async def _read_page(public_id):
    from oto_mcp.capabilities.node_view import _node, NodeInput
    from oto_mcp.capabilities._types import ResolvedCtx
    return await _node(ResolvedCtx(sub="projection-owner"), NodeInput(node_id=public_id))


def _public_id(conn, node_id):
    return conn.execute("SELECT public_id FROM nodes WHERE id = %s", (node_id,)).fetchone()["public_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ["on-demand", "init"])
async def test_guide_edit_is_immediately_readable_through_node_capability(base_fraiche, delivery):
    setter = guides.set_guide_db if delivery == "on-demand" else guides.set_init_guide_db
    row = setter("user", "projection-owner", "fresh", "# Initial\n\nAncien corps")
    public_id = _public_id(base_fraiche, row["id"])
    first = await _read_page(public_id)
    assert "".join(b.get("md", "") for b in first["body"]) == "# Initial\n\nAncien corps"
    setter("user", "projection-owner", "fresh", "# Nouveau\n\nCorps changé")
    current = await _read_page(public_id)
    assert "".join(b.get("md", "") for b in current["body"]) == "# Nouveau\n\nCorps changé"
    assert current["rev"] != first["rev"]
    _vector_is_current(base_fraiche, "nodes", row["id"])


@pytest.mark.asyncio
async def test_native_page_and_guide_share_content_but_metadata_does_not_rewrite_blocks(base_fraiche):
    from oto_mcp.db.blocks import write_node_blocks
    row = guides.set_guide_db("user", "projection-owner", "shared", "Premier corps")
    public_id = _public_id(base_fraiche, row["id"])
    nodes.update_page(row["id"], body_md="Corps édité depuis page")
    assert guides.get_guide_db("user", "projection-owner", "shared")["body_md"] == "Corps édité depuis page"
    assert "".join(b.get("md", "") for b in (await _read_page(public_id))["body"]) == "Corps édité depuis page"
    # Même une divergence préexistante des blocs ne doit pas être écrasée par une
    # édition de métadonnées. Aucune API ne propose aujourd'hui cette écriture seule.
    write_node_blocks(base_fraiche, row["id"], "Bloc indépendant à préserver")
    before = (await _read_page(public_id))["body"]
    guides.set_guide_db("user", "projection-owner", "shared", "Corps édité depuis page", title="Titre neuf")
    nodes.update_page(row["id"], title="Titre page neuf")
    assert (await _read_page(public_id))["body"] == before
    _vector_is_current(base_fraiche, "nodes", row["id"])


@pytest.mark.asyncio
async def test_seed_only_projects_inserted_nodes_and_never_overwrites_existing(base_fraiche):
    guides.seed_guide_db("user", "projection-owner", "seed", "Corps seed")
    row = guides.get_guide_db("user", "projection-owner", "seed")
    public_id = _public_id(base_fraiche, row["id"])
    assert "".join(b.get("md", "") for b in (await _read_page(public_id))["body"]) == "Corps seed"
    before = (await _read_page(public_id))["body"]
    guides.seed_guide_db("user", "projection-owner", "seed", "Un autre défaut")
    assert (await _read_page(public_id))["body"] == before


def test_brief_procedure_and_native_page_update_their_existing_nonnull_rank(base_fraiche):
    from oto_mcp.db import projects
    from oto_mcp.org_store import instructions
    pid = projects.create_project("user", "projection-owner", "Ancien", "Corps ancien")
    projects.update_project(pid, name="Nouveau titre", brief_md="Arbitrage budgétaire")
    _vector_is_current(base_fraiche, "projects", pid)
    instructions.set_instruction("user", "projection-owner", "procedure", "Premier corps", title="Ancien")
    instructions.set_instruction("user", "projection-owner", "procedure", "Corps révisé")
    instructions.set_instruction_meta("user", "projection-owner", "procedure", title="Procédure actuelle")
    proc = instructions.get_instruction("user", "projection-owner", "procedure")
    _vector_is_current(base_fraiche, "org_instructions", proc["id"])
    page = nodes.create_page(owner_type="user", owner_id="projection-owner", title="Avant", body_md="Ancien")
    nodes.update_page(page["id"], title="Après", body_md="Neuf")
    _vector_is_current(base_fraiche, "nodes", page["id"])


def test_failed_rank_refresh_invalidates_old_cache_and_preserves_write(base_fraiche, monkeypatch, caplog):
    from oto_mcp.db import projects
    pid = projects.create_project("user", "projection-owner", "Ancien", "Ancien corps")
    _vector_is_current(base_fraiche, "projects", pid)
    original = search._vec
    with monkeypatch.context() as patch:
        patch.setattr(search, "_vec", lambda expr: "missing_rank_function()")
        projects.update_project(pid, name="Nouveau", brief_md="budget budget budget")
    row = base_fraiche.execute(
        f"SELECT brief_md, search_vec, {search.rank_expr('projects')} = {original(search.PROJECTS_TEXT)} AS fresh "
        "FROM projects WHERE id = %s", (pid,),
    ).fetchone()
    assert row == {"brief_md": "budget budget budget", "search_vec": None, "fresh": True}
    assert "cache invalidated" in caplog.text
    assert search.rank_pending_counts()["projects"] >= 1
