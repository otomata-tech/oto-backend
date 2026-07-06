"""Capacité REST `me.guides.*` (ADR 0042) : autz par scope (org_admin / self /
platform-refusé) + délégation à guide_store. Seams guide_store/roles monkeypatchés."""
import pytest

from oto_mcp.capabilities import guides as G
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx


@pytest.fixture
def store(monkeypatch):
    calls = {}
    monkeypatch.setattr(G.guide_store, "list_guides_for",
                        lambda sub, org: [{"slug": "x", "scope": "user"}])
    monkeypatch.setattr(G.guide_store, "read_guide_scoped",
                        lambda slug, scope=None, org_id=None, sub=None:
                        {"slug": slug, "scope": scope, "body_md": "B"} if slug == "known" else None)

    def _set(scope, owner, slug, body, title="", desc=""):
        calls["set"] = (scope, owner, slug, body, title, desc)
        return {"slug": slug, "scope": scope, "title": title, "description": desc}
    monkeypatch.setattr(G.guide_store, "set_guide", _set)

    def _del(scope, owner, slug):
        calls["del"] = (scope, owner, slug)
        return True
    monkeypatch.setattr(G.guide_store, "delete_guide", _del)
    import oto_mcp.roles as roles
    monkeypatch.setattr(roles, "is_org_admin", lambda sub, org: sub == "admin")
    return calls


def _ctx(sub="u1", org=None):
    return ResolvedCtx(sub=sub, org_id=org)


def test_list(store):
    out = G._list(_ctx(), G._NoInput())
    assert out["guides"] == [{"slug": "x", "scope": "user"}]


def test_get_found_and_404(store):
    assert G._get(_ctx(), G.GuideRefInput(scope="user", slug="known"))["body_md"] == "B"
    with pytest.raises(AuthzDenied) as e:
        G._get(_ctx(), G.GuideRefInput(scope="user", slug="ghost"))
    assert e.value.status == 404


def test_set_user_is_self(store):
    G._set(_ctx(sub="u1"), G.GuideSetInput(scope="user", slug="s", body_md="b"))
    assert store["set"][:3] == ("user", "u1", "s")           # owner = sub


def test_set_org_requires_admin(store):
    with pytest.raises(AuthzDenied) as e:                     # u1 n'est pas admin
        G._set(_ctx(sub="u1", org=42), G.GuideSetInput(scope="org", slug="s", body_md="b"))
    assert e.value.status == 403
    G._set(_ctx(sub="admin", org=42), G.GuideSetInput(scope="org", slug="s", body_md="b"))
    assert store["set"][:3] == ("org", "42", "s")            # owner = org id (admin)


def test_set_org_without_active_org(store):
    with pytest.raises(AuthzDenied) as e:
        G._set(_ctx(sub="admin", org=None), G.GuideSetInput(scope="org", slug="s", body_md="b"))
    assert e.value.status == 400 and e.value.code == "no_active_org"


def test_set_platform_rejected(store):
    with pytest.raises(AuthzDenied) as e:
        G._set(_ctx(), G.GuideSetInput(scope="platform", slug="s", body_md="b"))
    assert e.value.status == 400 and e.value.code == "bad_scope"


def test_set_invalid_guide_maps_400(store, monkeypatch):
    def boom(*a, **k):
        raise G.guide_store.GuideError("slug invalide")
    monkeypatch.setattr(G.guide_store, "set_guide", boom)
    with pytest.raises(AuthzDenied) as e:
        G._set(_ctx(sub="u1"), G.GuideSetInput(scope="user", slug="Bad", body_md="b"))
    assert e.value.status == 400 and e.value.code == "invalid_guide"


def test_delete(store):
    out = G._delete(_ctx(sub="u1"), G.GuideRefInput(scope="user", slug="s"))
    assert out == {"scope": "user", "slug": "s", "deleted": True}
    assert store["del"] == ("user", "u1", "s")


def test_capabilities_registered():
    from oto_mcp.capabilities.registry import CAPABILITIES
    by_key = {c.key: c for c in CAPABILITIES}
    for k in ("me.guides.list", "me.guides.get", "me.guides.set", "me.guides.delete"):
        assert k in by_key and by_key[k].mcp is None
    rest = by_key["me.guides.set"].rest
    assert rest.verb == "PUT" and rest.path == "/api/me/guides/{scope}/{slug}"
