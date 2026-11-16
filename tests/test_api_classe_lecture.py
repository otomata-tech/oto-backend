"""`class: read` d'une action du « Custom API » face au mode `session` (lecture seule).

Une carte en mode `session` se connecte avec l'identifiant et le mot de passe d'une
personne : son agent ne fait que lire, tout verbe d'écriture est refusé par nom
(`api_session_read_only`). Une action que l'admin de la carte déclare `class: read`
(une recherche envoyée en POST) passe ce refus — elle seule : une action POST sans
`class`, et tout appel brut (`op=request`), restent refusés. Sans base : la résolution
de l'action est doublée, le refus est joué à blanc (`dry_run`), avant tout client.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from oto_mcp.mcp_errors import McpError
from oto_mcp.tools import api as H
from oto_mcp.tools import api_calls

SESSION = {"base_url": "https://api.service.test", "auth_mode": "session",
           "username": "u", "password": "p", "session": "{}"}
OPERATIONS = [
    {"name": "chercher", "description": "Search.", "method": "POST", "path": "/search",
     "input": {"type": "object", "properties": {"q": {"type": "string"}}},
     "class": "read"},
    {"name": "creer", "description": "Create.", "method": "POST", "path": "/items",
     "input": {"type": "object", "properties": {"q": {"type": "string"}}}},
]


@pytest.fixture
def carte(monkeypatch):
    rc = SimpleNamespace(account="espace", fields=dict(SESSION), entity_type="org",
                         entity_id=1)
    d = {"operations": OPERATIONS, "variables": {}, "headers": {}}

    def action(sub, operation):
        found = next((o for o in OPERATIONS if o["name"] == operation), None)
        assert found is not None
        return rc, d, found

    monkeypatch.setattr(api_calls, "_action", action)
    monkeypatch.setattr(api_calls, "_resolved", lambda account=None: rc)
    monkeypatch.setattr(api_calls, "_definition", lambda rc: d)
    return rc


def test_une_action_class_read_passe_la_lecture_seule(carte):
    plan = api_calls._call("sub-1", "chercher", {"q": "x"}, dry_run=True)
    assert plan["dry_run"] and plan["method"] == "POST" and plan["body"] == {"q": "x"}


def test_une_action_post_sans_class_reste_refusee(carte):
    with pytest.raises(McpError) as pris:
        api_calls._call("sub-1", "creer", {"q": "x"}, dry_run=True)
    assert pris.value.error.data["code"] == "api_session_read_only"


def test_un_appel_brut_reste_refuse(carte):
    with pytest.raises(McpError) as pris:
        api_calls._request("POST", "/search", None, {"q": "x"}, dry_run=True)
    assert pris.value.error.data["code"] == "api_session_read_only"


def test_le_refus_nomme_la_sortie():
    with pytest.raises(McpError) as pris:
        H.refuse_write(SESSION, "POST")
    assert "class: read" in pris.value.error.message
    H.refuse_write(SESSION, "POST", reads=True)
    H.refuse_write({**SESSION, "auth_mode": "basic"}, "POST")
