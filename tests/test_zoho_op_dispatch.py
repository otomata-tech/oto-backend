"""Dispatch `op=` des tools `zoho_*` (ADR 0047 §Amendement, appliqué au connecteur
zoho le 2026-08-11 : 9 tools → 3).

Ce que ce fichier verrouille : la SURFACE. Le module `tools/zoho.py` n'avait aucun
test de surface — ses tests voisins (`test_connector_verify*`, `test_zoho_*`) exercent
la sonde et le flux de consentement, jamais le câblage tool → méthode du client. Or une
consolidation par `op=` déplace précisément le risque là : une op mal câblée appelle
silencieusement la mauvaise méthode (`update_record` au lieu de `create_record` = une
écriture sur le mauvais enregistrement), et rien ne casse au boot. D'où, pour chaque
op : la méthode client appelée + ses arguments, le refus explicite d'une op inconnue
(jamais un fallback muet), et le refus nommé d'un argument obligatoire manquant.
"""
import asyncio
from unittest.mock import MagicMock

import pytest
from oto_mcp.mcp_errors import McpError
def _tool(name: str):
    from fastmcp import FastMCP
    from oto_mcp.tools import zoho as Z

    m = FastMCP("t")
    Z.register(m)
    return asyncio.run(m.get_tool(name)).fn


@pytest.fixture
def client(monkeypatch):
    """Faux ZohoClient — le credential (coffre + région) n'est pas le sujet ici.

    `register()` construit son `_client()` en fermeture sur la classe importée dans
    la fonction : on patche donc la classe DANS le module oto-core, avant `register`.

    ⚠️ Depuis oto#25 lot b2, `_client()` résout via `access.resolve_credential`
    (want="byo") et non plus `resolve_credential_fields` — il lui faut l'ENTITÉ
    gagnante (pas seulement les champs) pour marquer une ligne rejetée. D'où un
    substitut de `ResolvedCredential`, même patron que `test_salesforce_op_dispatch.py`."""
    from oto.tools.zoho import client as zoho_client_mod
    from oto_mcp import access

    inst = MagicMock()
    monkeypatch.setattr(zoho_client_mod, "ZohoClient", lambda **kw: inst)

    class _RC:
        entity_type, entity_id, account = "member", "2:sub-x", ""
        fields = {"client_id": "c", "client_secret": "s",
                  "refresh_token": "r", "data_center": "eu"}

    monkeypatch.setattr(access, "resolve_credential", lambda *a, **k: _RC())
    return inst


# --- enregistrements : les 6 verbes CRUD sous un seul tool ---------------------

@pytest.mark.parametrize("op,kwargs,method", [
    ("list", {}, "list_records"),
    ("get", {"record_id": "42"}, "get_record"),
    ("search", {"criteria": "(Email:equals:a@b.com)"}, "search_records"),
    ("create", {"data": {"Last_Name": "Dup"}}, "create_record"),
    ("update", {"record_id": "42", "data": {"Last_Name": "Dup"}}, "update_record"),
    ("delete", {"record_id": "42"}, "delete_record"),
])
def test_record_ops_route_to_the_right_client_method(client, op, kwargs, method):
    _tool("zoho_record")(module="Contacts", op=op, **kwargs)
    getattr(client, method).assert_called_once()


def test_record_list_is_the_default_op(client):
    """`zoho_record(module=…)` sans `op` liste — le cas le plus courant reste gratuit."""
    _tool("zoho_record")(module="Deals")
    client.list_records.assert_called_once()


def test_record_list_forwards_pagination_and_fields(client):
    """Les 3 params de la lecture paginée doivent atteindre le client tels quels :
    `fields` est requis par l'API v7 (une lecture nue → 400), `list_records` le
    complète par module connu quand il est omis."""
    _tool("zoho_record")(module="Contacts", op="list", page=3, per_page=50,
                         fields="Email,Phone")
    assert client.list_records.call_args.args[0] == "Contacts"
    assert client.list_records.call_args.kwargs == {
        "page": 3, "per_page": 50, "fields": "Email,Phone"}


def test_record_search_forwards_criteria_and_pagination(client):
    _tool("zoho_record")(module="Contacts", op="search",
                         criteria="(Email:equals:a@b.com)", page=2, per_page=10)
    assert client.search_records.call_args.args == (
        "Contacts", "(Email:equals:a@b.com)")
    assert client.search_records.call_args.kwargs == {"page": 2, "per_page": 10}


def test_record_update_passes_id_then_data(client):
    """L'ordre (record_id, data) n'est pas symétrique : inverser écrirait l'id comme
    payload sur un enregistrement nommé d'après les données."""
    _tool("zoho_record")(module="Deals", op="update", record_id="42",
                         data={"Stage": "Won"})
    assert client.update_record.call_args.args == ("Deals", "42", {"Stage": "Won"})


def test_record_get_returns_the_client_payload(client):
    client.get_record.return_value = {}
    assert _tool("zoho_record")(module="Deals", op="get", record_id="42") == {}


# --- notes ---------------------------------------------------------------------

@pytest.mark.parametrize("op,kwargs,method", [
    ("list", {}, "list_notes"),
    ("create", {"title": "T", "content": "C"}, "create_note"),
])
def test_note_ops_route_to_the_right_client_method(client, op, kwargs, method):
    _tool("zoho_note")(module="Contacts", record_id="42", op=op, **kwargs)
    getattr(client, method).assert_called_once()


def test_note_list_keeps_the_notes_envelope(client):
    """`list_notes` rend une LISTE ; la surface l'enveloppe dans `{"notes": …}` —
    contrat d'avant la consolidation, un consommateur en dépend."""
    client.list_notes.return_value = [{"Note_Title": "T"}]
    out = _tool("zoho_note")(module="Contacts", record_id="42")
    assert out == {"notes": [{"Note_Title": "T"}]}


def test_note_create_passes_title_then_content(client):
    _tool("zoho_note")(module="Contacts", record_id="42", op="create",
                       title="T", content="C")
    assert client.create_note.call_args.args == ("Contacts", "42", "T", "C")


# --- refus ----------------------------------------------------------------------

def test_unknown_record_op_is_refused_with_the_allowed_list(client):
    """Une op inconnue doit lever en nommant les ops valides — jamais retomber
    silencieusement sur le défaut (l'agent croirait sa demande honorée)."""
    with pytest.raises(McpError, match="op doit être"):
        _tool("zoho_record")(module="Contacts", op="nope")
    client.list_records.assert_not_called()


def test_unknown_note_op_is_refused_with_the_allowed_list(client):
    with pytest.raises(McpError, match="op doit être"):
        _tool("zoho_note")(module="Contacts", record_id="42", op="nope")
    client.list_notes.assert_not_called()


@pytest.mark.parametrize("op,kwargs,missing", [
    ("get", {}, "record_id"),
    ("delete", {}, "record_id"),
    ("update", {"data": {"a": 1}}, "record_id"),
    ("update", {"record_id": "42"}, "data"),
    ("create", {}, "data"),
    ("search", {}, "criteria"),
])
def test_record_missing_required_arg_names_the_op_and_the_arg(
        client, op, kwargs, missing):
    with pytest.raises(McpError, match=missing) as e:
        _tool("zoho_record")(module="Contacts", op=op, **kwargs)
    assert f"op='{op}'" in str(e.value)


@pytest.mark.parametrize("kwargs,missing", [
    ({"content": "C"}, "title"),
    ({"title": "T"}, "content"),
])
def test_note_create_missing_required_arg_names_the_op_and_the_arg(
        client, kwargs, missing):
    with pytest.raises(McpError, match=missing):
        _tool("zoho_note")(module="Contacts", record_id="42", op="create", **kwargs)
    client.create_note.assert_not_called()


def test_module_and_record_id_are_required_by_the_schema(client):
    """`module` (les 6 ops) et `record_id` (les 2 ops de note) sont exigés par la
    SIGNATURE, pas par un check runtime : la validation FastMCP les refuse en amont,
    donc aucun op ne peut tomber sur un module implicite."""
    from fastmcp import FastMCP
    from oto_mcp.tools import zoho as Z

    m = FastMCP("t")
    Z.register(m)
    rec = asyncio.run(m.get_tool("zoho_record")).parameters
    note = asyncio.run(m.get_tool("zoho_note")).parameters
    assert rec["required"] == ["module"]
    assert set(note["required"]) == {"module", "record_id"}


# --- inventaire de surface ------------------------------------------------------

def test_the_connector_exposes_exactly_three_tools(client):
    """9 → 3, zéro capacité perdue : `zoho_modules` (aucun paramètre, scope OAuth
    settings distinct) reste SEUL — ses params ne recouvrent ceux d'aucun voisin."""
    from fastmcp import FastMCP
    from oto_mcp.tools import zoho as Z

    m = FastMCP("t")
    Z.register(m)
    names = {t.name for t in asyncio.run(m.list_tools())}
    assert names == {"zoho_modules", "zoho_record", "zoho_note"}


def test_modules_lists_modules(client):
    client.list_modules.return_value = [{"api_name": "Contacts"}]
    assert _tool("zoho_modules")() == {"modules": [{"api_name": "Contacts"}]}
