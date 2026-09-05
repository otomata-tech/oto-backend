"""Lire les leads d'une campagne rend ce qu'elle contient, pas une liste vide (#719).

Le défaut de lemlist sur `state` **filtre tout** et rend une liste vide qui se lit
« pas de leads sur cette campagne » — silencieux, et faux. Vérifié en live le
2026-08-31 côté client, où `export_campaign_leads` porte depuis lors `state="all"`.

Le guide du connecteur annonçait ce forçage comme acquis **pour le connecteur entier**.
Il ne l'était que sur la route d'export : les deux autres routes de lecture rendaient
donc `[]` sur une campagne pleine, pendant que la route unitaire rendait le lead très
bien (signal 719, 04/09/2026). C'est la forme la plus coûteuse du défaut — une réponse
bien formée dont le contenu ment.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch


def _appel(nom, **kwargs):
    from fastmcp import FastMCP

    from oto_mcp.tools import lemlist
    m = FastMCP("t")
    lemlist.register(m)
    fn = asyncio.run(m.get_tool(nom)).fn
    return fn(**kwargs)


def _client():
    return (patch("oto_mcp.access.resolve_api_key", return_value=("k", False)),
            patch("oto.tools.lemlist.LemlistClient"))


def test_op_list_force_state_all_quand_on_ne_demande_rien():
    """La garde : sans ça, une campagne pleine se lit vide."""
    key, cls = _client()
    with key, cls as C:
        C.return_value.get_campaign_leads.return_value = [{"id": "lea_1"}]
        _appel("lemlist_lead", op="list", campaign_id="cam_1")
        assert C.return_value.get_campaign_leads.call_args.kwargs["state"] == "all"


def test_un_state_explicite_reste_maitre():
    """Filtrer délibérément doit rester possible — la garde vise l'omission, pas
    l'intention."""
    key, cls = _client()
    with key, cls as C:
        C.return_value.get_campaign_leads.return_value = []
        _appel("lemlist_lead", op="list", campaign_id="cam_1", state="replied")
        assert C.return_value.get_campaign_leads.call_args.kwargs["state"] == "replied"


def test_get_leads_passe_par_la_route_qui_porte_la_garde():
    """`get_all_leads` appelle l'export SANS `state` : il hérite du défaut qui filtre
    tout. On prend la surface qui a déjà la garde plutôt que d'en refaire une."""
    key, cls = _client()
    with key, cls as C:
        C.return_value.export_campaign_leads.return_value = [{"id": "lea_1"}]
        out = _appel("lemlist_get_leads", campaign_id="cam_1")
        C.return_value.get_all_leads.assert_not_called()
        assert C.return_value.export_campaign_leads.call_args.kwargs["format"] == "json"
        assert out["leads"] == [{"id": "lea_1"}]


def test_le_client_porte_bien_le_defaut_all_sur_la_route_retenue():
    """Garde de version-skew : le forçage vit dans `export_campaign_leads` côté
    client. Si un bump d'oto-core le retirait, les deux routes redeviendraient
    silencieusement vides — et aucun banc de surface ne le verrait."""
    import inspect

    from oto.tools.lemlist import LemlistClient
    sig = inspect.signature(LemlistClient.export_campaign_leads)
    doc = inspect.getdoc(LemlistClient.export_campaign_leads) or ""
    assert "state" in sig.parameters
    assert "all" in doc, (
        "la route retenue doit documenter son défaut `state=\"all\"` — c'est la seule "
        "chose qui empêche une lecture de rendre vide une campagne pleine")
