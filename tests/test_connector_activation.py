"""Cran d'activation des connecteurs (ADR 0010, B1).

Teste la résolution pure `override d'org > master global > OFF` — le cœur de
gouvernance. Les helpers DB (`is_exposed`/`exposed_connectors`) ne font
qu'alimenter cette résolution depuis les rows ; leur chemin SQL est vérifié au
déploiement (table + seed au boot).
"""
from oto_mcp.connector_activation import _resolve


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
