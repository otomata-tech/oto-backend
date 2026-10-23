"""`hubspot_list` (les segments) et `hubspot_property` (le schéma des champs).

Deux tools ajoutés à un connecteur qui n'avait que `hubspot_object`/`hubspot_owners`.
Ce qui est testé ici est ce qui peut casser SILENCIEUSEMENT :

1. **La traduction `object_type` → `objectTypeId`.** Les listes sont les seuls
   endpoints HubSpot keyés sur un id numérique (`0-1`…) là où tout le reste du
   connecteur parle en `"contacts"`. Une traduction fausse ne lève pas : elle
   crée la liste sur le mauvais type d'objet.
2. **Le garde-fou DYNAMIC.** Une liste dynamique refuse les écritures
   d'appartenance ; sans pré-lecture on envoie un 400 opaque à l'agent, qui
   conclura que la liste est cassée plutôt que du mauvais type.
3. **`dry_run` sur les ops destructrices.** `clear_members` vide une liste
   entière ; si son dry_run écrivait quand même, la garantie ne vaudrait rien.
"""
import asyncio
from unittest.mock import MagicMock

import pytest
from oto_mcp.mcp_errors import McpError


@pytest.fixture
def client(monkeypatch):
    import oto.tools.hubspot.client as hs

    inst = MagicMock()
    # forme réelle de GET /crm/v3/lists/{listId} : la liste est enveloppée
    inst.get_list.return_value = {
        "list": {"listId": "9", "name": "ICP France", "processingType": "MANUAL"}}
    monkeypatch.setattr(hs, "HubSpotClient", lambda *a, **k: inst)
    monkeypatch.setattr("oto_mcp.auth.hubspot.bearer", lambda *a, **k: "k")
    return inst


def _tool(name: str):
    from fastmcp import FastMCP
    from oto_mcp.tools import hubspot as H

    m = FastMCP("t")
    H.register(m)
    return asyncio.run(m.get_tool(name)).fn


def _dynamic(client):
    client.get_list.return_value = {
        "list": {"listId": "9", "name": "Auto", "processingType": "DYNAMIC"}}


# --- surface -------------------------------------------------------------------

def test_the_connector_now_exposes_four_tools(client):
    from fastmcp import FastMCP
    from oto_mcp.tools import hubspot as H

    m = FastMCP("t")
    H.register(m)
    names = {t.name for t in asyncio.run(m._list_tools())}
    assert names == {"hubspot_object", "hubspot_owners", "hubspot_list",
                     "hubspot_property"}


def test_every_tool_ships_a_description(client):
    """Piège maison : une docstring en f-string ne peuple pas `__doc__` et
    FastMCP embarque alors le tool SANS description, sans rien lever."""
    from fastmcp import FastMCP
    from oto_mcp.tools import hubspot as H

    m = FastMCP("t")
    H.register(m)
    for t in asyncio.run(m._list_tools()):
        assert t.description, f"{t.name} n'a pas de description"


# --- traduction object_type → objectTypeId -------------------------------------

@pytest.mark.parametrize("name,type_id", [
    ("contacts", "0-1"), ("companies", "0-2"), ("deals", "0-3"),
    ("tickets", "0-5"), ("Contacts", "0-1"), ("  deals ", "0-3"),
])
def test_object_names_translate_to_the_numeric_id(client, name, type_id):
    _tool("hubspot_list")(op="create", name="L", object_type=name)
    assert client.create_list.call_args.args == ("L", type_id)


def test_a_raw_custom_object_id_passes_through(client):
    """Aucune table ne peut couvrir les objets custom : leur id dépend du portail."""
    _tool("hubspot_list")(op="create", name="L", object_type="2-7")
    assert client.create_list.call_args.args == ("L", "2-7")


def test_an_unknown_object_type_is_refused_and_names_the_alternatives(client):
    with pytest.raises(McpError, match="object_type") as e:
        _tool("hubspot_list")(op="create", name="L", object_type="prospects")
    assert "2-7" in str(e.value)  # montre la forme d'un id custom
    client.create_list.assert_not_called()


# --- dispatch op → méthode client ----------------------------------------------

@pytest.mark.parametrize("op,kwargs,method", [
    ("search", {}, "search_lists"),
    ("get", {"list_id": "9"}, "get_list"),
    ("create", {"name": "L", "object_type": "contacts"}, "create_list"),
    ("delete", {"list_id": "9"}, "delete_list"),
    ("restore", {"list_id": "9"}, "restore_list"),
    ("members", {"list_id": "9"}, "get_list_memberships"),
    ("add_members", {"list_id": "9", "record_ids": ["1"]}, "add_list_memberships"),
    ("remove_members", {"list_id": "9", "record_ids": ["1"]},
     "remove_list_memberships"),
    ("clear_members", {"list_id": "9"}, "delete_all_list_memberships"),
    ("copy_from", {"list_id": "9", "source_list_id": "8"},
     "add_memberships_from_list"),
    ("record_lists", {"object_type": "contacts", "record_id": "1"},
     "get_record_memberships"),
])
def test_each_list_op_routes_to_the_right_client_method(client, op, kwargs, method):
    _tool("hubspot_list")(op=op, **kwargs)
    getattr(client, method).assert_called_once()


def test_search_is_the_default_list_op(client):
    _tool("hubspot_list")()
    client.search_lists.assert_called_once()


def test_get_by_name_needs_the_object_type_and_uses_the_by_name_endpoint(client):
    _tool("hubspot_list")(op="get", name="ICP France", object_type="contacts")
    assert client.get_list_by_name.call_args.args == ("0-1", "ICP France")
    client.get_list.assert_not_called()


def test_get_without_id_nor_name_is_refused(client):
    with pytest.raises(McpError, match="list_id"):
        _tool("hubspot_list")(op="get")


def test_record_lists_translates_the_type_and_keeps_the_record_id(client):
    _tool("hubspot_list")(op="record_lists", object_type="companies",
                          record_id="42")
    assert client.get_record_memberships.call_args.args == ("0-2", "42")


# --- création ------------------------------------------------------------------

def test_create_defaults_to_a_manual_list(client):
    _tool("hubspot_list")(op="create", name="L", object_type="contacts")
    assert client.create_list.call_args.kwargs["processing_type"] == "MANUAL"


def test_a_dynamic_list_without_criteria_is_refused(client):
    """Une liste dynamique sans `filterBranch` naît vide et le reste : HubSpot
    l'accepte, l'agent croit avoir segmenté."""
    with pytest.raises(McpError, match="filter_branch"):
        _tool("hubspot_list")(op="create", name="L", object_type="contacts",
                              processing_type="DYNAMIC")
    client.create_list.assert_not_called()


def test_the_filter_branch_is_passed_through_verbatim(client):
    branch = {"filterBranchType": "OR", "filterBranches": [
        {"filterBranchType": "AND", "filters": [
            {"filterType": "PROPERTY", "property": "hs_lead_status",
             "operation": {"operationType": "ENUMERATION", "operator": "IS_ANY_OF",
                           "values": ["NEW"]}}]}]}
    _tool("hubspot_list")(op="create", name="L", object_type="contacts",
                          processing_type="DYNAMIC", filter_branch=branch)
    assert client.create_list.call_args.kwargs["filter_branch"] == branch


# --- mise à jour ---------------------------------------------------------------

def test_update_without_anything_to_change_is_refused(client):
    with pytest.raises(McpError, match="filter_branch"):
        _tool("hubspot_list")(op="update", list_id="9")


def test_update_can_rename_and_refilter_in_one_call(client):
    _tool("hubspot_list")(op="update", list_id="9", name="Nouveau",
                          filter_branch={"filterBranchType": "AND"})
    client.update_list_name.assert_called_once_with("9", "Nouveau")
    client.update_list_filters.assert_called_once()


def test_update_renames_without_touching_the_filters(client):
    _tool("hubspot_list")(op="update", list_id="9", name="Nouveau")
    client.update_list_filters.assert_not_called()


# --- le garde-fou DYNAMIC ------------------------------------------------------

@pytest.mark.parametrize("op,kwargs,method", [
    ("add_members", {"record_ids": ["1"]}, "add_list_memberships"),
    ("remove_members", {"record_ids": ["1"]}, "remove_list_memberships"),
    ("clear_members", {}, "delete_all_list_memberships"),
    ("copy_from", {"source_list_id": "8"}, "add_memberships_from_list"),
])
def test_membership_writes_are_refused_on_a_dynamic_list(client, op, kwargs, method):
    """HubSpot répond un 400 générique ; l'agent doit lire « change les critères »."""
    _dynamic(client)
    with pytest.raises(McpError, match="DYNAMIC") as e:
        _tool("hubspot_list")(op=op, list_id="9", **kwargs)
    assert "filter_branch" in str(e.value)
    getattr(client, method).assert_not_called()


def test_reading_the_members_of_a_dynamic_list_stays_allowed(client):
    """Seule l'ÉCRITURE est refusée — lire une liste dynamique est légitime."""
    _dynamic(client)
    _tool("hubspot_list")(op="members", list_id="9")
    client.get_list_memberships.assert_called_once()


# --- dry_run -------------------------------------------------------------------

@pytest.mark.parametrize("op,kwargs,method", [
    ("clear_members", {}, "delete_all_list_memberships"),
    ("remove_members", {"record_ids": ["1", "2"]}, "remove_list_memberships"),
    ("delete", {}, "delete_list"),
])
def test_dry_run_reports_without_writing(client, op, kwargs, method):
    out = _tool("hubspot_list")(op=op, list_id="9", dry_run=True, **kwargs)
    assert out["dry_run"] is True and out["would"] == op
    getattr(client, method).assert_not_called()


def test_dry_run_returns_the_real_current_state_not_an_echo(client):
    """Un aperçu qui se contente de répéter l'entrée ne protège de rien : ce qu'on
    veut savoir, c'est QUELLE liste on s'apprête à vider."""
    out = _tool("hubspot_list")(op="clear_members", list_id="9", dry_run=True)
    assert out["current"]["name"] == "ICP France"


def test_dry_run_still_refuses_a_dynamic_list(client):
    """La validation est identique avec ou sans dry_run — seule l'écriture saute."""
    _dynamic(client)
    with pytest.raises(McpError, match="DYNAMIC"):
        _tool("hubspot_list")(op="clear_members", list_id="9", dry_run=True)


# --- appartenances -------------------------------------------------------------

def test_record_ids_are_stringified(client):
    """HubSpot veut des ids en chaînes ; un int passe le JSON mais pas l'API."""
    _tool("hubspot_list")(op="add_members", list_id="9", record_ids=[1, 2])
    assert client.add_list_memberships.call_args.args == ("9", ["1", "2"])


@pytest.mark.parametrize("ids", [None, []])
def test_membership_writes_need_a_non_empty_id_list(client, ids):
    with pytest.raises(McpError, match="record_ids"):
        _tool("hubspot_list")(op="add_members", list_id="9", record_ids=ids)
    client.add_list_memberships.assert_not_called()


def test_adding_and_removing_together_uses_a_single_revision(client):
    _tool("hubspot_list")(op="add_members", list_id="9", record_ids=["1"],
                          remove_record_ids=["2"])
    client.add_and_remove_list_memberships.assert_called_once_with(
        "9", record_ids_to_add=["1"], record_ids_to_remove=["2"])
    client.add_list_memberships.assert_not_called()


def test_copy_from_keeps_target_then_source_order(client):
    """Inversés, on remplirait la liste source avec la cible — plausible et faux."""
    _tool("hubspot_list")(op="copy_from", list_id="9", source_list_id="8")
    assert client.add_memberships_from_list.call_args.args == ("9", "8")


def test_members_forwards_pagination(client):
    _tool("hubspot_list")(op="members", list_id="9", limit=50, after="cur")
    args, kw = client.get_list_memberships.call_args
    assert args == ("9",) and kw == {"limit": 50, "after": "cur"}


# --- refus : op inconnue -------------------------------------------------------

def test_unknown_list_op_is_refused_and_names_every_op(client):
    with pytest.raises(McpError) as e:
        _tool("hubspot_list")(op="segment", list_id="9")
    msg = str(e.value)
    for op in ("search", "get", "create", "update", "delete", "restore",
               "members", "add_members", "remove_members", "clear_members",
               "copy_from", "record_lists"):
        assert f"'{op}'" in msg


# --- hubspot_property ----------------------------------------------------------

@pytest.mark.parametrize("op,kwargs,method", [
    ("list", {"object_type": "contacts"}, "list_properties"),
    ("get", {"object_type": "contacts", "property_name": "email"}, "get_property"),
    ("create", {"object_type": "deals", "definition": {"name": "x"}},
     "create_property"),
    ("update", {"object_type": "deals", "property_name": "x",
                "definition": {"label": "X"}}, "update_property"),
    ("delete", {"object_type": "deals", "property_name": "x"}, "delete_property"),
    ("groups", {"object_type": "contacts"}, "list_property_groups"),
])
def test_each_property_op_routes_to_the_right_client_method(
        client, op, kwargs, method):
    _tool("hubspot_property")(op=op, **kwargs)
    getattr(client, method).assert_called_once()


def test_property_list_is_the_default_op(client):
    _tool("hubspot_property")(object_type="contacts")
    client.list_properties.assert_called_once()


@pytest.mark.parametrize("op,other", [
    ("list", {}), ("get", {"property_name": "email"}),
    ("create", {"definition": {"name": "x"}}),
    ("update", {"property_name": "x", "definition": {"label": "X"}}),
    ("delete", {"property_name": "x"}), ("groups", {}),
])
def test_property_ops_all_require_the_object_type(client, op, other):
    with pytest.raises(McpError, match="object_type"):
        _tool("hubspot_property")(op=op, **other)


def test_a_property_definition_must_be_a_dict(client):
    """Une liste de noms ici créerait une propriété vide côté HubSpot."""
    with pytest.raises(McpError, match="definition"):
        _tool("hubspot_property")(op="create", object_type="deals",
                                  definition=["name", "label"])
    client.create_property.assert_not_called()


def test_property_delete_dry_run_shows_what_would_be_archived(client):
    client.get_property.return_value = {"name": "amount", "type": "number"}
    out = _tool("hubspot_property")(op="delete", object_type="deals",
                                    property_name="amount", dry_run=True)
    assert out["dry_run"] is True and out["current"]["name"] == "amount"
    client.delete_property.assert_not_called()


def test_archived_flag_reaches_the_client(client):
    _tool("hubspot_property")(op="list", object_type="contacts", archived=True)
    assert client.list_properties.call_args.kwargs == {"archived": True}


def test_unknown_property_op_is_refused(client):
    with pytest.raises(McpError, match="op must be"):
        _tool("hubspot_property")(op="schema", object_type="contacts")
