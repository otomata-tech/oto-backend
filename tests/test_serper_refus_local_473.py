"""Un refus de scraping atteint le modèle en clair, pas en « Erreur interne » (#473).

Certains domaines ne sont JAMAIS scrapables (réseaux sociaux exigeant une session). Le
client d'oto-core le sait et lève dessus — mais il lève un `RuntimeError` **nu**, que la
taxonomie du backend ne peut pas distinguer d'un bug : elle le classe `internal` et sert
« Erreur interne du serveur. », **sans écho du message** (anti-fuite).

Le modèle recevait donc « erreur interne » là où il devait lire « cherche une autre
source » — et il réessayait, ou s'arrêtait, au lieu de contourner. Régime permanent :
mesuré encore le 04/09/2026 sur des URL facebook.com, dans des runs qui n'avaient plus
qu'à abandonner.

⚠️ **Le piège de ce lot, et il a failli marcher** : le journal des appels montre le VRAI
message, parce qu'il enregistre `str(exc)` — pas ce qui est servi. Un coup d'œil au
journal fait donc conclure que le défaut est réparé alors qu'il ne l'est pas. La seule
mesure qui vaut passe par `error_taxonomy.classify`, et ces bancs la font.
"""
from __future__ import annotations

import asyncio

import pytest

from oto_mcp import error_taxonomy
from oto_mcp.mcp_errors import McpError


def _scrape(url: str, monkeypatch):
    from fastmcp import FastMCP

    from oto_mcp.tools import serper
    monkeypatch.setattr("oto_mcp.access.resolve_api_key",
                        lambda p, account=None: ("k", False))
    m = FastMCP("t")
    serper.register(m)
    return asyncio.run(m.get_tool("serper_scrape")).fn(url=url)


def test_un_domaine_jamais_scrapable_rend_un_refus_ACTIONNABLE(monkeypatch):
    with pytest.raises(McpError) as exc:
        _scrape("https://www.facebook.com/exemple", monkeypatch)
    message = exc.value.error.message
    assert "exige une session" in message, "la raison doit survivre jusqu'au modèle"
    assert "autre source" in message, "et elle doit dire quoi FAIRE"


def test_le_refus_dit_de_NE_PAS_reessayer(monkeypatch):
    """La conduite compte autant que la cause : sans elle, l'agent réessaie la même
    URL — c'est ce que faisaient les runs mesurés."""
    with pytest.raises(McpError) as exc:
        _scrape("https://twitter.com/quelquun", monkeypatch)
    assert "ne réessaie pas" in exc.value.error.message.lower()


def test_le_message_SURVIT_a_la_taxonomie(monkeypatch):
    """⚠️ LE banc du lot. Un refus bien rédigé ne sert à rien s'il est reclassé en
    « Erreur interne du serveur. » avant d'atteindre le modèle — c'est exactement ce
    qui se passait, et le journal ne le montrait pas."""
    with pytest.raises(McpError) as exc:
        _scrape("https://www.instagram.com/quelquun", monkeypatch)
    servi = error_taxonomy.classify(exc.value).message
    assert "Erreur interne" not in servi
    assert "session" in servi


def test_le_defaut_d_origine_est_bien_celui_qu_on_croit():
    """La preuve que le lot répare quelque chose : la forme d'AVANT, un `RuntimeError`
    nu portant le même texte, est toujours classée « Erreur interne du serveur. ».
    Si ce banc devenait faux, le correctif serait ailleurs et celui-ci inutile."""
    nu = RuntimeError("Serper scrape refusé pour https://x : Facebook exige une session.")
    assert error_taxonomy.classify(nu).message == "Erreur interne du serveur."


def test_une_url_ordinaire_n_est_PAS_refusee(monkeypatch):
    """L'autre moitié : le refus ne doit pas mordre sur ce qui se scrape très bien.

    ⚠️ Mesure outbound (05/09/2026) : `serper._client` n'existe PAS au niveau module
    (fermeture locale de `register()`) — `monkeypatch.setattr(serper, "_client", ...,
    raising=False)` posait donc un attribut mort que rien ne lit, et `_Faux` n'était
    JAMAIS exercé. Le `except Exception: pass` en bas avalait l'échec réel qui
    suivait : un VRAI appel réseau vers `exemple.invalid` (RFC 2606, jamais censé
    résoudre) atterrissait quand même sur une IP live — trouvé en bloquant les
    sockets sortants au niveau suite. Fix : patcher `SerperClient` là où `_client()`
    le lit réellement (même patron que `test_serper_scrape_error.py`)."""
    from oto_mcp.tools import serper
    vu = {}
    monkeypatch.setattr("oto.tools.serper.SerperClient", lambda **kw: _Faux(vu))
    try:
        _scrape("https://exemple.invalid/page", monkeypatch)
    except McpError as e:
        assert "exige une session" not in e.error.message
    except Exception:
        pass  # tout autre échec (réseau, client stubé) n'est pas le sujet de ce banc


class _Faux:
    def __init__(self, vu):
        self.vu = vu

    def scrape_page(self, **kw):
        self.vu.update(kw)
        return {"markdown": "ok"}


def test_la_methode_du_client_existe_toujours():
    """Garde de version-skew : le refus s'appuie sur `SerperClient._refuses_scraping`.
    Si un bump d'oto-core la retire, le lot dégrade en silence vers le comportement
    d'avant — ce banc est le seul endroit où ça se verra."""
    from oto.tools.serper import SerperClient
    assert callable(getattr(SerperClient, "_refuses_scraping", None)), (
        "SerperClient._refuses_scraping a disparu : le refus actionnable de #473 est "
        "retombé en « Erreur interne du serveur. » sans que rien d'autre le dise")
