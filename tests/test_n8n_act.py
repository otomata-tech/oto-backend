"""Tool `n8n_act`: writing a workflow of an n8n instance.

What this file locks — the generic tripwires already cover the effects table, the frozen
surface and the served prose:
- the routing of each op to its client method, with the credential's two fields;
- what is returned: a summary of the workflow written, never its whole graph, and the
  read-only keys left out of the body named in `ignored_fields`;
- intent refusals (an argument of another op, a missing required argument), before any
  call to n8n.
"""
import asyncio
from unittest.mock import MagicMock

import pytest

from oto_mcp.mcp_errors import McpError

_CREDS = {"api_key": "cle-de-test", "base_url": "https://n8n.example.org"}

_LU = {"id": "w1", "name": "Relance", "active": True, "tags": [{"id": "t1"}],
       "createdAt": "2026-11-01T00:00:00.000Z", "nodes": [{"name": "A"}, {"name": "B"}],
       "connections": {}, "settings": {}}


@pytest.fixture
def client(monkeypatch):
    inst, built = MagicMock(), []

    def fabrique(**kw):
        built.append(kw)
        return inst

    monkeypatch.setattr("oto.tools.n8n.N8nClient", fabrique)
    monkeypatch.setattr("oto_mcp.access.resolve_credential_fields",
                        lambda provider, account=None: dict(_CREDS))
    monkeypatch.setattr("oto_mcp.egress.check_url", lambda url, connector=None: None)
    inst.built = built
    return inst


def _tool(name: str):
    from fastmcp import FastMCP
    from oto_mcp.tools import n8n as N

    m = FastMCP("t")
    N.register(m)
    return asyncio.run(m.get_tool(name))


def test_l_outil_qui_ecrit_est_un_acte():
    assert _tool("n8n_act").annotations.readOnlyHint is False


def test_creer_rend_un_resume_et_nomme_les_cles_ecartees(client):
    client.create_workflow.return_value = dict(_LU, active=False)
    out = _tool("n8n_act").fn(op="create_workflow", workflow=_LU)
    client.create_workflow.assert_called_once_with(_LU)
    assert client.built == [_CREDS]
    assert out == {"id": "w1", "name": "Relance", "active": False,
                   "createdAt": "2026-11-01T00:00:00.000Z", "node_count": 2,
                   "ignored_fields": ["active", "createdAt", "id", "tags"]}


def test_creer_sans_le_graphe_est_refuse_avant_n8n(client):
    with pytest.raises(McpError, match="nodes, connections"):
        _tool("n8n_act").fn(op="create_workflow", workflow={"name": "x"})
    client.create_workflow.assert_not_called()


def test_modifier_passe_la_publication_et_resume(client):
    client.update_workflow.return_value = _LU
    out = _tool("n8n_act").fn(op="update_workflow", workflow_id="w1",
                              workflow={"name": "Relance"}, publish_if_active=False)
    client.update_workflow.assert_called_once_with(
        "w1", {"name": "Relance"}, publish_if_active=False)
    assert "nodes" not in out and out["node_count"] == 2 and "ignored_fields" not in out


def test_modifier_sans_cle_ecrivable_est_refuse(client):
    with pytest.raises(McpError, match="ignored: active, id"):
        _tool("n8n_act").fn(op="update_workflow", workflow_id="w1",
                            workflow={"id": "w1", "active": False})
    client.update_workflow.assert_not_called()


def test_supprimer_et_poser_les_tags(client):
    client.delete_workflow.return_value = _LU
    assert _tool("n8n_act").fn(op="delete_workflow", workflow_id="w1")["id"] == "w1"
    client.delete_workflow.assert_called_once_with("w1")
    client.set_workflow_tags.return_value = []
    assert _tool("n8n_act").fn(op="set_workflow_tags", workflow_id="w1", tag_ids=[]) == []
    client.set_workflow_tags.assert_called_once_with("w1", [])


@pytest.mark.parametrize("kw, attendu", [
    ({"op": "update_workflow", "workflow": {"name": "x"}}, "requires `workflow_id`"),
    ({"op": "delete_workflow", "workflow_id": "w1", "tag_ids": ["t1"]}, "does not use `tag_ids`"),
    ({"op": "set_workflow_tags", "workflow_id": "w1"}, "requires `tag_ids`"),
    ({"op": "create_workflow", "workflow_id": "w1", "workflow": _LU}, "does not use `workflow_id`"),
])
def test_une_intention_incoherente_est_refusee_sans_rien_faire(client, kw, attendu):
    with pytest.raises(McpError, match=attendu):
        _tool("n8n_act").fn(**kw)
    assert client.built == []
