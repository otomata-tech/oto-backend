"""serper : les échecs d'ENTRÉE Serper sont convertis en McpError d'entrée (message
actionnable pour l'agent + droppé par la taxonomie Sentry), pas remontés en 500 :
 - **5xx** du scrape (« Scraping failed ») → message « URL non scrapable » ;
 - **400** générique (dans `_run`, tout tool serper) : URL non scrapable
   (`Content-Type application/json`), param de lieu manquant (`Missing fid/cid/placeId`)…
Les 401/402/403/429 (clé/crédits/rate) restent propagés (vrais problèmes de config)."""
from __future__ import annotations

import pytest
from oto_mcp.mcp_errors import McpError
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
def tools(monkeypatch):
    """Enregistre le connecteur serper avec un client stubé (raise piloté)."""
    calls = {}

    class _Client:
        def __init__(self, *a, **k):
            ...

        def scrape_page(self, url, include_markdown=True, timeout_s=None):
            calls["url"] = url
            raise calls["exc"]

        def search_reviews(self, **kwargs):
            calls["kwargs"] = kwargs
            raise calls["exc"]

        # `serper_reviews` répond TOUT par défaut depuis la consolidation (op="all") :
        # le chemin nominal passe par `reviews_all`, pas par `search_reviews`.
        def reviews_all(self, **kwargs):
            calls["kwargs"] = kwargs
            raise calls["exc"]

    monkeypatch.setattr("oto.tools.serper.SerperClient", _Client)
    monkeypatch.setattr("oto_mcp.access.resolve_api_key", lambda p: ("k", False))
    # `serper_scrape` retente une lecture directe RÉELLE (`mail_obfuscation.repli`,
    # réseau + horloge non mockés) sur un 404/5xx amont — sans ce stub, les deux
    # tests qui empruntent cette branche dépendent d'un VRAI aller-retour vers
    # https://example.com/page : généralement trop court pour passer le seuil
    # `_EMPTY_TEXT_CHARS` (d'où le vert habituel), mais pas garanti — observé rouge
    # sous charge concurrente (3 suites ciblées en parallèle, 05/09/2026), vert en
    # isolation. Coupé ici plutôt que rendu "plus robuste" : ce test vérifie la mise
    # en forme de l'erreur, pas le comportement d'un repli réseau réel.
    monkeypatch.setattr("oto_mcp.tools.mail_obfuscation.repli",
                        lambda *a, **k: (None, "repli non exercé en test"))
    from oto_mcp.tools import serper
    reg = _Reg()
    serper.register(reg)
    return reg.tools, calls


@pytest.fixture()
def scrape(tools):
    fns, calls = tools
    return fns["serper_scrape"], calls


def test_scrape_5xx_becomes_managed_mcp_error(scrape):
    fn, calls = scrape
    calls["exc"] = RuntimeError("Serper scrape 500: Scraping failed.")
    with pytest.raises(McpError) as ei:
        fn("https://example.com/page")
    # message actionnable, pas un stacktrace ; et droppé par la taxonomie Sentry
    assert "Scrape impossible" in ei.value.error.message
    assert "example.com/page" in ei.value.error.message
    assert _is_expected_error(ei.value) is True


def test_scrape_404_becomes_managed_mcp_error(scrape):
    # 404 = l'URL ne mène à rien (page morte) → entrée invalide, pas un incident.
    # 1ᵉʳ contributeur de bruit Sentry avant ce mapping (37 événements).
    fn, calls = scrape
    calls["exc"] = RuntimeError("Serper scrape 404: Page not found.")
    with pytest.raises(McpError) as ei:
        fn("https://example.com/page-morte")
    assert "n'existe pas" in ei.value.error.message
    assert "page-morte" in ei.value.error.message
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


def test_scrape_400_becomes_managed_mcp_error(scrape):
    # 400 = URL non scrapable (JSON, page invalide) → entrée actionnable, pas un 500.
    fn, calls = scrape
    calls["exc"] = RuntimeError(
        'Serper scrape 400: URLs with Content-Type "application/json" currently not supported.')
    with pytest.raises(McpError) as ei:
        fn("https://api.example.com/data.json")
    assert "application/json" in ei.value.error.message
    assert _is_expected_error(ei.value) is True


@pytest.mark.parametrize("op", ["all", "page"])
def test_reviews_400_becomes_managed_mcp_error(tools, op):
    # 400 générique via `_run` (tout tool serper) : param de lieu manquant.
    # Les DEUX chemins (reviews_all par défaut, search_reviews sur op="page")
    # doivent rendre la même erreur gérée — sinon le défaut consolidé ferait
    # remonter un RuntimeError nu à Sentry.
    fns, calls = tools
    calls["exc"] = RuntimeError("Serper reviews 400: Missing \"fid\", \"cid\" or \"placeId\" parameter")
    with pytest.raises(McpError) as ei:
        fns["serper_reviews"](op=op, query="test")
    assert "placeId" in ei.value.error.message
    assert _is_expected_error(ei.value) is True
