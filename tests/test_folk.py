"""folk_create/update/delete/add_to_group — one tool per verb, solo OR bulk
depending on which param is passed (`item`/`id` vs `items`/`ids`). Verrouille :
la validation "exactement un des deux" (ni les deux, ni aucun), le for-loop
server-side en mode bulk (pas de batch natif Folk), le reçu allégé (compte +
erreurs par item, IDs pour create), l'abandon immédiat du lot sur erreur
d'auth/connexion (vs. continuer sur une erreur par item), le cap de taille,
l'entity allow-list élargie (note/reminder acceptés en solo comme en bulk),
et dry_run (diff pour update, preview pour create/delete, dégradation
gracieuse pour l'entité note qui n'a pas de get-par-id côté Folk).
"""
import asyncio
from unittest.mock import MagicMock, patch

import pytest
from mcp.shared.exceptions import McpError


def _register_and_call(tool_name: str, **kwargs):
    from fastmcp import FastMCP
    from oto_mcp.tools import folk as folk_tool

    m = FastMCP("t")
    folk_tool.register(m)
    fn = asyncio.run(m.get_tool(tool_name)).fn
    return fn(**kwargs)


@pytest.fixture(autouse=True)
def _fake_key(monkeypatch):
    monkeypatch.setattr(
        "oto_mcp.access.resolve_api_key", lambda provider, account=None: ("k", False)
    )


@pytest.fixture
def client_cls():
    with patch("oto.tools.folk.client.FolkClient") as cls:
        yield cls


def _instance(client_cls):
    return client_cls.return_value


# --- folk_create -------------------------------------------------------------

def test_create_solo_returns_record_directly(client_cls):
    inst = _instance(client_cls)
    inst.create_person.return_value = {"id": "per_1"}
    r = _register_and_call("folk_create", entity="person", item={"first_name": "Ada"})
    assert r == {"id": "per_1"}
    inst.create_person.assert_called_once_with(first_name="Ada")


def test_create_requires_exactly_one_of_item_or_items(client_cls):
    with pytest.raises(McpError):
        _register_and_call("folk_create", entity="person")  # neither
    with pytest.raises(McpError):
        _register_and_call("folk_create", entity="person",
                           item={"first_name": "A"}, items=[{"first_name": "B"}])  # both


def test_create_bulk_success_returns_ids(client_cls):
    inst = _instance(client_cls)
    inst.create_person.side_effect = [{"id": "per_1"}, {"id": "per_2"}]
    r = _register_and_call(
        "folk_create", entity="person",
        items=[{"first_name": "A"}, {"first_name": "B"}])
    assert r["total"] == 2
    assert r["succeeded"] == 2
    assert r["created"] == [{"index": 0, "id": "per_1"}, {"index": 1, "id": "per_2"}]
    assert r["failed"] == []


def test_create_bulk_partial_failure_continues_batch(client_cls):
    from oto.tools.common.errors import UpstreamHTTPError
    inst = _instance(client_cls)
    inst.create_person.side_effect = [
        {"id": "per_1"},
        UpstreamHTTPError(422, {"message": "invalid email"}, service="folk"),
        {"id": "per_3"},
    ]
    r = _register_and_call(
        "folk_create", entity="person",
        items=[{"first_name": "A"}, {"first_name": "B"}, {"first_name": "C"}])
    assert inst.create_person.call_count == 3  # le lot n'est pas interrompu
    assert r["total"] == 3
    assert r["succeeded"] == 2
    assert [c["index"] for c in r["created"]] == [0, 2]
    assert len(r["failed"]) == 1 and r["failed"][0]["index"] == 1


def test_create_bulk_auth_error_aborts_whole_batch(client_cls):
    from oto.tools.common.errors import UpstreamHTTPError
    inst = _instance(client_cls)
    inst.create_person.side_effect = UpstreamHTTPError(401, {"message": "bad key"}, service="folk")
    with pytest.raises(UpstreamHTTPError):
        _register_and_call(
            "folk_create", entity="person",
            items=[{"first_name": "A"}, {"first_name": "B"}, {"first_name": "C"}])
    assert inst.create_person.call_count == 1  # pas répété N fois


def test_create_bulk_rejects_over_cap(client_cls):
    inst = _instance(client_cls)
    with pytest.raises(McpError):
        _register_and_call(
            "folk_create", entity="person",
            items=[{"first_name": str(i)} for i in range(51)])
    inst.create_person.assert_not_called()


def test_create_unknown_entity_rejected_before_any_call(client_cls):
    inst = _instance(client_cls)
    with pytest.raises(McpError):
        _register_and_call("folk_create", entity="bogus", items=[{}])
    inst.create_person.assert_not_called()


def test_create_deal_requires_group_id(client_cls):
    inst = _instance(client_cls)
    with pytest.raises(McpError):
        _register_and_call("folk_create", entity="deal", items=[{"name": "Deal A"}])
    inst.create_deal.assert_not_called()


def test_create_solo_dry_run_makes_no_network_call(client_cls):
    inst = _instance(client_cls)
    r = _register_and_call("folk_create", entity="person", item={"first_name": "Ada"}, dry_run=True)
    inst.create_person.assert_not_called()
    assert r["dry_run"] is True
    assert r["would_create"]["first_name"] == "Ada"


def test_create_bulk_dry_run_makes_no_network_call(client_cls):
    inst = _instance(client_cls)
    r = _register_and_call(
        "folk_create", entity="person",
        items=[{"first_name": "A"}, {"first_name": "B"}], dry_run=True)
    inst.create_person.assert_not_called()
    assert r["dry_run"] is True
    assert r["total"] == 2
    assert r["would_create"] == [
        {"index": 0, "would_create": {"first_name": "A"}},
        {"index": 1, "would_create": {"first_name": "B"}}]
    assert r["failed"] == []


# --- folk_update ---------------------------------------------------------

def test_update_solo_returns_record_directly(client_cls):
    inst = _instance(client_cls)
    inst.update_person.return_value = {"id": "per_1"}
    r = _register_and_call(
        "folk_update", entity="person", id="per_1", fields={"jobTitle": "CEO"})
    assert r == {"id": "per_1"}


def test_update_requires_exactly_one_of_id_or_items(client_cls):
    with pytest.raises(McpError):
        _register_and_call("folk_update", entity="person")  # neither
    with pytest.raises(McpError):
        _register_and_call(
            "folk_update", entity="person", id="per_1",
            items=[{"id": "per_2", "fields": {}}])  # both


def test_update_solo_now_accepts_note_and_reminder(client_cls):
    inst = _instance(client_cls)
    inst.update_note.return_value = {"id": "nte_1"}
    r = _register_and_call("folk_update", entity="note", id="nte_1", fields={"content": "x"})
    assert r == {"id": "nte_1"}
    inst.update_note.assert_called_once_with("nte_1", content="x")


def test_update_bulk_success(client_cls):
    inst = _instance(client_cls)
    inst.update_person.side_effect = [{"id": "per_1"}, {"id": "per_2"}]
    r = _register_and_call(
        "folk_update", entity="person",
        items=[{"id": "per_1", "fields": {"jobTitle": "CTO"}},
               {"id": "per_2", "fields": {"jobTitle": "CEO"}}])
    assert r == {"total": 2, "succeeded": 2, "failed": []}


def test_update_bulk_partial_failure_reports_id(client_cls):
    from oto.tools.common.errors import UpstreamHTTPError
    inst = _instance(client_cls)
    inst.update_person.side_effect = [
        {"id": "per_1"},
        UpstreamHTTPError(404, {"message": "not found"}, service="folk"),
    ]
    r = _register_and_call(
        "folk_update", entity="person",
        items=[{"id": "per_1", "fields": {"jobTitle": "CTO"}},
               {"id": "per_404", "fields": {"jobTitle": "CEO"}}])
    assert r["succeeded"] == 1
    assert r["failed"] == [{"index": 1, "id": "per_404", "error": str(
        UpstreamHTTPError(404, {"message": "not found"}, service="folk"))}]


def test_update_bulk_missing_id_is_per_item_failure_not_abort(client_cls):
    inst = _instance(client_cls)
    inst.update_person.side_effect = [{"id": "per_2"}]
    r = _register_and_call(
        "folk_update", entity="person",
        items=[{"fields": {"jobTitle": "CTO"}},  # pas d'id
               {"id": "per_2", "fields": {"jobTitle": "CEO"}}])
    assert r["succeeded"] == 1
    assert r["failed"][0]["index"] == 0
    assert inst.update_person.call_count == 1


def test_update_interaction_rejected():
    with pytest.raises(McpError):
        _register_and_call("folk_update", entity="interaction", id="x", fields={})


def test_update_solo_dry_run_shows_diff(client_cls):
    inst = _instance(client_cls)
    inst.get_person.return_value = {"jobTitle": "CTO"}
    r = _register_and_call(
        "folk_update", entity="person", id="per_1",
        fields={"jobTitle": "CEO"}, dry_run=True)
    inst.update_person.assert_not_called()
    assert r == {"dry_run": True, "id": "per_1",
                 "changes": {"jobTitle": {"from": "CTO", "to": "CEO"}}}


def test_update_bulk_dry_run_shows_diff_and_writes_nothing(client_cls):
    inst = _instance(client_cls)
    inst.get_person.return_value = {"id": "per_1", "jobTitle": "CTO"}
    r = _register_and_call(
        "folk_update", entity="person",
        items=[{"id": "per_1", "fields": {"jobTitle": "CEO"}}], dry_run=True)
    inst.update_person.assert_not_called()
    inst.get_person.assert_called_once_with("per_1")
    assert r["dry_run"] is True
    assert r["would_update"] == [
        {"index": 0, "id": "per_1", "changes": {"jobTitle": {"from": "CTO", "to": "CEO"}}}]
    assert r["failed"] == []


def test_update_bulk_dry_run_note_entity_degrades_gracefully(client_cls):
    inst = _instance(client_cls)
    r = _register_and_call(
        "folk_update", entity="note",
        items=[{"id": "nte_1", "fields": {"content": "new text"}}], dry_run=True)
    inst.update_note.assert_not_called()
    assert r["would_update"] == [
        {"index": 0, "id": "nte_1", "fields": {"content": "new text"},
         "current_available": False}]


def test_update_bulk_dry_run_partial_failure_still_continues(client_cls):
    from oto.tools.common.errors import UpstreamHTTPError
    inst = _instance(client_cls)
    inst.get_person.side_effect = [
        UpstreamHTTPError(404, {"message": "not found"}, service="folk"),
        {"id": "per_2", "jobTitle": "CTO"},
    ]
    r = _register_and_call(
        "folk_update", entity="person",
        items=[{"id": "per_404", "fields": {"jobTitle": "X"}},
               {"id": "per_2", "fields": {"jobTitle": "CEO"}}], dry_run=True)
    assert inst.get_person.call_count == 2  # le lot continue après l'échec
    assert len(r["would_update"]) == 1 and r["would_update"][0]["index"] == 1
    assert len(r["failed"]) == 1 and r["failed"][0]["index"] == 0
    inst.update_person.assert_not_called()


# --- folk_delete -----------------------------------------------------------

def test_delete_requires_exactly_one_of_id_or_ids(client_cls):
    with pytest.raises(McpError):
        _register_and_call("folk_delete", entity="person")  # neither
    with pytest.raises(McpError):
        _register_and_call("folk_delete", entity="person", id="per_1", ids=["per_2"])  # both


def test_delete_solo_now_accepts_note_and_reminder(client_cls):
    inst = _instance(client_cls)
    inst.delete_note.return_value = {}
    r = _register_and_call("folk_delete", entity="note", id="nte_1")
    assert r == {}
    inst.delete_note.assert_called_once_with("nte_1")


def test_delete_bulk_success(client_cls):
    inst = _instance(client_cls)
    inst.delete_person.side_effect = [{}, {}]
    r = _register_and_call("folk_delete", entity="person", ids=["per_1", "per_2"])
    assert r == {"total": 2, "succeeded": 2, "failed": []}
    assert inst.delete_person.call_args_list == [(("per_1",),), (("per_2",),)]


def test_delete_interaction_rejected():
    with pytest.raises(McpError):
        _register_and_call("folk_delete", entity="interaction", id="x")


def test_delete_solo_dry_run(client_cls):
    inst = _instance(client_cls)
    inst.get_company.return_value = {"id": "com_1", "name": "Acme"}
    r = _register_and_call("folk_delete", entity="company", id="com_1", dry_run=True)
    inst.delete_company.assert_not_called()
    assert r == {"dry_run": True, "id": "com_1", "would_delete": {"id": "com_1", "name": "Acme"}}


def test_delete_bulk_dry_run_shows_would_delete_and_writes_nothing(client_cls):
    inst = _instance(client_cls)
    inst.get_person.return_value = {"id": "per_1", "firstName": "Ada"}
    r = _register_and_call(
        "folk_delete", entity="person", ids=["per_1"], dry_run=True)
    inst.delete_person.assert_not_called()
    inst.get_person.assert_called_once_with("per_1")
    assert r["dry_run"] is True
    assert r["would_delete"] == [
        {"index": 0, "id": "per_1", "would_delete": {"id": "per_1", "firstName": "Ada"}}]


def test_delete_bulk_dry_run_note_entity_degrades_gracefully(client_cls):
    inst = _instance(client_cls)
    r = _register_and_call(
        "folk_delete", entity="note", ids=["nte_1"], dry_run=True)
    inst.delete_note.assert_not_called()
    assert r["would_delete"] == [
        {"index": 0, "id": "nte_1", "would_delete": None, "current_available": False}]


# --- folk_add_to_group -------------------------------------------------------

def test_add_to_group_requires_exactly_one_of_id_or_ids(client_cls):
    with pytest.raises(McpError):
        _register_and_call("folk_add_to_group", entity="person", group_id="g1")  # neither
    with pytest.raises(McpError):
        _register_and_call("folk_add_to_group", entity="person", group_id="g1",
                           id="per_1", ids=["per_2"])  # both


def test_add_to_group_solo_preserves_existing_groups(client_cls):
    inst = _instance(client_cls)
    inst.get_person.return_value = {"groups": [{"id": "g1"}]}
    inst.update_person.return_value = {"id": "per_1"}
    r = _register_and_call("folk_add_to_group", entity="person", id="per_1", group_id="g2")
    assert r == {"id": "per_1"}
    inst.update_person.assert_called_once_with("per_1", groups=[{"id": "g1"}, {"id": "g2"}])


def test_add_to_group_bulk_preserves_existing_groups(client_cls):
    inst = _instance(client_cls)
    inst.get_person.return_value = {"groups": [{"id": "g1"}]}
    inst.update_person.return_value = {"id": "per_1"}
    r = _register_and_call(
        "folk_add_to_group", entity="person", ids=["per_1"], group_id="g2")
    assert r == {"total": 1, "succeeded": 1, "failed": []}
    inst.update_person.assert_called_once_with("per_1", groups=[{"id": "g1"}, {"id": "g2"}])


def test_add_to_group_already_member_is_noop_success(client_cls):
    inst = _instance(client_cls)
    inst.get_person.return_value = {"groups": [{"id": "g1"}]}
    inst.update_person.return_value = {"id": "per_1"}
    r = _register_and_call(
        "folk_add_to_group", entity="person", ids=["per_1"], group_id="g1")
    assert r["succeeded"] == 1
    inst.update_person.assert_called_once_with("per_1", groups=[{"id": "g1"}])


def test_add_to_group_deal_entity_rejected():
    with pytest.raises(McpError):
        _register_and_call("folk_add_to_group", entity="deal", ids=["x"], group_id="g1")


def test_add_to_group_solo_dry_run_shows_diff(client_cls):
    inst = _instance(client_cls)
    inst.get_person.return_value = {"groups": [{"id": "g1"}]}
    r = _register_and_call(
        "folk_add_to_group", entity="person", id="per_1", group_id="g2", dry_run=True)
    inst.update_person.assert_not_called()
    assert r == {"dry_run": True, "id": "per_1",
                 "changes": {"groups": {"from": [{"id": "g1"}], "to": [{"id": "g1"}, {"id": "g2"}]}}}


def test_add_to_group_bulk_dry_run_shows_diff_and_writes_nothing(client_cls):
    inst = _instance(client_cls)
    inst.get_person.return_value = {"groups": [{"id": "g1"}]}
    r = _register_and_call(
        "folk_add_to_group", entity="person", ids=["per_1"], group_id="g2", dry_run=True)
    inst.update_person.assert_not_called()
    assert r["dry_run"] is True
    assert r["would_add"] == [
        {"index": 0, "id": "per_1",
         "changes": {"groups": {"from": [{"id": "g1"}], "to": [{"id": "g1"}, {"id": "g2"}]}}}]


def test_add_to_group_bulk_dry_run_already_member_shows_noop(client_cls):
    inst = _instance(client_cls)
    inst.get_person.return_value = {"groups": [{"id": "g1"}]}
    r = _register_and_call(
        "folk_add_to_group", entity="person", ids=["per_1"], group_id="g1", dry_run=True)
    changes = r["would_add"][0]["changes"]["groups"]
    assert changes["from"] == changes["to"] == [{"id": "g1"}]
