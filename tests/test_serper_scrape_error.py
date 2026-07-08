"""serper_scrape : un échec de scrape (5xx « Scraping failed » côté Serper) est un
échec par-URL attendu, pas un bug backend → converti en McpError d'entrée (message
actionnable pour l'agent + droppé par la taxonomie Sentry). Les 4xx (crédits/clé)
restent propagés tels quels ; les autres endpoints ne sont pas touchés."""
from __future__ import annotations

import pytest
from mcp.shared.exceptions import McpError

from oto_mcp.error_taxonomy import _is_expected_error


class _Reg:
    def __init__(self):
        self.tools = {}

    def tool(self, *a, **k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        if a and callable(a[0]):
            return deco(a[0])
        return deco


@pytest.fixture()
def scrape(monkeypatch):
    """Enregistre le connecteur serper avec un client stubé (raise piloté)."""
    calls = {}

    class _Client:
        def __init__(self, *a, **k):
            ...

        def scrape_page(self, url, include_markdown=True):
            calls["url"] = url
            raise calls["exc"]

    monkeypatch.setattr("oto.tools.serper.SerperClient", _Client)
    monkeypatch.setattr("oto_mcp.access.resolve_api_key", lambda p: ("k", False))
    from oto_mcp.tools import serper
    reg = _Reg()
    serper.register(reg)
    return reg.tools["serper_scrape"], calls


def test_scrape_5xx_becomes_managed_mcp_error(scrape):
    fn, calls = scrape
    calls["exc"] = RuntimeError("Serper scrape 500: Scraping failed.")
    with pytest.raises(McpError) as ei:
        fn("https://example.com/page")
    # message actionnable, pas un stacktrace ; et droppé par la taxonomie Sentry
    assert "Scrape impossible" in ei.value.error.message
    assert "example.com/page" in ei.value.error.message
    assert _is_expected_error(ei.value) is True


def test_scrape_4xx_propagates_unchanged(scrape):
    # 402/403 (crédits épuisés, clé invalide) = vrai problème de config, pas un
    # échec d'URL → on ne le masque PAS derrière « URL non scrapable ».
    fn, calls = scrape
    calls["exc"] = RuntimeError("Serper scrape 402: Not enough credits")
    with pytest.raises(RuntimeError) as ei:
        fn("https://example.com/page")
    assert not isinstance(ei.value, McpError)
    assert "Not enough credits" in str(ei.value)
