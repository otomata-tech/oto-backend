"""compose_session : cumul des readmes « init » par scope (ADR 0042 B2). Renderer
unifié `_render_init_readme` — mêmes octets qu'avant (org/group/user cumulés, vars
substituées, scope vide omis). Mocks des sources, pas de DB."""
import oto_mcp.instructions as I


def _wire(monkeypatch, bodies, ctx):
    from oto_mcp import guide_store, providers
    monkeypatch.setattr(guide_store, "init_guide_body",
                        lambda scope, owner_id=None: bodies.get(scope))
    monkeypatch.setattr(providers, "render_namespace_catalog", lambda: "ns_a\nns_b")
    monkeypatch.setattr(I, "_resolve_context", lambda sub, org_id: dict(ctx))


_CTX = {"org_name": "Acme", "user_name": "Bob", "role": "member",
        "group_name": "Sales", "group_id": 5, "connectors": ["serper"],
        "projects": [], "runs": [], "profile": {}}


def test_full_cumulates_scopes_in_order(monkeypatch):
    _wire(monkeypatch, {"platform": "SAUCE", "org": "ORG {{org}}",
                        "group": "GRP", "user": "USR"}, _CTX)
    out = I.compose_session("u1", 42)
    # Ordre : plateforme → catalogue → contexte → org → équipe → user.
    i_plat = out.index("SAUCE")
    i_cat = out.index("ns_a")
    i_ctx = out.index("## Ton contexte oto")
    i_org = out.index("## README de ton organisation (Acme)")
    i_grp = out.index("## README de ton équipe (Sales)")
    i_usr = out.index("## README de ton utilisateur")
    assert i_plat < i_cat < i_ctx < i_org < i_grp < i_usr
    assert "ORG Acme" in out            # {{org}} substitué dans le corps org


def test_empty_scope_is_omitted(monkeypatch):
    # group/user sans corps → sections omises ; org présent.
    _wire(monkeypatch, {"platform": "SAUCE", "org": "ORG", "group": None, "user": None}, _CTX)
    out = I.compose_session("u1", 42)
    assert "## README de ton organisation" in out
    assert "## README de ton équipe" not in out
    assert "## README de ton utilisateur" not in out


def test_no_org_is_block_a_only(monkeypatch):
    _wire(monkeypatch, {"platform": "SAUCE"}, _CTX)
    out = I.compose_session("u1", None)
    assert "SAUCE" in out and "ns_a" in out
    assert "## Ton contexte oto" not in out       # bloc C omis sans org


def test_group_header_without_name(monkeypatch):
    ctx = dict(_CTX, group_name="")               # équipe sans nom → en-tête nu
    _wire(monkeypatch, {"platform": "SAUCE", "org": "ORG", "group": "GRP", "user": None}, ctx)
    out = I.compose_session("u1", 42)
    assert "## README de ton équipe\n\nGRP" in out  # pas de suffixe " (…)"
