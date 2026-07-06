"""ADR 0042 : `guide_store.init_guide_body` = lecture unique de la prose `init`.
platform + user lisent `guides` (delivery='init', barreau 1) ; org/group lisent encore
`*_instructions[claude_md]` (barreau 2 à venir). Fail-open identique."""
from oto_mcp import guide_store as G
from oto_mcp import instructions as I


# ── platform : guides delivery='init', owner='platform', slug=la clé ──

def test_platform_reads_init_guide(monkeypatch):
    seen = {}
    import oto_mcp.db as db
    def _get(scope, owner, slug):
        seen.update(scope=scope, owner=owner, slug=slug)
        return {"body_md": "  A  "}
    monkeypatch.setattr(db, "get_init_guide_db", _get)
    assert G.init_guide_body("platform", "secret_sauce") == "A"   # strippé
    assert seen == {"scope": "platform", "owner": "platform", "slug": "secret_sauce"}


def test_platform_defaults_key(monkeypatch):
    import oto_mcp.db as db
    monkeypatch.setattr(db, "get_init_guide_db",
                        lambda scope, owner, slug: {"body_md": slug})
    assert G.init_guide_body("platform") == "secret_sauce"        # slug par défaut


# ── org / group : guides delivery='init', owner=id::text, slug='readme' (barreau 2) ──

def test_org_reads_init_guide(monkeypatch):
    import oto_mcp.db as db
    seen = {}
    def _get(scope, owner, slug):
        seen.update(scope=scope, owner=owner, slug=slug)
        return {"body_md": "ORG"}
    monkeypatch.setattr(db, "get_init_guide_db", _get)
    assert G.init_guide_body("org", 42) == "ORG"
    assert seen == {"scope": "org", "owner": "42", "slug": "readme"}


def test_group_reads_init_guide(monkeypatch):
    import oto_mcp.db as db
    seen = {}
    def _get(scope, owner, slug):
        seen.update(scope=scope, owner=owner, slug=slug)
        return {"body_md": "GRP"}
    monkeypatch.setattr(db, "get_init_guide_db", _get)
    assert G.init_guide_body("group", 7) == "GRP"
    assert seen == {"scope": "group", "owner": "7", "slug": "readme"}


# ── user : guides delivery='init', owner=sub, slug='readme' ──

def test_user_reads_readme(monkeypatch):
    import oto_mcp.db as db
    seen = {}
    def _get(scope, owner, slug):
        seen.update(scope=scope, owner=owner, slug=slug)
        return {"body_md": "USER"}
    monkeypatch.setattr(db, "get_init_guide_db", _get)
    assert G.init_guide_body("user", "u1") == "USER"
    assert seen == {"scope": "user", "owner": "u1", "slug": "readme"}


# ── fail-open : absent / vide / erreur → None ──

def test_empty_body_is_none(monkeypatch):
    import oto_mcp.db as db
    monkeypatch.setattr(db, "get_init_guide_db", lambda scope, owner, slug: {"body_md": "   "})
    assert G.init_guide_body("platform") is None


def test_error_is_none(monkeypatch):
    import oto_mcp.db as db
    def boom(scope, owner, slug):
        raise RuntimeError("DB down")
    monkeypatch.setattr(db, "get_init_guide_db", boom)
    assert G.init_guide_body("org", 1) is None                   # fail-open


def test_unknown_scope_is_none():
    assert G.init_guide_body("nope", 1) is None


# ── _platform_block : None → seed (comportement conservé) ──

def test_platform_block_falls_back_to_seed(monkeypatch):
    monkeypatch.setattr(G, "init_guide_body", lambda scope, owner=None: None)
    assert I._platform_block(I.KEY_SECRET_SAUCE, I._SECRET_SAUCE) == I._SECRET_SAUCE.strip()


def test_platform_block_uses_db_override(monkeypatch):
    monkeypatch.setattr(G, "init_guide_body", lambda scope, owner=None: "OVERRIDE")
    assert I._platform_block(I.KEY_SECRET_SAUCE, I._SECRET_SAUCE) == "OVERRIDE"
