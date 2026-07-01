"""Mount fédéré SANS auth (justicelibre) — verrouille le chemin no-auth.

Un mount `kind="mount"` avec `auth_modes` VIDE (endpoint hébergé public, ex.
justicelibre.org/mcp) doit forwarder SANS token per-user : ni `resolve_mount_token`,
ni header `Authorization`, ni exigence d'un sub courant. Contraste avec un mount
byo_user (memento) dont la factory lève hors requête. On exerce le vrai chemin
(pas de réseau : la Client n'ouvre la connexion qu'à l'entrée du context manager).
"""
import asyncio

import pytest

from oto_mcp import providers
from oto_mcp.tools import mount


def _connector(name):
    c = providers.REGISTRY.get(name)
    assert c is not None and c.kind == "mount", f"{name} doit être un mount déclaré"
    return c


def test_justicelibre_is_declared_noauth_mount():
    c = _connector("justicelibre")
    assert not c.auth_modes, "justicelibre doit être no-auth (auth_modes vide)"
    assert c.mount_url == "https://justicelibre.org/mcp"
    assert c.namespaces == ("justicelibre",)


def test_noauth_factory_needs_no_token_no_sub():
    """La factory no-auth construit un Client SANS résoudre de token ni exiger un
    sub — même hors contexte de requête (là où la factory memento lèverait)."""
    called = {"resolve": False}

    def _boom(_name):  # ne doit JAMAIS être appelé pour un mount no-auth
        called["resolve"] = True
        raise AssertionError("resolve_mount_token appelé pour un mount no-auth")

    orig = mount.access.resolve_mount_token
    mount.access.resolve_mount_token = _boom
    try:
        factory = mount._make_factory(_connector("justicelibre"))
        client = asyncio.run(factory())
    finally:
        mount.access.resolve_mount_token = orig

    assert client is not None
    assert called["resolve"] is False


def test_byo_mount_factory_still_gates():
    """Contraste : un mount byo_user (memento) lève bien hors requête (aucun sub)."""
    from mcp.shared.exceptions import McpError

    factory = mount._make_factory(_connector("memento"))
    with pytest.raises(McpError):
        asyncio.run(factory())
