"""Tools `affinity_*` et sonde du connecteur Affinity.

Ce que ce fichier verrouille — les tripwires génériques couvrent déjà registre,
éditeur, logo, prose servie et jointure au client :
- la SURFACE (5 tools, l'op par défaut de chacun est une LECTURE) ;
- les valeurs de champ aplaties en `{nom: valeur}` (dropdown → son texte) et
  `full=True` qui rend le brut ;
- `set_fields` : colonne par NOM, option de dropdown par TEXTE (refus qui liste
  les options), colonnes calculées refusées, `item`/`items` exclusifs, plafond
  du lot, `dry_run` = diff sans écriture, reçu de lot, arrêt sur 401/403/429 ;
- les gardes d'écriture : société au domaine connu rendue au lieu d'un doublon,
  retrait refusé sur une liste d'opportunités, ajout ignoré si déjà présent ;
- la traduction des refus amont (403 = permission nommée) ;
- la sonde : `whoami`, 401 → `NonAutorise`, clé en lecture seule nommée.
"""
import asyncio
from unittest.mock import MagicMock

import pytest

from oto_mcp.connectors import verify as cv
from oto_mcp.mcp_errors import McpError
from oto.tools.common.errors import UpstreamHTTPError

_LIST_FIELDS = {"data": [
    {"id": "field-1", "name": "Status", "type": "list", "valueType": "dropdown"},
    {"id": "field-2", "name": "Amount", "type": "list", "valueType": "number"},
    {"id": "field-3", "name": "Owners", "type": "list", "valueType": "person-multi"},
    {"id": "affinity-data-location", "name": "Location", "type": "enriched",
     "valueType": "location"},
], "pagination": {"nextUrl": None}}
_OPTIONS = {"data": [{"id": 11, "text": "New"}, {"id": 12, "text": "Due diligence"}],
            "pagination": {"nextUrl": None}}


@pytest.fixture
def client(monkeypatch):
    inst, built = MagicMock(), []

    def fabrique(**kw):
        built.append(kw)
        return inst

    monkeypatch.setattr("oto.tools.affinity.AffinityClient", fabrique)
    monkeypatch.setattr("oto_mcp.access.resolve_api_key",
                        lambda provider, account=None, units=1: ("cle-de-test", False))
    inst.built = built
    inst.list_fields.return_value = _LIST_FIELDS
    inst.dropdown_options.return_value = _OPTIONS
    inst.last_rate_limit = {"org_remaining": 98000}
    return inst


def _mcp():
    from fastmcp import FastMCP
    from oto_mcp.tools import affinity as A

    m = FastMCP("t")
    A.register(m)
    return m


def _tool(name: str):
    return asyncio.run(_mcp().get_tool(name)).fn


def _http(status, body="x"):
    return UpstreamHTTPError(status, body, service="affinity")


# --- surface ---------------------------------------------------------------------

TOOLS = {"affinity_entity": "search", "affinity_list": "list",
         "affinity_list_entry": "query", "affinity_note": "list",
         "affinity_interactions": None}


def test_surface_cinq_tools_documentes():
    m = _mcp()
    names = {t.name for t in asyncio.run(m.list_tools())} if hasattr(m, "list_tools") else set(
        asyncio.run(m.get_tools()).keys())
    assert {n for n in names if n.startswith("affinity_")} == set(TOOLS)
    for name in TOOLS:
        tool = asyncio.run(m.get_tool(name))
        assert tool.description and len(tool.description) > 80, name


@pytest.mark.parametrize("name,default", [(n, d) for n, d in TOOLS.items() if d])
def test_l_op_par_defaut_est_une_lecture(name, default):
    import inspect
    sig = inspect.signature(_tool(name))
    assert sig.parameters["op"].default == default


def test_la_cle_resolue_est_passee_au_client(client):
    _tool("affinity_list")()
    assert client.built[-1] == {"api_key": "cle-de-test"}


# --- reads -----------------------------------------------------------------------

def test_get_aplatit_les_champs_et_full_rend_le_brut(client):
    raw = {"id": 7, "name": "Acme", "fields": [
        {"id": "field-1", "name": "Status", "type": "global",
         "value": {"type": "dropdown", "data": {"dropdownOptionId": 11, "text": "New"}}},
        {"id": "affinity-data-location", "name": "Location", "type": "enriched",
         "value": {"type": "location", "data": {"city": "Paris", "state": None,
                                                "country": "France"}}},
        {"id": "field-9", "name": "Team", "type": "global",
         "value": {"type": "person-multi", "data": [{"id": 1, "firstName": "Ada",
                                                     "lastName": "L"}]}},
    ]}
    client.get_company.return_value = raw
    out = _tool("affinity_entity")(op="get", id=7)
    assert out["fields"] == {"Status": "New", "Location": "Paris, France", "Team": ["Ada L"]}
    kw = client.get_company.call_args.kwargs
    assert kw["field_types"] == ["global", "enriched", "relationship-intelligence"]
    assert _tool("affinity_entity")(op="get", id=7, full=True) is raw


def test_query_rend_next_cursor(client):
    client.list_entries.return_value = {"data": [], "pagination": {
        "nextUrl": "https://api.affinity.co/v2/lists/1/list-entries?cursor=abc"}}
    out = _tool("affinity_list_entry")(list_id=1)
    assert out == {"data": [], "next_cursor": "abc"}
    assert client.list_entries.call_args.kwargs["field_types"] == ["list"]


def test_query_vue_sauvegardee(client):
    _tool("affinity_list_entry")(list_id=1, view_id=5, limit=10)
    client.saved_view_entries.assert_called_once_with(1, 5, cursor=None, limit=10)


def test_recherche_d_opportunite_renvoyee_vers_la_liste(client):
    with pytest.raises(McpError, match="through their list"):
        _tool("affinity_entity")(term="series a", kind="opportunity")


def test_argument_d_un_autre_op_refuse(client):
    with pytest.raises(McpError, match="does not use `item`"):
        _tool("affinity_entity")(term="acme", item={"name": "x"})


def test_interactions_par_entite_avec_fenetre_par_defaut(client):
    _tool("affinity_interactions")(kind="company", id=8, type="meeting")
    args, kw = client.list_interactions.call_args
    assert args == ("meeting",) and kw["organization_id"] == 8
    assert kw["start_time"] < kw["end_time"]


# --- set_fields --------------------------------------------------------------------

def test_set_fields_par_nom_et_texte_d_option(client):
    out = _tool("affinity_list_entry")(op="set_fields", list_id=1, item={
        "entry_id": 3, "fields": {"status": "due diligence", "Amount": "2000000",
                                  "Owners": [4, 5]}})
    args = client.update_list_entry_fields.call_args.args
    assert args[:2] == (1, 3)
    assert args[2] == [
        {"id": "field-1", "value": {"type": "dropdown", "data": {"dropdownOptionId": 12}}},
        {"id": "field-2", "value": {"type": "number", "data": 2000000.0}},
        {"id": "field-3", "value": {"type": "person-multi", "data": [{"id": 4}, {"id": 5}]}},
    ]
    assert out == {"entry_id": 3, "fields_updated": ["Status", "Amount", "Owners"]}


def test_option_inconnue_refusee_avec_la_liste(client):
    with pytest.raises(McpError, match="'New', 'Due diligence'"):
        _tool("affinity_list_entry")(op="set_fields", list_id=1,
                                     item={"entry_id": 3, "fields": {"Status": "Won"}})
    client.update_list_entry_fields.assert_not_called()


def test_null_vide_la_colonne(client):
    _tool("affinity_list_entry")(op="set_fields", list_id=1,
                                 item={"entry_id": 3, "fields": {"Status": None}})
    assert client.update_list_entry_fields.call_args.args[2] == [
        {"id": "field-1", "value": {"type": "dropdown", "data": None}}]


def test_colonne_calculee_refusee(client):
    with pytest.raises(McpError, match="computes it"):
        _tool("affinity_list_entry")(op="set_fields", list_id=1, item={
            "entry_id": 3, "fields": {"Location": {"city": "Paris"}}})


def test_colonne_inconnue_nomme_les_colonnes(client):
    with pytest.raises(McpError, match="Status \\(field-1\\)"):
        _tool("affinity_list_entry")(op="set_fields", list_id=1,
                                     item={"entry_id": 3, "fields": {"Stage": "x"}})


def test_item_et_items_exclusifs_et_lot_plafonne(client):
    fn = _tool("affinity_list_entry")
    with pytest.raises(McpError, match="exactly one"):
        fn(op="set_fields", list_id=1)
    with pytest.raises(McpError, match="exactly one"):
        fn(op="set_fields", list_id=1, item={"entry_id": 1, "fields": {"Amount": 1}},
           items=[{"entry_id": 2, "fields": {"Amount": 1}}])
    with pytest.raises(McpError, match="at most 25"):
        fn(op="set_fields", list_id=1,
           items=[{"entry_id": i, "fields": {"Amount": 1}} for i in range(26)])


def test_dry_run_rend_le_diff_sans_ecrire(client):
    client.get_list_entry.return_value = {"id": 3, "entity": {"id": 8, "fields": [
        {"id": "field-1", "name": "Status",
         "value": {"type": "dropdown", "data": {"dropdownOptionId": 11, "text": "New"}}}]}}
    out = _tool("affinity_list_entry")(op="set_fields", list_id=1, dry_run=True, item={
        "entry_id": 3, "fields": {"Status": "Due diligence"}})
    assert out == {"dry_run": True, "entry_id": 3,
                   "changes": {"Status": {"from": "New", "to": "Due diligence"}}}
    client.update_list_entry_fields.assert_not_called()


def test_lot_recu_et_erreur_par_ligne(client):
    client.update_list_entry_fields.side_effect = [None, _http(400, "bad"), None]
    out = _tool("affinity_list_entry")(op="set_fields", list_id=1, items=[
        {"entry_id": i, "fields": {"Amount": i}} for i in (1, 2, 3)])
    assert out["total"] == 3 and out["succeeded"] == 2
    assert [f["entry_id"] for f in out["failed"]] == [2]
    assert out["rate_limit"] == {"org_remaining": 98000}


@pytest.mark.parametrize("status", [401, 403, 429])
def test_lot_arrete_sur_erreur_fatale(client, status):
    client.update_list_entry_fields.side_effect = [None, _http(status)]
    with pytest.raises(McpError, match="Batch stopped at entry 1 after 1 written"):
        _tool("affinity_list_entry")(op="set_fields", list_id=1, items=[
            {"entry_id": i, "fields": {"Amount": i}} for i in (1, 2, 3)])
    assert client.update_list_entry_fields.call_count == 2


# --- write guards -----------------------------------------------------------------

def test_societe_au_domaine_connu_rendue_au_lieu_d_un_doublon(client):
    client.search_organizations.return_value = {"organizations": [
        {"id": 5, "name": "Acme", "domain": "acme.co", "domains": ["acme.co"]}]}
    out = _tool("affinity_entity")(op="create", item={"name": "Acme",
                                                     "domain": "https://www.Acme.co/"})
    assert out["existing"] is True and out["company"]["id"] == 5
    client.create_organization.assert_not_called()


def test_domaine_proche_n_est_pas_un_doublon(client):
    client.search_organizations.return_value = {"organizations": [
        {"id": 5, "name": "Acme Labs", "domain": "acmelabs.co", "domains": ["acmelabs.co"]}]}
    _tool("affinity_entity")(op="create", item={"name": "Acme", "domain": "acme.co"})
    client.create_organization.assert_called_once_with("Acme", "acme.co", None)


def test_retrait_refuse_sur_une_liste_d_opportunites(client):
    client.get_list.return_value = {"id": 1, "type": "opportunity"}
    with pytest.raises(McpError, match="delete"):
        _tool("affinity_list_entry")(op="remove", list_id=1, entry_id=3)
    client.remove_list_entry.assert_not_called()


def test_ajout_ignore_si_deja_sur_la_liste(client):
    client.get_list.return_value = {"id": 1, "type": "company"}
    client.entity_list_entries.return_value = {"data": [{"id": 77, "listId": 1}],
                                               "pagination": {"nextUrl": None}}
    out = _tool("affinity_list_entry")(op="add", list_id=1, entity_id=8)
    assert out == {"already_on_list": True, "entry_id": 77}
    client.add_list_entry.assert_not_called()


def test_ajout_sur_liste_d_opportunites_renvoye_vers_create(client):
    client.get_list.return_value = {"id": 1, "type": "opportunity"}
    with pytest.raises(McpError, match="kind='opportunity'"):
        _tool("affinity_list_entry")(op="add", list_id=1, entity_id=8)


def test_update_entite_champs_globaux_et_attributs(client):
    client.global_fields.return_value = {"data": [
        {"id": "field-7", "name": "Sector", "type": "global", "valueType": "dropdown"}],
        "pagination": {"nextUrl": None}}
    client.dropdown_options.return_value = {"data": [{"id": 70, "text": "Fintech"}],
                                            "pagination": {"nextUrl": None}}
    out = _tool("affinity_entity")(op="update", kind="company", id=8, item={
        "name": "Acme Inc", "fields": {"Sector": "fintech"}})
    client.update_organization.assert_called_once_with(8, name="Acme Inc")
    client.update_entity_fields.assert_called_once_with("company", 8, [
        {"id": "field-7", "value": {"type": "dropdown", "data": {"dropdownOptionId": 70}}}])
    assert out["fields_updated"] == ["Sector"]


def test_note_delete_dry_run(client):
    client.get_note.return_value = {"id": 4, "content": {"html": "<p>x</p>"}}
    out = _tool("affinity_note")(op="delete", note_id=4, dry_run=True)
    assert out["dry_run"] is True
    client.delete_note.assert_not_called()


def test_notes_bornees_par_date(client):
    _tool("affinity_note")(kind="person", entity_id=7, since="2026-09-01T00:00:00Z")
    assert client.list_notes.call_args.kwargs["filter"] == "createdAt>=2026-09-01T00:00:00Z"


# --- upstream errors ---------------------------------------------------------------

def test_403_nomme_les_permissions(client):
    client.list_entries.side_effect = _http(403, {"errors": [{"code": "permission"}]})
    with pytest.raises(McpError, match="Export data from Lists"):
        _tool("affinity_list_entry")(list_id=1)


# --- probe ---------------------------------------------------------------------

def test_sonde_401_non_autorise(client):
    from oto_mcp.tools import affinity as A
    client.whoami.side_effect = _http(401, "Invalid or missing API key")
    with pytest.raises(cv.NonAutorise):
        A._verify({"key": "k"})


def test_sonde_cle_en_lecture_seule(client):
    from oto_mcp.tools import affinity as A
    client.whoami.return_value = {"grant": {"type": "access-token", "scopes": ["api.read"]}}
    with pytest.raises(cv.NonAutorise, match="read-only"):
        A._verify({"key": "k"})


def test_sonde_cle_api_ok_sur_les_deux_generations(client):
    from oto_mcp.tools import affinity as A
    client.whoami.return_value = {"grant": {"type": "api-key", "scopes": ["api"]}}
    A._verify({"key": "k"})
    client.whoami_v1.assert_called_once_with()


def test_sonde_v1_refusee_non_autorise(client):
    from oto_mcp.tools import affinity as A
    client.whoami.return_value = {"grant": {"type": "api-key", "scopes": ["api"]}}
    client.whoami_v1.side_effect = _http(401, "Unauthorized API Key.")
    with pytest.raises(cv.NonAutorise):
        A._verify({"key": "k"})


def test_dry_run_entite_lit_les_tableaux_actuels_en_v1(client):
    client.get_company.return_value = {"id": 8, "name": "Acme", "fields": []}
    client.get_entity_v1.return_value = {"id": 8, "person_ids": [1, 2]}
    out = _tool("affinity_entity")(op="update", kind="company", id=8, dry_run=True,
                                   item={"name": "Acme Inc", "person_ids": [3]})
    assert out["changes"] == {
        "name": {"from": "Acme", "to": "Acme Inc"},
        "person_ids": {"from": [1, 2], "to": [3], "replaces": True}}
    client.update_organization.assert_not_called()


def test_pagination_qui_boucle_s_arrete(client):
    page = {"data": _LIST_FIELDS["data"], "pagination": {
        "nextUrl": "https://api.affinity.co/v2/lists/1/fields?cursor=same"}}
    client.list_fields.return_value = page
    _tool("affinity_list_entry")(op="set_fields", list_id=1,
                                 item={"entry_id": 3, "fields": {"Amount": 1}})
    assert client.list_fields.call_count == 2


def test_liste_de_notes_rend_la_taille_du_corps(client):
    client.list_notes.return_value = {"data": [{"id": 1, "content": {"html": "<p>abc</p>"}}],
                                      "pagination": {"nextUrl": None}}
    out = _tool("affinity_note")()
    assert out["data"] == [{"id": 1, "content_length": 10}]
    assert _tool("affinity_note")(full=True)["data"][0]["content"] == {"html": "<p>abc</p>"}


def test_interactions_gardent_l_email_principal(client):
    client.list_interactions.return_value = {"emails": [{"id": 1, "from": {
        "id": 2, "primary_email": "a@x.io", "emails": ["a@x.io", "b@x.io"]}}]}
    out = _tool("affinity_interactions")(kind="person", id=2)
    assert out["emails"][0]["from"] == {"id": 2, "primary_email": "a@x.io"}


def test_colonnes_sans_filterability_par_defaut(client):
    client.list_fields.return_value = {"data": [{"id": "f", "filterability": {"x": 1}}],
                                       "pagination": {"nextUrl": None}}
    assert _tool("affinity_list")(op="fields", list_id=1)["data"] == [{"id": "f"}]
