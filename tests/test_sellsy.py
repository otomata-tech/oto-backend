"""Connecteur Sellsy — CRM + gestion commerciale FR (api.sellsy.com/v2).

Verrouille : l'entrée de registre (credential multi-champs byo-only), la surface
MCP sous le namespace `sellsy`, la jointure tool↔client oto-core (garde
version-skew), la sonde « tester la connexion », et les endroits où le module ne
se contente PAS de passer le plat — un tool par objet métier avec le verbe en
`op` (ADR 0047), un seul tool pour les quatre documents de vente comme pour les
deux faces du tiers (société / particulier), et les verbes qui n'existent que
pour certains d'entre eux (`validate` ≠ `status`).

Le dispatch op par op vit dans `test_sellsy_op_dispatch.py`.
"""
import asyncio
from unittest.mock import patch

import pytest

from oto_mcp import providers
from oto_mcp.connectors import verify as connector_verify
from oto_mcp.tool_visibility import namespace_of

EXPECTED_TOOLS = {
    "sellsy_third_party", "sellsy_contact", "sellsy_opportunity",
    "sellsy_document", "sellsy_payment", "sellsy_item", "sellsy_task",
    "sellsy_ref", "sellsy_search",
}


@pytest.fixture(scope="module")
def all_tools():
    from fastmcp import FastMCP
    from oto_mcp.tools import register_all

    m = FastMCP("t")
    register_all(m)
    return {t.name for t in asyncio.run(m._list_tools())}


def _tool(name):
    from fastmcp import FastMCP
    from oto_mcp.tools import sellsy

    m = FastMCP("t")
    sellsy.register(m)
    return asyncio.run(m.get_tool(name))


# --- registre -----------------------------------------------------------------

def test_sellsy_is_a_multi_field_byo_connector():
    c = providers.REGISTRY["sellsy"]
    assert c.kind == "tools"
    # client_credentials : le secret posé n'est PAS la clé d'appel (jeton dérivé)
    # → credential multi-champs résolu par resolve_credential_fields, pas keyed.
    assert not c.keyed and c.secret_kind == "fields"
    assert "sellsy" not in providers.KEY_PROVIDERS
    assert {f.name for f in c.credential_fields} == {"client_id", "client_secret"}
    assert all(f.secret for f in c.credential_fields)
    # byo-only : un compte Sellsy est celui d'une entreprise, rien à partager.
    assert c.auth_modes == frozenset({"byo_user", "byo_org"})
    assert c.category == "CRM"
    assert c.publisher_name == "Sellsy"


def test_sellsy_has_onboarding_doc():
    kinds = {s.kind for s in providers.REGISTRY["sellsy"].doc_sections}
    assert {"prerequisite", "usage"} <= kinds


# --- surface MCP --------------------------------------------------------------

def test_sellsy_tools_register_under_namespace(all_tools):
    assert EXPECTED_TOOLS <= all_tools
    assert {t for t in all_tools if t.startswith("sellsy_")} == EXPECTED_TOOLS
    assert all(namespace_of(t) == "sellsy"
               for t in all_tools if t.startswith("sellsy_"))


def test_verify_probe_registered(all_tools):
    # La sonde s'enregistre dans `register(mcp)` — au boot, `_build_mcp` → `register_all`.
    # `all_tools` rejoue ce chemin : sans lui, ce test ne passait que si un AUTRE test du
    # même processus l'avait déjà fait (rouge isolé, révélé par la suite en parts, #1111).
    assert connector_verify.supports("sellsy")


# --- jointure tool ↔ client oto-core (garde version-skew) ---------------------

def test_client_exposes_methods_called_by_tools():
    from oto.tools.sellsy import SellsyClient
    for meth in ("list_records", "search_records", "list_all", "get_record",
                 "create_record", "update_record", "delete_record", "list_sub",
                 "act", "get_custom_fields", "set_custom_fields",
                 "link_contact_to_company", "unlink_contact_from_company",
                 "global_search", "smart_tags_autocomplete"):
        assert callable(getattr(SellsyClient, meth, None)), \
            f"SellsyClient.{meth} manquant"


# --- dispatch -----------------------------------------------------------------

def _with_fake_client():
    creds = patch("oto_mcp.access.resolve_credential_fields",
                  return_value={"client_id": "id", "client_secret": "sec"})
    cls = patch("oto.tools.sellsy.SellsyClient")
    return creds, cls


def test_search_goes_through_the_filtered_endpoint():
    creds, cls = _with_fake_client()
    with creds, cls as client_cls:
        inst = client_cls.return_value
        _tool("sellsy_third_party").fn(kind="company", op="search",
                                       filters={"name": "acme"}, limit=50)

        assert inst.search_records.call_args.args[0] == "companies"
        assert inst.search_records.call_args.args[1] == {"name": "acme"}


def test_all_pages_switches_to_the_paginating_helper():
    creds, cls = _with_fake_client()
    with creds, cls as client_cls:
        inst = client_cls.return_value
        _tool("sellsy_task").fn(op="list", all_pages=True, max_pages=3)

        assert inst.list_all.call_args.kwargs["max_pages"] == 3
        assert not inst.list_records.called


def test_documents_share_one_tool_and_map_to_their_resource():
    creds, cls = _with_fake_client()
    with creds, cls as client_cls:
        inst = client_cls.return_value
        tool = _tool("sellsy_document")

        tool.fn(kind="credit_note", op="list")
        assert inst.list_records.call_args.args[0] == "credit-notes"

        tool.fn(kind="estimate", op="get", record_id=3)
        assert inst.get_record.call_args.args[0] == "estimates"


def test_unknown_document_kind_is_rejected():
    from oto_mcp.mcp_errors import McpError
    creds, cls = _with_fake_client()
    with creds, cls:
        with pytest.raises(McpError, match="kind"):
            _tool("sellsy_document").fn(kind="quote", op="list")


def test_validate_and_status_are_not_offered_on_every_document():
    """`validate` (facture/avoir) et `status` (devis) ne sont pas interchangeables :
    laisser passer l'un pour l'autre produirait un 404 opaque côté Sellsy."""
    from oto_mcp.mcp_errors import McpError
    creds, cls = _with_fake_client()
    with creds, cls as client_cls:
        inst = client_cls.return_value
        tool = _tool("sellsy_document")

        with pytest.raises(McpError, match="validate"):
            tool.fn(kind="estimate", op="validate", record_id=1)
        with pytest.raises(McpError, match="status"):
            tool.fn(kind="invoice", op="status", record_id=1, status="sent")

        tool.fn(kind="invoice", op="validate", record_id=1)
        assert inst.act.call_args.args[:3] == ("invoices", 1, "validate")

        tool.fn(kind="estimate", op="status", record_id=2, status="sent")
        assert inst.act.call_args.kwargs["payload"] == {"status": "sent"}
        assert inst.act.call_args.kwargs["method"] == "PUT"


def test_moving_an_opportunity_uses_the_dedicated_endpoint():
    """Changer d'étape passe par `step-rank` (PATCH) — un `update` ne la bouge pas."""
    creds, cls = _with_fake_client()
    with creds, cls as client_cls:
        inst = client_cls.return_value
        _tool("sellsy_opportunity").fn(op="move", record_id=7, step=299,
                                       before_sibling=57)

        assert inst.act.call_args.args[:3] == ("opportunities", 7, "step-rank")
        assert inst.act.call_args.kwargs["payload"] == {"step": 299,
                                                        "before_sibling": 57}
        assert inst.act.call_args.kwargs["method"] == "PATCH"


def test_custom_fields_reads_or_writes_depending_on_data():
    creds, cls = _with_fake_client()
    with creds, cls as client_cls:
        inst = client_cls.return_value
        tool = _tool("sellsy_third_party")

        tool.fn(kind="individual", op="custom_fields", record_id=42)
        assert inst.get_custom_fields.called and not inst.set_custom_fields.called

        tool.fn(kind="individual", op="custom_fields", record_id=42,
                data={"custom_fields": [{"id": 12, "value": "x"}]})
        assert inst.set_custom_fields.call_args.args[2] == [{"id": 12, "value": "x"}]


def test_ref_resolves_pipeline_steps_under_their_pipeline():
    creds, cls = _with_fake_client()
    with creds, cls as client_cls:
        inst = client_cls.return_value
        _tool("sellsy_ref").fn(kind="steps", pipeline_id=5)

        assert inst.list_records.call_args.args[0] == "opportunities/pipelines/5/steps"


def test_ref_rejects_an_unknown_kind():
    from oto_mcp.mcp_errors import McpError
    creds, cls = _with_fake_client()
    with creds, cls:
        with pytest.raises(McpError, match="kind"):
            _tool("sellsy_ref").fn(kind="widgets")


def test_missing_required_argument_says_which_one():
    from oto_mcp.mcp_errors import McpError
    creds, cls = _with_fake_client()
    with creds, cls:
        with pytest.raises(McpError, match="record_id"):
            _tool("sellsy_item").fn(op="get")
        with pytest.raises(McpError, match="data"):
            _tool("sellsy_item").fn(op="create")


def test_unknown_op_lists_the_accepted_ones():
    from oto_mcp.mcp_errors import McpError
    creds, cls = _with_fake_client()
    with creds, cls:
        with pytest.raises(McpError, match="Unknown op"):
            _tool("sellsy_contact").fn(op="archive")


def test_dry_run_maps_to_the_api_validation_flag():
    """`verify=true` côté Sellsy : le payload est validé, rien n'est persisté."""
    creds, cls = _with_fake_client()
    with creds, cls as client_cls:
        inst = client_cls.return_value
        _tool("sellsy_third_party").fn(kind="company", op="create",
                                       data={"name": "Acme", "type": "prospect"},
                                       dry_run=True)

        assert inst.create_record.call_args.kwargs["verify"] is True


# --- erreurs amont ------------------------------------------------------------

def test_upstream_403_becomes_an_actionable_tool_error():
    """Un scope manquant se voit à l'APPEL, pas à la connexion : le message doit
    envoyer vers les droits de l'accès API, pas vers un 403 nu."""
    from oto_mcp.mcp_errors import McpError
    from oto.tools.common.errors import UpstreamHTTPError

    creds, cls = _with_fake_client()
    with creds, cls as client_cls:
        client_cls.return_value.list_records.side_effect = UpstreamHTTPError(
            403, {"error": {"message": "Insufficient privileges"}}, service="sellsy")
        with pytest.raises(McpError, match="rights"):
            _tool("sellsy_third_party").fn(kind="company", op="list")


def test_upstream_429_mentions_the_quota_windows():
    from oto_mcp.mcp_errors import McpError
    from oto.tools.common.errors import UpstreamHTTPError

    creds, cls = _with_fake_client()
    with creds, cls as client_cls:
        client_cls.return_value.global_search.side_effect = UpstreamHTTPError(
            429, {"error": "Too Many Requests"}, service="sellsy")
        with pytest.raises(McpError, match="quota"):
            _tool("sellsy_search").fn(q="acme")
