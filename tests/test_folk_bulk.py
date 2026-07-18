"""Bulk actions on the `folk` connector — verrouille : le for-loop server-side
(pas de batch natif Folk), le reçu allégé (compte + erreurs par item, IDs pour
create), l'abandon immédiat du lot sur erreur d'auth/connexion (vs. continuer
sur une erreur par item), le cap de taille, et la non-régression des outils
singuliers refactorés (`folk_update`/`folk_delete`/`folk_create_*`) pour
partager la même logique de dispatch que leurs équivalents bulk.
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


# --- folk_bulk_create -----------------------------------------------------

def test_bulk_create_success_returns_ids(client_cls):
    inst = _instance(client_cls)
    inst.create_person.side_effect = [{"id": "per_1"}, {"id": "per_2"}]
    r = _register_and_call(
        "folk_bulk_create", entity="person",
        items=[{"first_name": "A"}, {"first_name": "B"}])
    assert r["total"] == 2
    assert r["succeeded"] == 2
    assert r["created"] == [{"index": 0, "id": "per_1"}, {"index": 1, "id": "per_2"}]
    assert r["failed"] == []


def test_bulk_create_partial_failure_continues_batch(client_cls):
    from oto.tools.common.errors import UpstreamHTTPError
    inst = _instance(client_cls)
    inst.create_person.side_effect = [
        {"id": "per_1"},
        UpstreamHTTPError(422, {"message": "invalid email"}, service="folk"),
        {"id": "per_3"},
    ]
    r = _register_and_call(
        "folk_bulk_create", entity="person",
        items=[{"first_name": "A"}, {"first_name": "B"}, {"first_name": "C"}])
    assert inst.create_person.call_count == 3  # le lot n'est pas interrompu
    assert r["total"] == 3
    assert r["succeeded"] == 2
    assert [c["index"] for c in r["created"]] == [0, 2]
    assert len(r["failed"]) == 1 and r["failed"][0]["index"] == 1


def test_bulk_create_auth_error_aborts_whole_batch(client_cls):
    from oto.tools.common.errors import UpstreamHTTPError
    inst = _instance(client_cls)
    inst.create_person.side_effect = UpstreamHTTPError(401, {"message": "bad key"}, service="folk")
    with pytest.raises(UpstreamHTTPError):
        _register_and_call(
            "folk_bulk_create", entity="person",
            items=[{"first_name": "A"}, {"first_name": "B"}, {"first_name": "C"}])
    assert inst.create_person.call_count == 1  # pas répété N fois


def test_bulk_create_rejects_over_cap(client_cls):
    inst = _instance(client_cls)
    with pytest.raises(McpError):
        _register_and_call(
            "folk_bulk_create", entity="person",
            items=[{"first_name": str(i)} for i in range(51)])
    inst.create_person.assert_not_called()


def test_bulk_create_unknown_entity_rejected_before_any_call(client_cls):
    inst = _instance(client_cls)
    with pytest.raises(McpError):
        _register_and_call("folk_bulk_create", entity="bogus", items=[{}])
    inst.create_person.assert_not_called()


def test_bulk_create_deal_requires_group_id(client_cls):
    inst = _instance(client_cls)
    with pytest.raises(McpError):
        _register_and_call("folk_bulk_create", entity="deal", items=[{"name": "Deal A"}])
    inst.create_deal.assert_not_called()


# --- folk_bulk_update -------------------------------------------------------

def test_bulk_update_success(client_cls):
    inst = _instance(client_cls)
    inst.update_person.side_effect = [{"id": "per_1"}, {"id": "per_2"}]
    r = _register_and_call(
        "folk_bulk_update", entity="person",
        items=[{"id": "per_1", "fields": {"jobTitle": "CTO"}},
               {"id": "per_2", "fields": {"jobTitle": "CEO"}}])
    assert r == {"total": 2, "succeeded": 2, "failed": []}


def test_bulk_update_partial_failure_reports_id(client_cls):
    from oto.tools.common.errors import UpstreamHTTPError
    inst = _instance(client_cls)
    inst.update_person.side_effect = [
        {"id": "per_1"},
        UpstreamHTTPError(404, {"message": "not found"}, service="folk"),
    ]
    r = _register_and_call(
        "folk_bulk_update", entity="person",
        items=[{"id": "per_1", "fields": {"jobTitle": "CTO"}},
               {"id": "per_404", "fields": {"jobTitle": "CEO"}}])
    assert r["succeeded"] == 1
    assert r["failed"] == [{"index": 1, "id": "per_404", "error": str(
        UpstreamHTTPError(404, {"message": "not found"}, service="folk"))}]


def test_bulk_update_missing_id_is_per_item_failure_not_abort(client_cls):
    inst = _instance(client_cls)
    inst.update_person.side_effect = [{"id": "per_2"}]
    r = _register_and_call(
        "folk_bulk_update", entity="person",
        items=[{"fields": {"jobTitle": "CTO"}},  # pas d'id
               {"id": "per_2", "fields": {"jobTitle": "CEO"}}])
    assert r["succeeded"] == 1
    assert r["failed"][0]["index"] == 0
    assert inst.update_person.call_count == 1


def test_bulk_update_interaction_rejected():
    with pytest.raises(McpError):
        _register_and_call("folk_bulk_update", entity="interaction", items=[{"id": "x"}])


# --- folk_bulk_delete --------------------------------------------------------

def test_bulk_delete_success(client_cls):
    inst = _instance(client_cls)
    inst.delete_person.side_effect = [{}, {}]
    r = _register_and_call("folk_bulk_delete", entity="person", ids=["per_1", "per_2"])
    assert r == {"total": 2, "succeeded": 2, "failed": []}
    assert inst.delete_person.call_args_list == [(("per_1",),), (("per_2",),)]


def test_bulk_delete_interaction_rejected():
    with pytest.raises(McpError):
        _register_and_call("folk_bulk_delete", entity="interaction", ids=["x"])


# --- folk_bulk_add_to_group --------------------------------------------------

def test_bulk_add_to_group_preserves_existing_groups(client_cls):
    inst = _instance(client_cls)
    inst.get_person.return_value = {"groups": [{"id": "g1"}]}
    inst.update_person.return_value = {"id": "per_1"}
    r = _register_and_call(
        "folk_bulk_add_to_group", entity="person", ids=["per_1"], group_id="g2")
    assert r == {"total": 1, "succeeded": 1, "failed": []}
    inst.update_person.assert_called_once_with("per_1", groups=[{"id": "g1"}, {"id": "g2"}])


def test_bulk_add_to_group_already_member_is_noop_success(client_cls):
    inst = _instance(client_cls)
    inst.get_person.return_value = {"groups": [{"id": "g1"}]}
    inst.update_person.return_value = {"id": "per_1"}
    r = _register_and_call(
        "folk_bulk_add_to_group", entity="person", ids=["per_1"], group_id="g1")
    assert r["succeeded"] == 1
    inst.update_person.assert_called_once_with("per_1", groups=[{"id": "g1"}])


def test_bulk_add_to_group_deal_entity_rejected():
    with pytest.raises(McpError):
        _register_and_call("folk_bulk_add_to_group", entity="deal", ids=["x"], group_id="g1")


# --- non-régression des outils singuliers refactorés -------------------------

def test_singular_create_person_unchanged(client_cls):
    inst = _instance(client_cls)
    inst.create_person.return_value = {"id": "per_1"}
    r = _register_and_call("folk_create_person", first_name="Ada")
    assert r == {"id": "per_1"}
    inst.create_person.assert_called_once_with(
        first_name="Ada", last_name=None, emails=None, phones=None, job_title=None,
        company_name=None, company_id=None, group_ids=None, urls=None, description=None)


def test_singular_update_still_rejects_unknown_entity(client_cls):
    with pytest.raises(McpError):
        _register_and_call("folk_update", entity="note", id="nte_1", fields={"content": "x"})


def test_singular_delete_still_rejects_unknown_entity(client_cls):
    with pytest.raises(McpError):
        _register_and_call("folk_delete", entity="note", id="nte_1")
