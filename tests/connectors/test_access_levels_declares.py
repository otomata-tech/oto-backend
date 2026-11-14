"""CLIQUET : tout outil SERVI d'un connecteur à niveaux a sa déclaration de scopes.

La population vient du MONTAGE (`register_all` + capacités, comme le boot), jamais d'une
liste écrite ici : un outil ajouté demain à `zoho` ou `zohodesk` sans déclaration rougit
ce banc — sinon, sous un niveau restreint, il serait masqué et refusé (fail-closed) sans
que personne ait décidé de ce qu'il exige.
"""
from __future__ import annotations

import asyncio

import pytest
from fastmcp import FastMCP

from oto_mcp.connectors import access_levels as al


@pytest.fixture(scope="module")
def montes() -> dict:
    from oto_mcp.capabilities import _mcp_adapter, registry
    from oto_mcp.tools import register_all

    mcp = FastMCP("sonde-niveaux")
    register_all(mcp)
    _mcp_adapter.register(mcp, registry.CAPABILITIES)

    async def _lire():
        return {t.name: t for t in await mcp.list_tools(run_middleware=False)}
    return asyncio.run(_lire())


def _population(montes: dict) -> dict[str, list[str]]:
    par_connecteur: dict[str, list[str]] = {}
    for nom in montes:
        decl = al.for_tool(nom)
        if decl is not None:
            par_connecteur.setdefault(decl.connector, []).append(nom)
    return par_connecteur


def test_il_y_a_des_connecteurs_a_niveaux(montes):
    assert set(_population(montes)) == set(al.DECLARED) == {"zoho", "zohodesk", "payfit", "zohosign",
                                                         "google_search_console"}


def test_tout_outil_servi_a_sa_declaration(montes):
    manquants = sorted(n for noms in _population(montes).values() for n in noms
                       if n not in al.for_tool(n).tools)
    assert not manquants, (
        f"{manquants} : outil(s) servi(s) d'un connecteur à niveaux sans scopes déclarés "
        f"(`ACCESS_LEVELS` de `providers/<nom>.py`).")


def test_aucune_declaration_fantome(montes):
    fantomes = sorted(t for decl in al.DECLARED.values() for t in decl.tools
                      if t not in montes)
    assert not fantomes, f"{fantomes} : déclarés, jamais servis"


def test_les_ops_declarees_sont_celles_du_schema_servi(montes):
    """Un outil multiplexé déclare SES ops, toutes : une op servie non déclarée serait
    refusée sous `read` sans décision, une op déclarée non servie mentirait."""
    for decl in al.DECLARED.values():
        for tool, ops in decl.tools.items():
            prop = (montes[tool].parameters.get("properties") or {}).get(al.OP_PARAM)
            if isinstance(ops, dict):
                assert prop is not None, f"{tool} déclaré op par op sans paramètre `op`"
                assert set(ops) == set(prop["enum"]), tool


def test_la_garde_mord_sur_un_outil_non_declare():
    """Contre-épreuve : un outil du connecteur que la déclaration ignore n'est couvert à
    AUCUN niveau — c'est ce qui rend le premier test nécessaire."""
    decl = al.DECLARED["zoho"]
    assert not decl.covers(al.FULL, "zoho_nouvel_outil")


def test_les_outils_que_full_ne_couvre_pas_sont_nommes():
    """L'écart connu, nommé plutôt que tu : le consentement historique du Desk n'accorde
    aucun scope d'écriture, ses trois outils d'écriture ne sont donc couverts par AUCUN
    niveau. `full` ne filtre rien, ils restent servis (et Zoho les refuse, comme avant).
    Corriger = ajouter les scopes `WRITE` au niveau `full` — une décision, pas ce lot."""
    hors = sorted(t for decl in al.DECLARED.values() for t in decl.tools
                  if (decl.is_op_tool(t) and decl.covered_ops(al.FULL, t) != set(decl.tools[t]))
                  or (not decl.is_op_tool(t) and not decl.covers(al.FULL, t)))
    assert hors == ["zohodesk_create_contact", "zohodesk_create_ticket",
                    "zohodesk_update_ticket"]
