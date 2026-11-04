"""Guides on-demand scopés (ADR 0042 B5, tout-DB 2026-07-16) : platform ∪ org ∪ user,
tous en DB, + write/delete avec autz par scope (platform_admin / org_admin / self).
Seams DB/auth monkeypatchés."""
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

    def set_guide_db(self, scope, owner_id, slug, body_md, title, description, *,
                     expected_rev=None, **_):
        row = {"slug": slug, "title": title, "description": description, "body_md": body_md}
        self.rows[(scope, str(owner_id), slug)] = row
        return row

    def delete_guide_db(self, scope, owner_id, slug, *, expected_rev=None, **_):
        return self.rows.pop((scope, str(owner_id), slug), None) is not None


@pytest.fixture
def db(monkeypatch):
    fake = _FakeGuidesDB()
    import oto_mcp.db as real
    for n in ("list_guides_db", "get_guide_db", "set_guide_db", "delete_guide_db"):
        monkeypatch.setattr(real, n, getattr(fake, n))
    return fake


def test_list_merges_platform_org_user(db):
    db.set_guide_db("platform", "platform", "bulk-load", "corps", "Bulk", "d")
    db.set_guide_db("org", "42", "process-x", "corps", "Process X", "d")
    db.set_guide_db("user", "u1", "mon-truc", "corps", "Mon truc", "d")
    out = G.list_guides_for(sub="u1", org_id=42)
    by = {(g["scope"], g["slug"]) for g in out}
    assert ("platform", "bulk-load") in by      # DB, plus un fichier
    assert ("org", "process-x") in by
    assert ("user", "mon-truc") in by


def test_read_scoped_search_order(db):
    db.set_guide_db("platform", "platform", "bulk-load", "CORPS PF", "", "")
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
        G.set_guide("group", "7", "s", "b", edited_by=None)             # scope inconnu du on-demand
    with pytest.raises(G.GuideError):
        G.set_guide("org", "42", "Bad Slug", "b", edited_by=None)       # slug invalide
    with pytest.raises(G.GuideError):
        G.set_guide("user", "u1", "ok", "   ", edited_by=None)          # corps vide
    from oto_mcp.db.guides import guide_rev
    out = G.set_guide("org", "42", "ok-slug", "corps", "T", "D", edited_by=None)
    # `rev` / `previous_rev` (signal #1203) : ce que l'écriture a posé, et remplacé.
    assert out == {"slug": "ok-slug", "scope": "org", "title": "T", "description": "D",
                   "rev": guide_rev("T", "D", "corps"), "previous_rev": None}
    # platform est désormais éditable au niveau STORE (l'autz vit dans les surfaces)
    out = G.set_guide("platform", G.PLATFORM_OWNER, "pf-slug", "corps", "T", "D", edited_by=None)
    assert out["scope"] == "platform"


# ── face MCP `oto_guide` : capacité op-aware, autz par scope ──
# Depuis le 2026-07-28 ce n'est plus un tool écrit à la main mais la capacité `me.guide`
# (ADR 0042 §Convergence des surfaces) : mêmes handlers que les routes REST.

@pytest.fixture
def tool(monkeypatch):
    import oto_mcp.capabilities.guides as C
    import oto_mcp.roles as roles
    monkeypatch.setattr(roles, "is_org_admin", lambda sub, org: sub == "admin")
    monkeypatch.setattr(roles, "is_platform_admin", lambda sub: False)
    # Un guide PERSONNEL pose d'abord la maison de son auteur (`ownership.maison_de`).
    monkeypatch.setattr(C.ownership, "maison_de", lambda sub: 1)
    calls = {}
    monkeypatch.setattr(G, "list_guides_for", lambda sub, org: [{"slug": "z", "scope": "user"}])

    def _set(scope, owner_id, slug, body_md, title="", description="", *,
             expected_rev=None, **_):
        calls["set"] = ((scope, owner_id, slug, body_md, title, description), {})
        return {"slug": slug, "scope": scope}

    def _del(scope, owner_id, slug, *, expected_rev=None, **_):
        calls["del"] = (scope, owner_id, slug)
        return True

    monkeypatch.setattr(G, "set_guide", _set)
    monkeypatch.setattr(G, "delete_guide", _del)

    class _Runner:
        """Exerce la capacité comme le ferait l'adaptateur MCP (ctx déjà résolu)."""
        _calls = calls

        def fn(self, **kw):
            ctx = C.ResolvedCtx(sub="u1", org_id=42)
            return C._guide_op(ctx, C.GuideOpInput(**kw))

    return _Runner()


def test_tool_write_user_scope_is_self(tool):
    out = tool.fn(op="write", slug="mine", body_md="x", scope="user")
    assert out["scope"] == "user"
    assert tool._calls["set"][0] == ("user", "u1", "mine", "x", "", "")   # owner = sub


def test_tool_write_defaults_to_user_scope(tool):
    """Scope omis à l'écriture = l'utilisateur — un agent n'écrit jamais pour l'org
    par défaut (elle exigerait org_admin de toute façon)."""
    out = tool.fn(op="write", slug="mine", body_md="x")
    assert out["scope"] == "user" and tool._calls["set"][0][1] == "u1"


def test_tool_write_org_requires_admin(tool, monkeypatch):
    from oto_mcp.capabilities._types import AuthzDenied
    with pytest.raises(AuthzDenied):                    # u1 n'est pas admin
        tool.fn(op="write", slug="proc", body_md="x", scope="org")
    monkeypatch.setattr(__import__("oto_mcp.roles", fromlist=["x"]),
                        "is_org_admin", lambda sub, org: True)
    out = tool.fn(op="write", slug="proc", body_md="x", scope="org")
    assert out["scope"] == "org" and tool._calls["set"][0][1] == "42"   # owner = org id


def test_tool_write_platform_requires_platform_admin(tool, monkeypatch):
    from oto_mcp.capabilities._types import AuthzDenied
    with pytest.raises(AuthzDenied):                    # u1 n'est pas platform_admin
        tool.fn(op="write", slug="x", body_md="y", scope="platform")
    monkeypatch.setattr(__import__("oto_mcp.roles", fromlist=["x"]),
                        "is_platform_admin", lambda sub: True)
    out = tool.fn(op="write", slug="x", body_md="y", scope="platform")
    assert out["scope"] == "platform"
    assert tool._calls["set"][0][1] == G.PLATFORM_OWNER   # owner = 'platform'


def test_tool_write_rejects_empty_and_oversized_body(tool):
    from oto_mcp.capabilities._types import AuthzDenied
    with pytest.raises(AuthzDenied):
        tool.fn(op="write", slug="mine", body_md="   ")
    with pytest.raises(AuthzDenied):                    # cap 64 KB, désormais des DEUX faces
        tool.fn(op="write", slug="mine", body_md="x" * (64 * 1024 + 1))


def test_tool_delete(tool):
    out = tool.fn(op="delete", slug="mine", scope="user")
    assert out == {"scope": "user", "slug": "mine", "deleted": True}


def test_tool_requires_slug(tool):
    from oto_mcp.capabilities._types import AuthzDenied
    with pytest.raises(AuthzDenied):
        tool.fn(op="read")
