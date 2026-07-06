"""ADR 0042 barreau 2 : le readme (slug `claude_md`) est routé vers `guides` DANS
`org_store`/`group_store` — les procédures nommées gardent leur table + versioning.
On mocke `guide_store.{get,set}_init_guide` : le readme n'a plus version/slots/historique,
mais garde la FORME d'une instruction (compat vues doctrine)."""
import oto_mcp.group_store as group_store
import oto_mcp.org_store as org_store


# ── org ──

def test_org_get_base_reads_guides(monkeypatch):
    import oto_mcp.guide_store as G
    monkeypatch.setattr(G, "get_init_guide",
                        lambda scope, oid: {"body_md": "README ORG", "updated_at": "T"})
    out = org_store.get_instruction(42, "claude_md")
    assert out["body_md"] == "README ORG" and out["slug"] == "claude_md"
    assert out["version"] == 1 and out["slots"] == [] and out["id"] is None


def test_org_get_base_absent_is_none(monkeypatch):
    import oto_mcp.guide_store as G
    monkeypatch.setattr(G, "get_init_guide", lambda scope, oid: {"body_md": "", "updated_at": None})
    assert org_store.get_instruction(42, "claude_md") is None


def test_org_get_base_version_is_none(monkeypatch):
    # le readme n'a pas d'historique → une version précise = None (404 côté capacité).
    import oto_mcp.guide_store as G
    monkeypatch.setattr(G, "get_init_guide", lambda scope, oid: {"body_md": "X", "updated_at": "T"})
    assert org_store.get_instruction(42, "claude_md", version=2) is None


def test_org_set_base_writes_guides(monkeypatch):
    seen = {}
    import oto_mcp.guide_store as G
    monkeypatch.setattr(G, "set_init_guide",
                        lambda scope, oid, body: seen.update(scope=scope, oid=oid, body=body))
    v = org_store.set_instruction(42, "claude_md", "NOUVEAU README")
    assert v == 1                                            # pas de versioning
    assert seen == {"scope": "org", "oid": 42, "body": "NOUVEAU README"}


def test_org_base_no_versions():
    assert org_store.list_instruction_versions(42, "claude_md") == []


# ── group (miroir) ──

def test_group_set_base_writes_guides(monkeypatch):
    seen = {}
    import oto_mcp.guide_store as G
    monkeypatch.setattr(G, "set_init_guide",
                        lambda scope, gid, body: seen.update(scope=scope, gid=gid, body=body))
    v = group_store.set_group_instruction(7, "claude_md", "README ÉQUIPE")
    assert v == 1
    assert seen == {"scope": "group", "gid": 7, "body": "README ÉQUIPE"}


def test_group_get_base_reads_guides(monkeypatch):
    import oto_mcp.guide_store as G
    monkeypatch.setattr(G, "get_init_guide", lambda scope, gid: {"body_md": "GRP", "updated_at": "T"})
    out = group_store.get_group_instruction(7, "claude_md")
    assert out["body_md"] == "GRP" and out["group_id"] == 7 and out["version"] == 1


def test_group_base_no_versions():
    assert group_store.list_group_instruction_versions(7, "claude_md") == []
