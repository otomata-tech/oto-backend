"""Connecteur Qonto — banque d'entreprise, lecture seule.

Les tripwires génériques couvrent déjà le registre, l'éditeur, le logo, la prose
servie, la paire lire/agir et la jointure au client oto-core. Ce fichier verrouille
ce qui est PROPRE à ce module :

- un seul outil `qonto` (LECTURE), le verbe de lecture dans `op` ;
- chaque op appelle la BONNE méthode, avec les bons arguments, et la clé
  `login:secret-key` arrive au client — jointe depuis les deux cases (identifiant
  en `meta`, clé secrète au coffre) ou lue telle quelle (clé posée avant la scission) ;
- les bornes de date posées sur le champ de l'op, `date_field` pour un autre ;
- les transactions sans compte lisent le compte principal ;
- `all_pages` suit `meta.next_page`, borné ; la vue par défaut allège les lignes ;
- aucun argument ignoré en silence ;
- la traduction des erreurs Qonto (401 clé, 401 OAuth exigé) et la sonde.

Ne tournent qu'après installation d'oto-core avec `oto.tools.qonto`. Toutes les
valeurs sont factices.
"""
import asyncio
from unittest.mock import patch

import pytest

from oto_mcp import providers
from oto_mcp.mcp_errors import McpError

KEY = "acme-1234:sk_test"
ACCOUNT = "acc-main"
ORG = {"organization": {"bank_accounts": [
    {"id": "acc-other", "main": False}, {"id": ACCOUNT, "main": True}]}}


class _Resolved:
    """Ce que `access.resolve_credential` rend : la clé du coffre, le `meta` en config."""

    def __init__(self, key, config=None):
        self.key, self.config = key, config or {}


@pytest.fixture(autouse=True)
def _fake_key(monkeypatch):
    monkeypatch.setattr("oto_mcp.access.resolve_credential",
                        lambda provider, want="auto", account=None: _Resolved(KEY))


def _tools():
    from fastmcp import FastMCP
    from oto_mcp.tools import qonto

    m = FastMCP("t")
    qonto.register(m)
    return {t.name: t for t in asyncio.run(m._list_tools())}


def _fn():
    return _tools()["qonto"].fn


@pytest.fixture()
def client():
    with patch("oto.tools.qonto.QontoClient") as cls:
        cls.return_value.get_organization.return_value = ORG
        yield cls


# --- registre ---------------------------------------------------------------

def test_registry_entry():
    c = providers.REGISTRY["qonto"]
    assert c.secret_kind == "api_key"
    assert c.keyed is True
    assert c.auth_modes == {"byo_user", "byo_org"}
    assert c.category == "Finance"
    assert {f.name: f.secret for f in c.credential_fields} == {"login": False, "key": True}
    # The login lives in `meta`: the vault blob keeps ONE field, so a key saved
    # before the split (`login:secret-key`, raw) still unpacks.
    assert [f.name for f in c.vault_fields] == ["key"]


def test_a_key_saved_before_the_split_still_unpacks():
    from oto_mcp import credentials_store

    assert credentials_store.unpack_secret("qonto", KEY) == {"key": KEY}
    assert credentials_store.pack_secret("qonto", {"login": "acme-1234", "key": "sk"}) == "sk"


def test_one_read_tool_served():
    tools = _tools()
    assert set(tools) == {"qonto"}
    t = tools["qonto"]
    assert t.description
    assert t.annotations.readOnlyHint is True


# --- ops -> méthodes ----------------------------------------------------------

def test_key_reaches_the_client_and_default_op_reads_accounts(client):
    assert _fn()() == ORG
    client.assert_called_once_with(api_key=KEY)
    client.return_value.get_organization.assert_called_once_with()


def test_split_credential_is_joined_for_the_client(client, monkeypatch):
    monkeypatch.setattr("oto_mcp.access.resolve_credential",
                        lambda provider, want="auto", account=None:
                        _Resolved("sk_test", {"login": "acme-1234"}))
    _fn()()
    client.assert_called_once_with(api_key=KEY)


def test_login_in_meta_wins_over_a_legacy_key(client, monkeypatch):
    monkeypatch.setattr("oto_mcp.access.resolve_credential",
                        lambda provider, want="auto", account=None:
                        _Resolved("old-1:sk_test", {"login": "acme-1234"}))
    _fn()()
    client.assert_called_once_with(api_key=KEY)


def test_split_credential_without_login_is_refused_clearly(client, monkeypatch):
    monkeypatch.setattr("oto_mcp.access.resolve_credential",
                        lambda provider, want="auto", account=None: _Resolved("sk_test"))
    with pytest.raises(McpError, match="Login is missing"):
        _fn()()
    client.assert_not_called()


@pytest.mark.parametrize("key, login, expected", [
    ("sk_test", "acme-1234", KEY),                 # the two boxes
    (KEY, None, KEY),                              # saved before the split
    (KEY, "acme-1234", KEY),                       # pair pasted in the secret box
    ("old-1:sk_test", "acme-1234", KEY),           # login edited over a legacy row
    ("  sk_test \n", " acme-1234 ", KEY),          # copy-paste debris
])
def test_authorization_joins_the_two_halves(key, login, expected):
    from oto_mcp.tools import qonto

    assert qonto._authorization(key, login) == expected


@pytest.mark.parametrize("key, login, match", [
    ("sk_test", None, "Login is missing"),
    ("", "acme-1234", "Secret key is empty"),
    ("acme-1234:", None, "Secret key is empty"),
])
def test_authorization_names_the_missing_half(key, login, match):
    from oto_mcp.tools import qonto

    with pytest.raises(ValueError, match=match):
        qonto._authorization(key, login)


def test_transactions_without_account_read_the_main_one(client):
    _fn()(op="transactions", side="credit", date_from="2026-10-01",
          date_to="2026-10-31", includes=["vat_details"])
    client.return_value.list_transactions.assert_called_once_with(
        ACCOUNT, None, status=None, side="credit", operation_type=None,
        with_attachments=None, includes=["vat_details"], sort_by=None, page=None,
        per_page=None, settled_at_from="2026-10-01", settled_at_to="2026-10-31")


def test_transactions_by_iban_and_another_date_field(client):
    _fn()(op="transactions", iban="FR76X", date_from="2026-10-01",
          date_field="emitted_at")
    client.return_value.get_organization.assert_not_called()
    kwargs = client.return_value.list_transactions.call_args
    assert kwargs.args == (None, "FR76X")
    assert kwargs.kwargs["emitted_at_from"] == "2026-10-01"
    assert "settled_at_from" not in kwargs.kwargs


def test_a_date_field_the_op_does_not_filter_is_refused(client):
    with pytest.raises(McpError, match="due_date"):
        _fn()(op="transactions", date_from="2026-10-01", date_field="due_date")


@pytest.mark.parametrize("op, method, id_", [
    ("transaction", "get_transaction", "t-1"),
    ("attachment", "get_attachment", "a-1"),
    ("statement", "get_statement", "s-1"),
    ("supplier_invoice", "get_supplier_invoice", "i-1"),
    ("client_invoice", "get_client_invoice", "i-2"),
    ("client", "get_client", "c-1"),
])
def test_by_id_ops(client, op, method, id_):
    _fn()(op=op, id=id_)
    assert getattr(client.return_value, method).call_args.args[0] == id_


def test_by_id_needs_an_id(client):
    with pytest.raises(McpError, match="`id` is required"):
        _fn()(op="transaction")


def test_supplier_invoices_take_one_status_and_created_window(client):
    _fn()(op="supplier_invoices", status=["to_review"], query="Acme",
          date_from="2026-10-01T00:00:00Z")
    client.return_value.list_supplier_invoices.assert_called_once_with(
        "to_review", query="Acme", page=None, per_page=None,
        created_at_from="2026-10-01T00:00:00Z", created_at_to=None)
    with pytest.raises(McpError, match="ONE status"):
        _fn()(op="supplier_invoices", status=["paid", "to_review"])


def test_client_invoices_by_due_date(client):
    _fn()(op="client_invoices", status=["unpaid"], date_to="2026-10-31",
          date_field="due_date")
    client.return_value.list_client_invoices.assert_called_once_with(
        "unpaid", sort_by=None, page=None, per_page=None,
        due_date_from=None, due_date_to="2026-10-31")


def test_statements_take_a_month_or_a_date(client):
    _fn()(op="statements", date_from="2026-01-15", date_to="12-2026")
    client.return_value.list_statements.assert_called_once_with(
        None, None, period_from="01-2026", period_to="12-2026", sort_by=None,
        page=None, per_page=None)


def test_clients_search_by_name(client):
    _fn()(op="clients", query="Kla")
    assert client.return_value.list_clients.call_args.kwargs["name"] == "Kla"


@pytest.mark.parametrize("op, method", [
    ("transfers", "list_sepa_transfers"), ("beneficiaries", "list_sepa_beneficiaries"),
    ("requests", "list_requests"), ("members", "list_memberships"),
    ("labels", "list_labels"), ("credit_notes", "list_credit_notes"),
    ("quotes", "list_quotes"),
])
def test_list_ops(client, op, method):
    _fn()(op=op)
    getattr(client.return_value, method).assert_called_once()


# --- arguments ignorés ----------------------------------------------------------

@pytest.mark.parametrize("op, kwargs", [
    ("accounts", {"page": 2}),
    ("labels", {"side": "debit"}),
    ("client_invoices", {"query": "x"}),
    ("transaction", {"date_from": "2026-10-01"}),
    ("members", {"date_from": "2026-10-01"}),
    ("transfers", {"id": "x"}),
])
def test_an_argument_the_op_does_not_read_is_refused(client, op, kwargs):
    with pytest.raises(McpError, match="does not take|filters"):
        _fn()(op=op, **({"id": "x"} if op == "transaction" else {}), **kwargs)


# --- pages et vue -------------------------------------------------------------------

def test_all_pages_follows_next_page_and_merges(client):
    client.return_value.list_labels.side_effect = [
        {"labels": [{"id": 1}], "meta": {"next_page": 2, "total_count": 2}},
        {"labels": [{"id": 2}], "meta": {"next_page": None, "total_count": 2}},
    ]
    out = _fn()(op="labels", all_pages=True)
    assert out == {"labels": [{"id": 1}, {"id": 2}],
                   "meta": {"pages_read": 2, "total_count": 2, "truncated": False,
                            "next_page": None}}
    assert [c.kwargs["page"] for c in client.return_value.list_labels.call_args_list] == [None, 2]


def test_all_pages_is_bounded(client):
    client.return_value.list_labels.return_value = {
        "labels": [{"id": 1}], "meta": {"next_page": 99, "total_count": 10_000}}
    out = _fn()(op="labels", all_pages=True)
    assert out["meta"]["pages_read"] == 20
    assert out["meta"]["truncated"] is True


def test_default_view_trims_transactions_full_keeps_raw(client):
    row = {"id": "t", "amount": 1.0, "logo": {"small": "x"}, "amount_cents": 100}
    client.return_value.list_transactions.return_value = {"transactions": [row], "meta": {}}
    assert _fn()(op="transactions")["transactions"] == [{"id": "t", "amount": 1.0}]
    assert _fn()(op="transactions", full=True)["transactions"] == [row]


# --- erreurs et sonde ------------------------------------------------------------------

def _upstream(status, body):
    from oto.tools.common.errors import UpstreamHTTPError
    return UpstreamHTTPError(status_code=status, body=body, service="qonto")


def test_401_names_both_halves(client):
    client.return_value.get_organization.side_effect = _upstream(
        401, {"errors": [{"code": "unauthorized"}]})
    with pytest.raises(McpError, match="Login .* Secret key"):
        _fn()()


def test_401_oauth_only_is_said(client):
    client.return_value.list_labels.side_effect = _upstream(
        401, {"errors": [{"detail": "OAuth2 authentication is required here"}]})
    with pytest.raises(McpError, match="OAuth"):
        _fn()(op="labels")


def test_probe_refuses_a_bad_key():
    from oto_mcp.connectors import verify
    from oto_mcp.tools import qonto

    with pytest.raises(verify.NonAutorise):
        qonto._verify({"key": "no-colon"})
    with patch("oto.tools.qonto.QontoClient") as cls:
        cls.return_value.probe.side_effect = _upstream(401, "nope")
        # The saving screen shows this message: plain words, not Qonto's raw body.
        with pytest.raises(verify.NonAutorise, match="not an email") as e:
            qonto._verify({"key": KEY})
        assert "trace_id" not in str(e.value)


def test_probe_joins_the_login_from_fields_or_config():
    from oto_mcp.tools import qonto

    with patch("oto.tools.qonto.QontoClient") as cls:
        qonto._verify({"login": "acme-1234", "key": "sk_test"})
        cls.assert_called_with(api_key=KEY)
        qonto._verify({"key": "sk_test"}, {"login": "acme-1234"})
        cls.assert_called_with(api_key=KEY)
