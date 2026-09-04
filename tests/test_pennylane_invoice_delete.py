"""`pennylane_invoice(op="delete")` — oto-backend#715 (signaux #423, #602).

La suppression d'un brouillon de facture existait déjà côté CLI
(`oto pennylane delete-invoice`, oto-core `PennylaneClient.delete_invoice`) mais
n'était pas exposée sur le tool MCP `pennylane_invoice` : ménage de brouillons
obsolètes impossible depuis l'agent, seul recours = l'UI web. Ce test câble le
routage `op="delete"` → `client.delete_invoice`, l'argument obligatoire, et le
refus explicite d'un document déjà finalisé (Pennylane le renvoie en erreur,
jamais un fallback silencieux — même convention que
`pennylane_supplier_invoice(op="import")`).
"""
import asyncio
from unittest.mock import MagicMock

import pytest
from oto_mcp.mcp_errors import McpError


def _tool(name: str):
    from fastmcp import FastMCP
    from oto_mcp.tools import pennylane as P

    m = FastMCP("t")
    P.register(m)
    return asyncio.run(m.get_tool(name)).fn


@pytest.fixture
def client(monkeypatch):
    """Faux PennylaneClient + clé résolue. `register()` importe `PennylaneClient`
    depuis le PACKAGE (`from oto.tools.pennylane import PennylaneClient`, pas
    depuis `.client`) : patcher l'attribut du package, avant l'appel à
    `register()`, est donc ce qu'il faut monkeypatcher pour que l'import capté
    à l'intérieur de la closure du tool voie le faux."""
    import oto.tools.pennylane as pkg

    inst = MagicMock()
    monkeypatch.setattr(pkg, "PennylaneClient", lambda **kw: inst)
    monkeypatch.setattr("oto_mcp.access.resolve_api_key", lambda *a, **k: ("k", False))
    return inst


def test_delete_calls_client_delete_invoice(client):
    _tool("pennylane_invoice")(op="delete", invoice_id=42)
    client.delete_invoice.assert_called_once_with(42)


def test_delete_requires_invoice_id(client):
    with pytest.raises(McpError, match="op='delete' requiert invoice_id"):
        _tool("pennylane_invoice")(op="delete")
    client.delete_invoice.assert_not_called()


def test_delete_raises_on_pennylane_refusal(client):
    """Un document déjà finalisé : Pennylane refuse. Depuis oto-core#77 le client
    LÈVE, et la garde du connecteur traduit — le refus doit remonter actionnable,
    jamais passer pour un succès silencieux. Le double échoue comme le vrai,
    sinon l'épreuve validerait un chemin qui n'existe plus."""
    from oto.tools.common.errors import UpstreamHTTPError

    client.delete_invoice.side_effect = UpstreamHTTPError(
        422, "Only drafts can be deleted", service="pennylane")
    with pytest.raises(McpError, match="Only drafts can be deleted"):
        _tool("pennylane_invoice")(op="delete", invoice_id=99)


def test_delete_returns_the_client_result_on_success(client):
    client.delete_invoice.return_value = {"ok": True}
    out = _tool("pennylane_invoice")(op="delete", invoice_id=42)
    assert out == {"ok": True}


def test_unknown_op_names_delete_among_the_allowed_ops(client):
    with pytest.raises(McpError, match="op doit être.*'delete'"):
        _tool("pennylane_invoice")(op="nope")
    client.delete_invoice.assert_not_called()


def test_default_op_never_deletes(client):
    """Le défaut de `op` reste une lecture — un appel sans `op` ne doit
    jamais pouvoir supprimer une facture."""
    _tool("pennylane_invoice")()
    client.delete_invoice.assert_not_called()
