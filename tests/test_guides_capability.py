"""Capacité REST `me.guides.*` (ADR 0042, tout-DB 2026-07-16) : autz par scope
(platform_admin / org_admin / self) + délégation à guide_store. Seams monkeypatchés."""
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

    def _set(scope, owner, slug, body, title="", desc="", *, expected_rev=None, **_):
        calls["set"] = (scope, owner, slug, body, title, desc)
        return {"slug": slug, "scope": scope, "title": title, "description": desc}
    monkeypatch.setattr(G.guide_store, "set_guide", _set)

    def _del(scope, owner, slug, *, expected_rev=None, **_):
        calls["del"] = (scope, owner, slug)
        return True
    monkeypatch.setattr(G.guide_store, "delete_guide", _del)
    import oto_mcp.roles as roles
    monkeypatch.setattr(roles, "is_org_admin", lambda sub, org: sub == "admin")
    monkeypatch.setattr(roles, "is_platform_admin", lambda sub: sub == "superadmin")
    # Un guide PERSONNEL pose d'abord la maison de son auteur (`ownership.maison_de`).
    monkeypatch.setattr(G.ownership, "maison_de",
                        lambda sub: calls.setdefault("maison", []).append(sub))
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
    assert store["maison"] == ["u1"]                          # sa maison, d'abord


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


def test_set_platform_requires_platform_admin(store):
    with pytest.raises(AuthzDenied) as e:                     # u1 n'est pas platform_admin
        G._set(_ctx(), G.GuideSetInput(scope="platform", slug="s", body_md="b"))
    assert e.value.status == 403
    G._set(_ctx(sub="superadmin"), G.GuideSetInput(scope="platform", slug="s", body_md="b"))
    assert store["set"][:3] == ("platform", "platform", "s")  # owner = 'platform'


def test_set_unknown_scope_rejected(store):
    with pytest.raises(AuthzDenied) as e:
        G._set(_ctx(), G.GuideSetInput(scope="planete", slug="s", body_md="b"))
    assert e.value.status == 400 and e.value.code == "bad_scope"


def test_set_group_scope_requires_team_lead(store, monkeypatch):
    """Le scope `group` est éditable depuis la convergence des surfaces (ADR 0042) —
    l'équipe ACTIVE, gatée sur le chef d'équipe (escalade roles.py)."""
    import oto_mcp.access as access
    import oto_mcp.roles as roles
    monkeypatch.setattr(access, "current_group", lambda sub: 7)
    monkeypatch.setattr(roles, "can_admin_group", lambda sub, gid: sub == "chef")
    with pytest.raises(AuthzDenied) as e:
        G._set(_ctx(sub="u1"), G.GuideSetInput(scope="group", slug="s", body_md="b"))
    assert e.value.status == 403
    G._set(_ctx(sub="chef"), G.GuideSetInput(scope="group", slug="s", body_md="b"))
    assert store["set"][:3] == ("group", "7", "s")            # owner = l'id d'équipe


def test_set_group_scope_without_active_team(store, monkeypatch):
    import oto_mcp.access as access
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    with pytest.raises(AuthzDenied) as e:
        G._set(_ctx(sub="u1"), G.GuideSetInput(scope="group", slug="s", body_md="b"))
    assert e.value.code == "no_active_group"


def test_set_invalid_guide_maps_400(store, monkeypatch):
    def boom(*a, **k):
        raise G.guide_store.GuideError("slug invalide")
    monkeypatch.setattr(G.guide_store, "set_guide", boom)
    with pytest.raises(AuthzDenied) as e:
        G._set(_ctx(sub="u1"), G.GuideSetInput(scope="user", slug="Bad", body_md="b"))
    assert e.value.status == 400 and e.value.code == "invalid_guide"


def test_delete(store):
    out = G._delete(_ctx(sub="u1"), G.GuideDeleteInput(scope="user", slug="s"))
    assert out == {"scope": "user", "slug": "s", "deleted": True}
    assert store["del"] == ("user", "u1", "s")


def test_capabilities_registered():
    from oto_mcp.capabilities.registry import CAPABILITIES
    by_key = {c.key: c for c in CAPABILITIES}
    for k in ("me.guides.list", "me.guides.get", "me.guides.set", "me.guides.delete"):
        assert k in by_key and by_key[k].mcp is None
    first = by_key["me.guides.set"].rest_bindings()[0]
    assert first.verb == "PUT" and first.path == "/api/me/guides/{scope}/{slug}"


# ── delivery='init' : le readme d'un scope EST un guide (ADR 0042 §Surfaces) ──

@pytest.fixture
def init_store(monkeypatch):
    """Seams des readmes init (table `guides` delivery='init')."""
    rows, calls = {}, {}

    def _get_init(scope, ident=None):
        return {"body_md": rows.get((scope, str(ident)), ""), "updated_at": None}

    def _set_init(scope, ident, body_md, *, expected_rev=None, **_):
        calls["set_init"] = (scope, str(ident), body_md)
        rows[(scope, str(ident))] = body_md
        return {"body_md": body_md, "updated_at": "2026-07-28 10:00:00"}

    monkeypatch.setattr(G.guide_store, "get_init_guide", _get_init)
    monkeypatch.setattr(G.guide_store, "set_init_guide", _set_init)
    import oto_mcp.roles as roles
    monkeypatch.setattr(roles, "is_org_admin", lambda sub, org: sub == "admin")
    monkeypatch.setattr(roles, "is_platform_admin", lambda sub: sub == "superadmin")
    calls["rows"] = rows
    return calls


def test_init_read_write_user_readme(init_store):
    """La note perso : slug canonique (pas à connaître), owner = le sub."""
    out = G._set(_ctx(sub="u1"), G.GuideSetInput(scope="user", delivery="init", body_md="ma note"))
    assert init_store["set_init"] == ("user", "u1", "ma note")
    assert out["delivery"] == "init" and out["slug"] == G.guide_store.INIT_SLUG
    assert G._get(_ctx(sub="u1"), G.GuideRefInput(scope="user", delivery="init"))["body_md"] == "ma note"


def test_init_empty_body_clears_the_layer(init_store):
    """Un corps vide EFFACE une couche injectée (là où un on-demand vide est refusé)."""
    G._set(_ctx(sub="u1"), G.GuideSetInput(scope="user", delivery="init", body_md=""))
    assert init_store["set_init"] == ("user", "u1", "")
    with pytest.raises(AuthzDenied) as e:                      # on-demand : toujours refusé
        G._set(_ctx(sub="u1"), G.GuideSetInput(scope="user", slug="s", body_md="  "))
    assert e.value.code == "missing_body"


def test_init_org_readme_requires_admin(init_store):
    with pytest.raises(AuthzDenied) as e:
        G._set(_ctx(sub="u1", org=42), G.GuideSetInput(scope="org", delivery="init", body_md="x"))
    assert e.value.status == 403
    G._set(_ctx(sub="admin", org=42), G.GuideSetInput(scope="org", delivery="init", body_md="x"))
    assert init_store["set_init"] == ("org", "42", "x")


def test_init_platform_ident_is_the_block_key(init_store):
    """Scope plateforme : plusieurs blocs init → l'ident passé au store EST le slug."""
    G._set(_ctx(sub="superadmin"), G.GuideSetInput(scope="platform", delivery="init",
                                                   slug="secret_sauce", body_md="posture"))
    assert init_store["set_init"] == ("platform", "secret_sauce", "posture")


def test_mcp_face_reaches_init_without_a_slug(init_store):
    out = G._guide_op(_ctx(sub="u1"), G.GuideOpInput(op="write", delivery="init", body_md="hop"))
    assert out["scope"] == "user" and out["delivery"] == "init"
    assert G._guide_op(_ctx(sub="u1"), G.GuideOpInput(op="read", delivery="init"))["body_md"] == "hop"


def test_init_targets_an_explicit_org_not_the_active_one(init_store, monkeypatch):
    """Une vue qui gère UNE org/équipe passe son id — sinon on éditerait le readme de
    l'org « active » de la session, qui n'est pas ce que l'écran montre."""
    G._set(_ctx(sub="admin", org=1), G.GuideSetInput(scope="org", delivery="init",
                                                     owner_id="99", body_md="x"))
    assert init_store["set_init"] == ("org", "99", "x")     # 99, pas l'org active 1


def test_init_read_of_another_org_requires_membership(init_store, monkeypatch):
    import oto_mcp.roles as roles
    monkeypatch.setattr(roles, "is_org_member", lambda sub, oid: sub == "membre")
    with pytest.raises(AuthzDenied) as e:
        G._get(_ctx(sub="etranger", org=1), G.GuideRefInput(scope="org", delivery="init",
                                                            owner_id="99"))
    assert e.value.status == 403
    assert G._get(_ctx(sub="membre", org=1),
                  G.GuideRefInput(scope="org", delivery="init", owner_id="99"))["scope"] == "org"


def test_init_group_targets_explicit_team(init_store, monkeypatch):
    import oto_mcp.roles as roles
    monkeypatch.setattr(roles, "can_admin_group", lambda sub, gid: sub == "chef")
    G._set(_ctx(sub="chef"), G.GuideSetInput(scope="group", delivery="init",
                                             owner_id="7", body_md="équipe"))
    assert init_store["set_init"] == ("group", "7", "équipe")


def test_rest_bindings_cover_me_and_by_id():
    from oto_mcp.capabilities.registry import CAPABILITIES
    by_key = {c.key: c for c in CAPABILITIES}
    paths = {b.path for b in by_key["me.guides.set"].rest_bindings()}
    assert paths == {"/api/me/guides/{scope}/{slug}",
                     "/api/orgs/{id}/guides/{scope}/{slug}",
                     "/api/groups/{id}/guides/{scope}/{slug}"}
