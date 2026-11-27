"""`silae_act` — the four Silae payroll entries, in their own tool (`tools/silae_act.py`).

What this file locks:
  1. each op → its `SilaeClient` write, with the dossier and the matricule positional and
     everything else by keyword (two strings side by side swap without a type error);
  2. the refusals: unknown op, missing argument naming the op, an argument the op does not
     use, both or neither of `montant` / `valeur_chaine`, labels over Silae's lengths,
     a confirmation that confirms nothing;
  3. `dry_run` checks everything and calls nothing;
  4. the gates that are ours: the tool is `ACTE` (the client confirms it), and the
     `read` access level — the default — does not cover any of its ops.
The request bodies themselves are tested client-side, in oto-core.
"""
import asyncio
from unittest.mock import MagicMock

import pytest
from oto_mcp.mcp_errors import McpError

_WRITES = ("ajouter_element_variable", "ajouter_prime", "ajouter_heures", "confirmer_saisies")
_EMP = {"numero_dossier": "001", "matricule_salarie": "0042", "periode": "2026-05"}


@pytest.fixture
def client(monkeypatch):
    inst = MagicMock()
    for m in _WRITES:
        getattr(inst, m).return_value = {"ok": True}
    monkeypatch.setattr("oto.tools.silae.SilaeClient", lambda **kw: inst)
    monkeypatch.setattr("oto_mcp.access.resolve_credential_fields",
                        lambda service: {"client_id": "i", "client_secret": "s",
                                         "subscription_key": "k"})
    return inst


def _tool():
    from fastmcp import FastMCP
    from oto_mcp.tools import silae_act

    m = FastMCP("t")
    silae_act.register(m)
    return asyncio.run(m.get_tool("silae_act"))


def _act(**kw):
    return _tool().fn(**kw)


def _only_called(client, method):
    for m in _WRITES:
        if m == method:
            getattr(client, m).assert_called_once()
        else:
            getattr(client, m).assert_not_called()


# --- routing ------------------------------------------------------------------

def test_variable_element_goes_to_its_write_with_keywords(client):
    _act(op="add_variable_element", code="EV12", montant=150.0, controle_existence=True, **_EMP)
    _only_called(client, "ajouter_element_variable")
    client.ajouter_element_variable.assert_called_once_with(
        "001", "0042", periode="2026-05", code="EV12", montant=150.0, valeur_chaine=None,
        controle_existence=True)


def test_bonus_passes_its_labels(client):
    _act(op="add_bonus", code="PR01", montant=300.0, prime_nette=False,
         intitule_bulletin="Prime exceptionnelle", intitule_court="P.EXC", **_EMP)
    _only_called(client, "ajouter_prime")
    client.ajouter_prime.assert_called_once_with(
        "001", "0042", periode="2026-05", code="PR01", montant=300.0, valeur_chaine=None,
        prime_nette=False, intitule_bulletin="Prime exceptionnelle", intitule_court="P.EXC")


def test_hours_say_add_or_replace(client):
    _act(op="add_hours", code="HS25", nombre=4.5, ajouter=False, **_EMP)
    _only_called(client, "ajouter_heures")
    client.ajouter_heures.assert_called_once_with(
        "001", "0042", periode="2026-05", code="HS25", nombre=4.5, ajouter=False)


def test_confirmation_is_dossier_wide(client):
    _act(op="set_entries_confirmed", numero_dossier="001", periode="2026-05",
         confirmer_heures=True, confirmer_primes=False)
    _only_called(client, "confirmer_saisies")
    client.confirmer_saisies.assert_called_once_with(
        "001", periode="2026-05", confirmer_heures=True, confirmer_primes=False)


def test_a_text_value_replaces_the_amount(client):
    _act(op="add_variable_element", code="EV13", valeur_chaine="OUI", **_EMP)
    kw = client.ajouter_element_variable.call_args.kwargs
    assert kw["montant"] is None and kw["valeur_chaine"] == "OUI"


# --- refusals -----------------------------------------------------------------

def test_unknown_op_names_the_valid_ones(client):
    with pytest.raises(McpError, match="op must be") as e:
        _act(op="delete_payslip", **_EMP)
    assert "'set_entries_confirmed'" in str(e.value)
    for m in _WRITES:
        getattr(client, m).assert_not_called()


@pytest.mark.parametrize("kwargs,missing", [
    ({"op": "add_bonus", "code": "PR01", "montant": 1.0, "numero_dossier": "001",
      "periode": "2026-05"}, "matricule_salarie"),
    ({"op": "add_bonus", "montant": 1.0, **_EMP}, "code"),
    ({"op": "add_hours", "code": "HS25", "nombre": 2.0, **_EMP}, "ajouter"),
    ({"op": "add_hours", "code": "HS25", "ajouter": True, **_EMP}, "nombre"),
    ({"op": "set_entries_confirmed", "numero_dossier": "001", "periode": "2026-05",
      "confirmer_heures": True}, "confirmer_primes"),
    ({"op": "add_variable_element", "code": "EV12", "montant": 1.0, "numero_dossier": "001",
      "matricule_salarie": "0042", "periode": ""}, "periode"),
])
def test_a_missing_argument_names_the_op_and_the_argument(client, kwargs, missing):
    with pytest.raises(McpError, match=missing):
        _act(**kwargs)
    for m in _WRITES:
        getattr(client, m).assert_not_called()


def test_an_argument_the_op_does_not_use_is_refused(client):
    with pytest.raises(McpError, match="does not use nombre"):
        _act(op="add_bonus", code="PR01", montant=1.0, nombre=3.0, **_EMP)
    with pytest.raises(McpError, match="does not use matricule_salarie"):
        _act(op="set_entries_confirmed", confirmer_heures=True, confirmer_primes=True, **_EMP)


@pytest.mark.parametrize("value", [{}, {"montant": 1.0, "valeur_chaine": "OUI"},
                                   {"valeur_chaine": ""}])
def test_exactly_one_value(client, value):
    with pytest.raises(McpError, match="exactly one of montant"):
        _act(op="add_variable_element", code="EV12", **value, **_EMP)


def test_bonus_labels_keep_silae_lengths(client):
    with pytest.raises(McpError, match="45"):
        _act(op="add_bonus", code="PR01", montant=1.0, intitule_bulletin="x" * 46, **_EMP)
    with pytest.raises(McpError, match="10"):
        _act(op="add_bonus", code="PR01", montant=1.0, intitule_court="x" * 11, **_EMP)


def test_a_confirmation_must_confirm_something(client):
    with pytest.raises(McpError, match="hours, the bonuses or both"):
        _act(op="set_entries_confirmed", numero_dossier="001", periode="2026-05",
             confirmer_heures=False, confirmer_primes=False)


def test_a_silae_refusal_becomes_a_named_tool_error(client):
    from oto.tools.common.errors import UpstreamHTTPError

    client.ajouter_prime.side_effect = UpstreamHTTPError(
        403, {"errors": [{"code": "403", "message": "fonction non autorisée"}]},
        service="silae")
    with pytest.raises(McpError, match="HTTP 403"):
        _act(op="add_bonus", code="PR01", montant=1.0, **_EMP)


# --- dry_run ------------------------------------------------------------------

def test_dry_run_checks_everything_and_calls_nothing(client):
    out = _act(op="add_hours", code="HS25", nombre=4.5, ajouter=True, dry_run=True, **_EMP)
    assert out["dry_run"] is True and out["would_call"] == "ajouter_heures"
    assert out["arguments"]["ajouter"] is True
    for m in _WRITES:
        getattr(client, m).assert_not_called()
    with pytest.raises(McpError):
        _act(op="add_hours", code="HS25", nombre=4.5, dry_run=True, **_EMP)


# --- our gates ----------------------------------------------------------------

def test_the_tool_is_declared_acting():
    ann = _tool().annotations
    assert ann.readOnlyHint is False and ann.destructiveHint is True


def test_read_is_the_default_level_and_covers_no_entry():
    from oto_mcp.connectors import access_levels

    d = access_levels.DECLARED["silae"]
    assert d.default == "read"
    lu, tout = d.closure("read"), d.closure("full")
    for op, scopes in d.tools["silae_act"].items():
        assert not set(scopes) <= lu, op
        assert set(scopes) <= tout, op
    for tool, decl in d.tools.items():
        if tool == "silae_act":
            continue
        for scopes in (decl.values() if isinstance(decl, dict) else [decl]):
            assert set(scopes) <= lu, tool
