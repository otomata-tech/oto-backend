"""`connector_verify.run` — helper partagé du verify-avant-persist (#106).

Exécute la sonde d'un connecteur (sync OU async), lève sur échec d'auth, no-op si
aucune sonde n'est enregistrée. C'est ce que `api_key_save` appelle pour refuser une
clé cassée avant de l'écrire.
"""
from __future__ import annotations

import pytest

from oto_mcp import connector_verify


@pytest.fixture(autouse=True)
def _clean_registry():
    saved = dict(connector_verify._REGISTRY)
    try:
        yield
    finally:
        connector_verify._REGISTRY.clear()
        connector_verify._REGISTRY.update(saved)


async def _run(connector, fields, config=None):
    return await connector_verify.run(connector, fields, config)


def test_no_probe_is_noop():
    # connecteur sans sonde → aucune exception, aucune vérif
    import asyncio
    asyncio.run(_run("connector-sans-sonde", {"key": "x"}))


def test_sync_probe_success_and_receives_fields_config():
    seen = {}

    def probe(fields, config):
        seen["fields"] = fields
        seen["config"] = config

    connector_verify.register("acme", probe)
    import asyncio
    asyncio.run(_run("acme", {"key": "good"}, {"dsn": "api.example.com"}))
    assert seen == {"fields": {"key": "good"}, "config": {"dsn": "api.example.com"}}


def test_sync_probe_failure_raises():
    def probe(fields, config):
        raise ValueError("bad data center")

    connector_verify.register("acme", probe)
    import asyncio
    with pytest.raises(ValueError, match="bad data center"):
        asyncio.run(_run("acme", {"key": "bad"}))


def test_async_probe_is_awaited():
    calls = []

    async def probe(fields, config):
        calls.append(fields)

    connector_verify.register("acme", probe)
    import asyncio
    asyncio.run(_run("acme", {"key": "z"}))
    assert calls == [{"key": "z"}]


def test_async_probe_failure_raises():
    async def probe(fields, config):
        raise RuntimeError("expired token")

    connector_verify.register("acme", probe)
    import asyncio
    with pytest.raises(RuntimeError, match="expired token"):
        asyncio.run(_run("acme", {"key": "bad"}))
