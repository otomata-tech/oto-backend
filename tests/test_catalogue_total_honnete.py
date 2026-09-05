"""`oto_list_my_tools` : le `total` décrit ce qu'il accompagne (oto#42, entrée 1).

Le champ valait le CATALOGUE ENTIER, calculé AVANT le filtrage. Sur une recherche, la
réponse portait donc un champ littéralement nommé `total` qui ne décrivait pas ce
qu'elle rendait, un `shown` plafonné à 40, et **jamais** le nombre de correspondances
— pourtant disponible trois lignes plus bas. C'est la forme exacte du « 92 résultats,
10 rendus » corrigé ailleurs, en pire : là le total manquait, ici il était présent et
faux. Et c'est la surface par laquelle un agent découvre ce qu'il sait faire.

⚠️ Ces bancs exécutent le tool RÉELLEMENT MONTÉ (`_test_mcp().get_tool`), jamais une
fonction recopiée : c'est la leçon du 03/09 — un banc est une mesure, et il doit lire
ce que le montage expose, pas l'objet intermédiaire dont on suppose qu'il le reflète.
"""
from __future__ import annotations

from _mcp_app import static_mcp as _test_mcp

import fastmcp as _fc
import pytest

from oto_mcp import server
from oto_mcp.tools import meta as _meta

_SUB = "u-catalogue"


@pytest.fixture()
def compte(monkeypatch):
    """Un compte nu : aucun outil désactivé, aucune denylist, aucun écart de boîte."""
    monkeypatch.setattr(_meta, "current_user_sub_from_token", lambda: _SUB)
    monkeypatch.setattr(_meta.db, "list_user_disabled_tools", lambda s, o=None: [])
    monkeypatch.setattr(_meta.db, "list_user_enabled_tools", lambda s, o=None: [])
    monkeypatch.setattr(_meta.access, "current_org", lambda s: None)
    monkeypatch.setattr(_meta.access, "org_admin_hidden_tools", lambda o: set())
    monkeypatch.setattr(_meta.access, "current_group", lambda s: None)
    monkeypatch.setattr(_meta.access, "group_admin_hidden_tools", lambda g: set())
    from oto_mcp.capabilities.connectors import selection
    monkeypatch.setattr(selection, "_toolbox_scope", lambda sub: None)


async def _appelle(args: dict) -> dict:
    tool = await _test_mcp().get_tool("oto_list_my_tools")
    async with _fc.Context(fastmcp=_test_mcp()):
        return (await tool.run(args)).structured_content


@pytest.mark.asyncio
async def test_sans_recherche_le_total_est_le_catalogue(compte):
    """Le cas nominal ne change pas : sans filtre, `total` EST le catalogue."""
    out = await _appelle({})
    assert out["total"] == len(out["tools"]) or out["total"] >= out["shown"]
    assert "catalog_total" not in out, "rien n'a été écarté : pas de champ parasite"


@pytest.mark.asyncio
async def test_sur_une_recherche_le_total_compte_les_CORRESPONDANCES(compte):
    """LE défaut. `total` doit compter ce que la recherche a trouvé, pas le catalogue
    — sinon un agent lit « 350 » sur une réponse qui en porte trois."""
    out = await _appelle({"query": "datastore"})
    assert out["total"] < out["catalog_total"], (
        "le total décrit encore le catalogue entier : un agent croira que sa "
        "recherche a ramené tout oto")
    assert out["total"] >= out["shown"]


@pytest.mark.asyncio
async def test_ce_que_le_filtre_a_ecarte_est_DIT(compte):
    """Sans le catalogue en regard, « 3 outils » se lit « oto n'en a que 3 ». Le
    chiffre écarté ne disparaît pas, il change de nom."""
    out = await _appelle({"query": "datastore"})
    assert out["catalog_total"] > out["total"]
    assert "catalog_disabled_count" in out


@pytest.mark.asyncio
async def test_une_reponse_TRONQUEE_le_dit(compte):
    """La branche « trop de résultats » était la seule non traitée : la branche zéro
    rendait la carte des namespaces, celle-ci ne disait rien. Un agent qui reçoit 2
    entrées sur 40 correspondances doit l'apprendre de la réponse."""
    out = await _appelle({"query": "a", "limit": 2})
    assert out["shown"] == 2
    if out["total"] > 2:
        assert out.get("truncated") is True
        assert str(out["total"]) in out["hint_truncated"]
        assert "limit" in out["hint_truncated"], "le hint doit dire le GESTE"
