"""Guides on-demand scopés (ADR 0042 B5) : platform (fichiers) ∪ org ∪ user (DB),
+ write/delete avec autz. Seams DB/auth monkeypatchés."""
import pytest

from oto_mcp import guide_store as G


# ── guide_store : dispatch scope (DB mockée) ──

class _FakeGuidesDB:
    def __init__(self):
        self.rows = {}   # (scope, owner, slug) -> dict

    def list_guides_db(self, scope, owner_id):
        return [v for (s, o, _), v in sorted(self.rows.items())
                if s == scope and o == str(owner_id)]

    def get_guide_db(self, scope, owner_id, slug):
        return self.rows.get((scope, str(owner_id), slug))

    def set_guide_db(self, scope, owner_id, slug, body_md, title, description):
        row = {"slug": slug, "title": title, "description": description, "body_md": body_md}
        self.rows[(scope, str(owner_id), slug)] = row
        return row

    def delete_guide_db(self, scope, owner_id, slug):
        return self.rows.pop((scope, str(owner_id), slug), None) is not None


@pytest.fixture
def db(monkeypatch):
    fake = _FakeGuidesDB()
    import oto_mcp.db as real
    for n in ("list_guides_db", "get_guide_db", "set_guide_db", "delete_guide_db"):
        monkeypatch.setattr(real, n, getattr(fake, n))
    return fake


def test_list_merges_platform_org_user(db):
    db.set_guide_db("org", "42", "process-x", "corps", "Process X", "d")
    db.set_guide_db("user", "u1", "mon-truc", "corps", "Mon truc", "d")
    out = G.list_guides_for(sub="u1", org_id=42)
    by = {(g["scope"], g["slug"]) for g in out}
    assert ("platform", "bulk-load") in by      # fichier livré
    assert ("org", "process-x") in by
    assert ("user", "mon-truc") in by


def test_read_scoped_search_order(db):
    db.set_guide_db("org", "42", "only-org", "CORPS ORG", "", "")
    g = G.read_guide_scoped("only-org", org_id=42, sub="u1")
    assert g["scope"] == "org" and g["body_md"] == "CORPS ORG"
    # platform gagne l'ordre de recherche
    assert G.read_guide_scoped("bulk-load", org_id=42, sub="u1")["scope"] == "platform"
    assert G.read_guide_scoped("inexistant", org_id=42, sub="u1") is None


def test_read_scoped_explicit_scope(db):
    db.set_guide_db("user", "u1", "x", "USR", "", "")
    assert G.read_guide_scoped("x", scope="user", sub="u1")["body_md"] == "USR"
    assert G.read_guide_scoped("x", scope="org", org_id=42) is None   # pas ce scope


def test_set_guide_validates(db):
    with pytest.raises(G.GuideError):
        G.set_guide("platform", "x", "s", "b")          # platform non éditable
    with pytest.raises(G.GuideError):
        G.set_guide("org", "42", "Bad Slug", "b")       # slug invalide
    with pytest.raises(G.GuideError):
        G.set_guide("user", "u1", "ok", "   ")          # corps vide
    out = G.set_guide("org", "42", "ok-slug", "corps", "T", "D")
    assert out == {"slug": "ok-slug", "scope": "org", "title": "T", "description": "D"}


# ── tool oto_guide : autz inline ──

class _FakeMCP:
    def __init__(self): self.fn = None
    def tool(self, **kw):
        def deco(f): self.fn = f; return f
        return deco


@pytest.fixture
def tool(monkeypatch):
    from oto_mcp.tools import guide
    monkeypatch.setattr(guide, "current_user_sub_from_token", lambda: "u1")
    import oto_mcp.access as access
    import oto_mcp.roles as roles
    monkeypatch.setattr(access, "current_org", lambda sub: 42)
    monkeypatch.setattr(roles, "is_org_admin", lambda sub, org: sub == "admin")
    calls = {}
    monkeypatch.setattr(G, "list_guides_for", lambda sub, org: [{"slug": "z", "scope": "user"}])

    def _set(scope, owner_id, slug, body_md, title="", description=""):
        calls["set"] = ((scope, owner_id, slug, body_md, title, description), {})
        return {"slug": slug, "scope": scope}

    def _del(scope, owner_id, slug):
        calls["del"] = (scope, owner_id, slug)
        return True

    monkeypatch.setattr(G, "set_guide", _set)
    monkeypatch.setattr(G, "delete_guide", _del)
    m = _FakeMCP()
    guide.register(m)
    m._calls = calls
    return m


def test_tool_write_user_scope_is_self(tool):
    out = tool.fn(op="write", slug="mine", body_md="x", scope="user")
    assert out["scope"] == "user"
    assert tool._calls["set"][0] == ("user", "u1", "mine", "x", "", "")   # owner = sub


def test_tool_write_org_requires_admin(tool, monkeypatch):
    from mcp.shared.exceptions import McpError
    with pytest.raises(McpError):                       # u1 n'est pas admin
        tool.fn(op="write", slug="proc", body_md="x", scope="org")
    monkeypatch.setattr(__import__("oto_mcp.roles", fromlist=["x"]),
                        "is_org_admin", lambda sub, org: True)
    out = tool.fn(op="write", slug="proc", body_md="x", scope="org")
    assert out["scope"] == "org" and tool._calls["set"][0][1] == "42"   # owner = org id


def test_tool_write_platform_rejected(tool):
    from mcp.shared.exceptions import McpError
    with pytest.raises(McpError):
        tool.fn(op="write", slug="x", body_md="y", scope="platform")


def test_tool_delete(tool):
    out = tool.fn(op="delete", slug="mine", scope="user")
    assert out == {"slug": "mine", "scope": "user", "deleted": True}
