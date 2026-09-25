"""Cran d'activation des connecteurs (ADR 0010, B1).

Teste la résolution pure `override d'org > master global > OFF` — le cœur de
gouvernance. Les helpers DB (`is_exposed`/`exposed_connectors`) ne font
qu'alimenter cette résolution depuis les rows ; leur chemin SQL est vérifié au
déploiement (table + seed au boot).
"""
from oto_mcp.connectors.activation import _resolve


def test_master_global():
    assert _resolve({"a": True}, {}) == {"a"}
    assert _resolve({"a": False}, {}) == set()


def test_org_override_beats_global():
    # override d'org OFF masque un master global ON…
    assert _resolve({"a": True}, {"a": False}) == set()
    # …et override ON expose malgré un global OFF.
    assert _resolve({"a": False}, {"a": True}) == {"a"}


def test_org_only_override():
    # pas de master global, override d'org seul.
    assert _resolve({}, {"a": True}) == {"a"}
    assert _resolve({}, {"a": False}) == set()


def test_deny_by_default():
    # un connecteur sans aucune ligne n'est jamais exposé.
    assert _resolve({}, {}) == set()
    assert _resolve({"a": True, "b": True}, {"b": False}) == {"a"}


def test_registry_importable_for_seed():
    # le seed dérive du registre : il doit s'importer (pas de circular) et être
    # non vide, sinon B1 ne pourrait rien activer.
    from oto_mcp import providers

    assert len(providers.REGISTRY) >= 1
    assert "serper" in providers.REGISTRY


# --- le cran TENANT : un plafond (2026-09-26) ------------------------------------

def test_le_tenant_coupe_sous_le_master_et_au_dessus_de_lorg():
    # Coupé par le tenant : ni le master ON ni un override d'org ON ne rouvrent.
    assert _resolve({"a": True}, {}, {"a": False}) == set()
    assert _resolve({"a": True}, {"a": True}, {"a": False}) == set()
    # Une ligne tenant à True n'expose rien que la plateforme ne donne pas.
    assert _resolve({"a": False}, {}, {"a": True}) == set()
    assert _resolve({}, {}, {"a": True}) == set()
    # Sans ligne tenant sur `a`, rien ne change pour lui.
    assert _resolve({"a": True, "b": True}, {"b": False}, {"c": False}) == {"a"}
    assert _resolve({"a": True}, {}, None) == {"a"}
