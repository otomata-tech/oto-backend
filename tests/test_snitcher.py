"""Connecteur Snitcher — identification des visiteurs du site web
(api.snitcher.com/v1).

Verrouille : l'entrée de registre (keyed byo-only, catégorie Prospection), la
doc how-to, la surface MCP (5 tools — `snitcher_workspace`,
`snitcher_organisation`, `snitcher_contact`, `snitcher_session`,
`snitcher_custom_field` — chacun avec une description, régression du piège
f-string-docstring), la sonde « tester la connexion », la jointure
tool↔client oto-core (garde version-skew), et le dispatch `op=` (required
manquant refusé, arg non pertinent pour CET op refusé, exclusions
date/plage et organisation_uuid/domain).

Verrouille aussi, depuis le signalement #625 (30/08/2026), **le texte SERVI** de
`snitcher_organisation` et `snitcher_session` : le nom d'entreprise rendu est une
supposition amont que rien dans la charge utile ne permet de noter — cf. la
section « texte servi » plus bas pour le détail des faits et de leur source.
"""
import asyncio
from unittest.mock import patch

import pytest
from oto_mcp.mcp_errors import McpError
from oto_mcp import providers
from oto_mcp.connectors import verify as connector_verify
from oto_mcp.tool_visibility import namespace_of
from oto_mcp.tools import snitcher

EXPECTED_TOOLS = {
    "snitcher_workspace", "snitcher_organisation", "snitcher_contact",
    "snitcher_session", "snitcher_custom_field",
}


@pytest.fixture(scope="module")
def all_tools():
    from fastmcp import FastMCP
    from oto_mcp.tools import register_all

    m = FastMCP("t")
    register_all(m)
    return {t.name: t for t in asyncio.run(m._list_tools())}


@pytest.fixture(autouse=True)
def _fake_key(monkeypatch):
    monkeypatch.setattr(
        "oto_mcp.access.resolve_api_key", lambda provider, account=None: ("k", False))


def _fn_with_mock_client():
    """Enregistre le module avec `SnitcherClient` mocké, DANS le patch (sinon
    `register()`'s `from ... import SnitcherClient` capture la vraie classe
    avant que le patch ne s'applique)."""
    from fastmcp import FastMCP

    patcher = patch("oto.tools.snitcher.client.SnitcherClient")
    cls = patcher.start()
    m = FastMCP("t")
    snitcher.register(m)
    return m, cls, patcher


# --- registre -----------------------------------------------------------------

def test_snitcher_is_keyed_byo_only_connector():
    c = providers.REGISTRY["snitcher"]
    assert c.kind == "tools"
    assert c.keyed and c.secret_kind == "api_key"
    assert c.auth_modes == frozenset({"byo_user", "byo_org"})
    assert "platform" not in c.auth_modes
    assert c.default_active is False
    assert c.default_quota == 0
    assert "snitcher" in providers.KEY_PROVIDERS
    assert c.category == "Lead & company data"
    assert c.publisher_name == "Snitcher"
    assert c.label == "Snitcher"
    assert providers._LOGO_DOMAIN_BY_CONNECTOR["snitcher"] == "snitcher.com"
    assert [f.name for f in c.credential_fields] == ["key"]


def test_snitcher_has_onboarding_doc():
    kinds = {s.kind for s in providers.REGISTRY["snitcher"].doc_sections}
    assert {"prerequisite", "usage"} <= kinds


# --- surface MCP --------------------------------------------------------------

def test_snitcher_tools_register_under_namespace(all_tools):
    assert EXPECTED_TOOLS <= set(all_tools)
    assert all(namespace_of(t) == "snitcher" for t in all_tools if t.startswith("snitcher_"))


def test_snitcher_tools_all_have_descriptions(all_tools):
    for name in EXPECTED_TOOLS:
        assert all_tools[name].description, f"{name} has no description"


def test_verify_probe_registered():
    _fn_with_mock_client()
    assert connector_verify.supports("snitcher")


# --- texte servi : le nom d'entreprise est une supposition amont --------------
#
# Signalement #625 (30/08/2026). Une balayage des visiteurs a rendu, pour UNE
# localisation de visiteur (une ville d'Afrique australe), DEUX multinationales
# sans lien ; et entre deux relevés du même jour, des pages vues sont passées
# d'une ligne à l'autre. Le rapporteur en tire — à raison — qu'un nom sorti d'ici
# ne qualifie pas un lead seul.
#
# **Ce que fait notre code, vérifié à la source.** Rien. Chaque branche de
# `snitcher_organisation` rend `_run(lambda: client.<methode>(…))` (tools/
# snitcher.py:274-305) et `SnitcherClient._request` rend `resp.json()` tel quel
# (oto-core, oto/tools/snitcher/client.py:86). Aucune fusion, aucun
# dédoublonnage, aucun cache, aucune notation : l'identité rendue est celle que
# le fournisseur a résolue. Il n'y a donc rien à corriger dans le transport.
#
# **Ce que le contrat amont dit, et ce qu'il ne dit pas** (spec OpenAPI officiel
# app.snitcher.com/api/docs?api-docs.json, schéma `Organisation` — 18 champs) :
#   - l'objet ne porte AUCUN qualificatif de match : ni `confidence`, ni
#     `match_type`, ni `ip`, ni `type`. Le contraste est net — l'autre surface de
#     Snitcher (IP2Company) rend, elle, `fuzzy` (bool) et
#     `type: business|isp|educational|government`, et son exemple officiel de
#     non-match est `{"fuzzy": false, "domain": null, "type": "isp"}`. Ces
#     qualificatifs EXISTENT chez le fournisseur et ne traversent pas jusqu'ici.
#   - Snitcher documente lui-même que les IP d'ISP grand public, de VPN et de
#     réseaux mobiles sont partagées et non identifiables
#     (docs.snitcher.com/product/how-snitcher-works), et son propre produit
#     propose « ISP, Public Place, Customer, Identification inaccurate » comme
#     motifs de suppression d'une société.
#   - `visitor_locations` est un simple `array of string` (ex. `["Amsterdam,
#     NL"]`) : deux sociétés sans lien sous la même ville sont un résultat que le
#     contrat AUTORISE, pas une anomalie.
#
# ⚠️ **Là où l'hypothèse du rapporteur ne tient pas** — et c'est ce qui rend la
# mise en garde délicate : il lit le déplacement des pages vues comme une
# ré-attribution rétroactive. Rien ne l'établit. `total_pageviews` n'a AUCUNE
# description dans le spec : personne ne dit s'il compte tout l'historique ou
# seulement la fenêtre `date`/`date_from`/`date_to` de l'appel, et aucune
# stabilité n'est promise entre deux lectures. Écrire « les compteurs se
# ré-attribuent entre sociétés » affirmerait un mécanisme que le fournisseur ne
# documente nulle part, et masquerait la cause banale et vérifiable : un champ
# dont la sémantique n'est pas définie. Le texte servi dit donc l'absence de
# garantie, pas une garantie contraire.
#
# Classement : comportement du fournisseur servi brut ⟹ le remède est de le DIRE
# (oto#42), pas de corriger un transport qui ne transforme rien. Ces bancs
# verrouillent la phrase dans le texte SERVI et non dans la docstring : fastmcp
# jette toute prose placée après le bloc `Args:` (cf.
# test_docstring_prose_served.py), donc une mise en garde bien écrite mais mal
# placée n'atteindrait aucun agent. Éprouvé : déplacée après `Args:`, elle
# disparaît du texte servi et les trois cas ci-dessous rougissent.


def _description_servie(nom: str) -> str:
    """Le texte que `tools/list` rend au modèle — PAS `inspect.getdoc` : le
    harnais retire le bloc `Args:` et jette ce qui le suit."""
    from fastmcp import FastMCP

    m = FastMCP("t")
    snitcher.register(m)
    return asyncio.run(m.get_tool(nom)).description or ""


def _mise_en_garde(servie: str) -> str:
    """La partie ⚠️ de la description — le texte le plus proche du geste.

    Une mention noyée ailleurs ne compterait pas : l'agent qui lit une ligne
    d'organisation doit trouver l'avertissement AVEC elle."""
    assert "⚠️" in servie, "aucune mise en garde dans le texte servi"
    return servie[servie.index("⚠️"):]


def test_organisation_dit_que_le_nom_ne_qualifie_pas_un_lead_seul():
    """Le remède du #625, servi au modèle : un second signal avant de qualifier.

    La description ouvre sur « Companies Snitcher identified visiting … » — un
    agent y lit une identification établie, et rien ne le détrompe."""
    garde = _mise_en_garde(_description_servie("snitcher_organisation")).lower()
    assert "second signal" in garde, (
        "le texte servi ne nomme pas le remède — un agent qui lit « Companies "
        "Snitcher identified » prend le nom pour un fait vérifié")


def test_organisation_nomme_les_deux_champs_qui_ont_trompe():
    """`visitor_locations` (la collision) et `total_pageviews` (le compteur sans
    sémantique définie) sont des jetons d'API stables, pas de la prose : c'est ce
    qui rend ce banc vérifiable sans figer une tournure — et c'est sous ces deux
    noms exacts que l'agent reverra le cas."""
    servie = _description_servie("snitcher_organisation")
    for champ in ("visitor_locations", "total_pageviews"):
        assert champ in servie, (
            f"`{champ}` n'est pas nommé dans le texte servi — l'agent ne peut pas "
            f"relier ce qu'il lit à la mise en garde")


def test_session_herite_de_la_supposition_quand_il_narrow_sur_une_organisation():
    """Le tool qui SUBIT le plus la supposition est celui qui attribue un
    comportement à une société : `organisation_uuid` n'est pas une clé métier,
    c'est le résultat d'une résolution amont."""
    garde = _mise_en_garde(_description_servie("snitcher_session"))
    assert "organisation_uuid" in garde, (
        "la mise en garde de snitcher_session ne vise pas le narrowing par "
        "organisation_uuid — c'est pourtant là que le nom devient une conclusion")


# --- jointure tool ↔ client oto-core (garde version-skew) ---------------------

def test_client_exposes_methods_called_by_tools():
    from oto.tools.snitcher.client import SnitcherClient
    for meth in ("get_me", "list_workspaces", "create_workspace", "get_workspace",
                 "update_workspace", "delete_workspace", "invite_user",
                 "create_workspace_tag", "list_organisations", "filter_organisations",
                 "get_organisation", "add_organisation_tag", "remove_organisation_tag",
                 "list_contacts", "reveal_contact_email", "list_sessions",
                 "list_organisation_sessions", "list_segments", "list_custom_fields",
                 "create_custom_field", "get_custom_field", "update_custom_field",
                 "delete_custom_field", "list_custom_field_values",
                 "set_custom_field_values", "set_custom_field_value",
                 "clear_custom_field_value"):
        assert callable(getattr(SnitcherClient, meth, None)), f"SnitcherClient.{meth} manquant"


# --- dispatch op= : snitcher_workspace ---------------------------------------

def test_workspace_list_refuses_target_params():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("snitcher_workspace")).fn
        with pytest.raises(McpError, match="op='list' does not use"):
            fn(op="list", workspace_uuid="ws_1")
        cls.return_value.list_workspaces.assert_not_called()

        cls.return_value.list_workspaces.return_value = {"data": []}
        fn(op="list", page=2, size=50)
        cls.return_value.list_workspaces.assert_called_once_with(page=2, size=50)
    finally:
        patcher.stop()


def test_workspace_me_and_segments():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("snitcher_workspace")).fn
        cls.return_value.get_me.return_value = {"data": {"name": "J"}}
        assert fn(op="me") == {"data": {"name": "J"}}

        with pytest.raises(McpError, match="requires .workspace_uuid."):
            fn(op="segments")
        cls.return_value.list_segments.return_value = {"data": []}
        fn(op="segments", workspace_uuid="ws_1")
        cls.return_value.list_segments.assert_called_once_with("ws_1")
    finally:
        patcher.stop()


def test_workspace_create_update_invite_create_tag_required_params():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("snitcher_workspace")).fn
        with pytest.raises(McpError, match="requires .url."):
            fn(op="create")
        with pytest.raises(McpError, match="requires .usage_limit."):
            fn(op="update", workspace_uuid="ws_1")
        with pytest.raises(McpError, match="requires .email."):
            fn(op="invite", workspace_uuid="ws_1")
        with pytest.raises(McpError, match="requires .tag_name."):
            fn(op="create_tag", workspace_uuid="ws_1")

        cls.return_value.create_workspace.return_value = {"data": {"uuid": "ws_new"}}
        fn(op="create", url="https://example.com")
        cls.return_value.create_workspace.assert_called_once_with("https://example.com")

        cls.return_value.delete_workspace.return_value = None
        fn(op="delete", workspace_uuid="ws_1")
        cls.return_value.delete_workspace.assert_called_once_with("ws_1")
    finally:
        patcher.stop()


# --- dispatch op= : snitcher_organisation ------------------------------------

def test_organisation_list_date_exclusivity_and_search():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("snitcher_organisation")).fn
        with pytest.raises(McpError, match="mutually exclusive"):
            fn(workspace_uuid="ws_1", op="list", date="2026-08-01", date_from="2026-07-01")

        with pytest.raises(McpError, match="requires .filters."):
            fn(workspace_uuid="ws_1", op="search")

        filters = {"operator": "AND", "conditions": [
            {"field": "employees", "comparison": "greater_than", "value": 200}]}
        cls.return_value.filter_organisations.return_value = {"data": []}
        fn(workspace_uuid="ws_1", op="search", filters=filters, size=100)
        cls.return_value.filter_organisations.assert_called_once_with(
            "ws_1", filters, segment_uuid=None, page=None, size=100)
    finally:
        patcher.stop()


def test_organisation_get_tag_untag():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("snitcher_organisation")).fn
        with pytest.raises(McpError, match="requires .organisation_uuid."):
            fn(workspace_uuid="ws_1", op="get")
        with pytest.raises(McpError, match="requires .tag_name."):
            fn(workspace_uuid="ws_1", op="tag", organisation_uuid="org_1")

        cls.return_value.add_organisation_tag.return_value = None
        fn(workspace_uuid="ws_1", op="tag", organisation_uuid="org_1", tag_name="hot")
        cls.return_value.add_organisation_tag.assert_called_once_with("ws_1", "org_1", "hot")

        cls.return_value.remove_organisation_tag.return_value = None
        fn(workspace_uuid="ws_1", op="untag", organisation_uuid="org_1", tag_name="hot")
        cls.return_value.remove_organisation_tag.assert_called_once_with("ws_1", "org_1", "hot")
    finally:
        patcher.stop()


# --- dispatch : snitcher_contact ---------------------------------------------

def test_contact_list_requires_exactly_one_of_org_domain():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("snitcher_contact")).fn
        with pytest.raises(McpError, match="EXACTLY ONE"):
            fn(workspace_uuid="ws_1", op="list")
        with pytest.raises(McpError, match="EXACTLY ONE"):
            fn(workspace_uuid="ws_1", op="list", organisation_uuid="org_1", domain="acme.com")

        cls.return_value.list_contacts.return_value = {"data": []}
        fn(workspace_uuid="ws_1", op="list", domain="acme.com")
        cls.return_value.list_contacts.assert_called_once_with(
            "ws_1", organisation_uuid=None, domain="acme.com", page=None, size=None)
    finally:
        patcher.stop()


def test_contact_reveal_email_requires_contact_uuid():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("snitcher_contact")).fn
        with pytest.raises(McpError, match="requires .contact_uuid."):
            fn(workspace_uuid="ws_1", op="reveal_email")
        with pytest.raises(McpError, match="does not use"):
            fn(workspace_uuid="ws_1", op="reveal_email", contact_uuid="c_1", domain="acme.com")

        cls.return_value.reveal_contact_email.return_value = {"data": {"email": "j@acme.com"}}
        fn(workspace_uuid="ws_1", op="reveal_email", contact_uuid="c_1")
        cls.return_value.reveal_contact_email.assert_called_once_with("ws_1", "c_1")
    finally:
        patcher.stop()


# --- dispatch : snitcher_session ---------------------------------------------

def test_session_routes_to_org_or_workspace_endpoint():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("snitcher_session")).fn
        with pytest.raises(McpError, match="mutually exclusive"):
            fn(workspace_uuid="ws_1", date="2026-08-01", date_to="2026-08-10")
        with pytest.raises(McpError, match="workspace-wide"):
            fn(workspace_uuid="ws_1", organisation_uuid="org_1", segment_uuid="seg_1")

        cls.return_value.list_organisation_sessions.return_value = {"data": []}
        fn(workspace_uuid="ws_1", organisation_uuid="org_1")
        cls.return_value.list_organisation_sessions.assert_called_once()

        cls.return_value.list_sessions.return_value = {"data": []}
        fn(workspace_uuid="ws_1", date="2026-08-01")
        cls.return_value.list_sessions.assert_called_once()
    finally:
        patcher.stop()


# --- dispatch : snitcher_custom_field ----------------------------------------

def test_custom_field_definition_ops():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("snitcher_custom_field")).fn
        with pytest.raises(McpError, match="requires .name. and .type."):
            fn(workspace_uuid="ws_1", op="create")
        with pytest.raises(McpError, match="requires .key."):
            fn(workspace_uuid="ws_1", op="get")
        with pytest.raises(McpError, match="immutable"):
            fn(workspace_uuid="ws_1", op="update", key="tier", type="text")

        cls.return_value.create_custom_field.return_value = {"data": {"key": "industry"}}
        fn(workspace_uuid="ws_1", op="create", name="Industry", type="text")
        cls.return_value.create_custom_field.assert_called_once_with(
            "ws_1", "Industry", "text", key=None, description=None,
            visible_in_spotter=None, field_rules=None, options=None)
    finally:
        patcher.stop()


def test_custom_field_value_ops_require_organisation():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("snitcher_custom_field")).fn
        with pytest.raises(McpError, match="requires .organisation_uuid."):
            fn(workspace_uuid="ws_1", op="values")
        with pytest.raises(McpError, match="requires .key. and .value."):
            fn(workspace_uuid="ws_1", op="set", organisation_uuid="org_1")
        with pytest.raises(McpError, match="requires .values."):
            fn(workspace_uuid="ws_1", op="set_many", organisation_uuid="org_1")

        cls.return_value.set_custom_field_value.return_value = {"data": {}}
        fn(workspace_uuid="ws_1", op="set", organisation_uuid="org_1",
           key="account_tier", value="enterprise")
        cls.return_value.set_custom_field_value.assert_called_once_with(
            "ws_1", "org_1", "account_tier", "enterprise")

        cls.return_value.set_custom_field_values.return_value = {"data": {}}
        fn(workspace_uuid="ws_1", op="set_many", organisation_uuid="org_1",
           values={"deal_size": 50000})
        cls.return_value.set_custom_field_values.assert_called_once_with(
            "ws_1", "org_1", {"deal_size": 50000})

        cls.return_value.clear_custom_field_value.return_value = None
        fn(workspace_uuid="ws_1", op="clear", organisation_uuid="org_1", key="account_tier")
        cls.return_value.clear_custom_field_value.assert_called_once_with(
            "ws_1", "org_1", "account_tier")
    finally:
        patcher.stop()
