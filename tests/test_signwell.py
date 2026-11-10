"""Connecteur SignWell — documents, modèles, envois groupés, webhooks, compte.

Verrouille : l'entrée de registre (keyed byo-only, deux modules), la doc how-to, la
surface MCP (5 tools décrits, annotés `-> dict`), la sonde « tester la connexion »,
la jointure tool↔client oto-core (les 26 méthodes), le dispatch `op=` (requis
manquant et argument hors op refusés avant le réseau), et ce que la couche tool
AJOUTE au transport :
  1. un document créé est un BROUILLON sauf `draft=False` explicite,
  2. la vue d'un document : un lien par destinataire, un `emailed` explicite,
  3. le PDF signé rendu en lien (`url_only` forcé), jamais en octets,
  4. `dry_run` sur toute mutation, qui n'appelle JAMAIS le client mutant,
  5. l'envoi groupé en aperçu par défaut.
"""
import asyncio
import base64
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from oto_mcp.mcp_errors import McpError

from oto_mcp import providers
from oto_mcp.connectors import verify as connector_verify
from oto_mcp.tool_visibility import namespace_of
from oto_mcp.tools import signwell, signwell_envois

pytestmark = pytest.mark.exige_pin_oto_core

EXPECTED_TOOLS = {"signwell_document", "signwell_template", "signwell_bulk_send",
                  "signwell_webhook", "signwell_account"}

CLIENT_METHODS = (
    "get_me", "get_api_application", "delete_api_application",
    "create_document", "get_document", "delete_document", "send_document",
    "send_reminder", "update_recipients", "update_authentication",
    "get_completed_pdf", "get_nom151_certificate",
    "create_template", "get_template", "update_template", "delete_template",
    "create_document_from_template",
    "list_bulk_sends", "get_bulk_send", "get_bulk_send_documents",
    "get_bulk_send_csv_template", "validate_bulk_send_csv", "create_bulk_send",
    "list_webhooks", "create_webhook", "delete_webhook",
)

FILES = [{"name": "nda.pdf", "file_url": "https://files.example/nda.pdf"}]
RECIPIENTS = [{"id": "1", "name": "Signataire", "email": "signataire@example.com"}]


def _doc(**over):
    doc = {
        "id": "doc-1", "name": "NDA", "status": "Sent", "test_mode": False,
        "embedded_signing": False, "apply_signing_order": True,
        "fields": [[{"type": "signature"}, {"type": "text"}]],
        "files": [{"name": "nda.pdf"}],
        "recipients": [{"id": "1", "name": "Signataire", "email": "signataire@example.com",
                        "status": "sent", "signing_order": 1,
                        "signing_url": "https://www.signwell.com/docs/abc/",
                        "embedded_signing_url": None, "send_email": False}],
    }
    doc.update(over)
    return doc


@pytest.fixture(scope="module")
def all_tools():
    from fastmcp import FastMCP
    from oto_mcp.tools import register_all

    m = FastMCP("t")
    register_all(m)
    return {t.name: t for t in asyncio.run(m._list_tools())}


@pytest.fixture(autouse=True)
def _fake_key(monkeypatch):
    monkeypatch.setattr(
        "oto_mcp.access.resolve_api_key", lambda provider, account=None: ("k", False))


@contextmanager
def _mock_client():
    """Les deux modules montés avec `SignWellClient` mocké, DANS le patch (le
    socle importe la classe à chaque appel)."""
    from fastmcp import FastMCP

    with patch("oto.tools.signwell.SignWellClient") as cls:
        m = FastMCP("t")
        signwell.register(m)
        signwell_envois.register(m)
        yield m, cls.return_value


def _call(tool_name, setup=None, **kwargs):
    with _mock_client() as (m, inst):
        if setup:
            setup(inst)
        fn = asyncio.run(m.get_tool(tool_name)).fn
        return fn(**kwargs), inst


# --- registre -----------------------------------------------------------------

def test_signwell_is_keyed_byo_only_connector():
    c = providers.REGISTRY["signwell"]
    assert c.kind == "tools"
    assert c.keyed and c.secret_kind == "api_key"
    assert c.auth_modes == frozenset({"byo_user", "byo_org"})
    assert "platform" not in c.auth_modes
    assert c.default_active is False
    assert c.default_quota == 0
    assert c.modules == ("signwell", "signwell_envois")
    assert "signwell" in providers.KEY_PROVIDERS
    assert c.category == "Workspace"
    assert c.publisher_name == "SignWell"
    assert providers._LOGO_DOMAIN_BY_CONNECTOR["signwell"] == "signwell.com"


def test_signwell_has_onboarding_doc():
    kinds = {s.kind for s in providers.REGISTRY["signwell"].doc_sections}
    assert {"prerequisite", "usage", "note"} <= kinds


# --- surface MCP ------------------------------------------------------------------

def test_tools_register_under_namespace_with_descriptions(all_tools):
    assert EXPECTED_TOOLS <= set(all_tools)
    for name in EXPECTED_TOOLS:
        assert namespace_of(name) == "signwell"
        assert all_tools[name].description, f"{name} has no description"


def test_tools_are_annotated_plain_dict():
    """Un `-> object` fabrique l'enveloppe `{"result": …}` que la dette d'outils
    interdit à un outil neuf (tests/structured_output_debt.txt)."""
    import inspect
    with _mock_client() as (m, _):
        for name in EXPECTED_TOOLS:
            fn = asyncio.run(m.get_tool(name)).fn
            assert inspect.signature(fn).return_annotation in (dict, "dict"), name


def test_verify_probe_registered():
    with _mock_client():
        pass
    assert connector_verify.supports("signwell")


def test_client_exposes_every_method_the_tools_call():
    from oto.tools.signwell import SignWellClient
    for meth in CLIENT_METHODS:
        assert callable(getattr(SignWellClient, meth, None)), f"SignWellClient.{meth} manquant"
    public = {n for n in dir(SignWellClient)
              if not n.startswith("_") and callable(getattr(SignWellClient, n))}
    assert public == set(CLIENT_METHODS)


# --- dispatch ---------------------------------------------------------------------

def test_missing_required_arg_is_refused_before_the_network():
    with pytest.raises(McpError):
        _call("signwell_document", op="get")
    with pytest.raises(McpError):
        _call("signwell_document", op="create", files=FILES)          # recipients
    with pytest.raises(McpError):
        _call("signwell_webhook", op="delete")


def test_irrelevant_arg_is_refused_not_ignored():
    with pytest.raises(McpError):
        _call("signwell_document", op="get", document_id="d", subject="hello")
    with pytest.raises(McpError):
        _call("signwell_bulk_send", op="list", dry_run=True)


def test_unknown_option_key_is_refused():
    with pytest.raises(McpError, match="unknown options"):
        _call("signwell_document", op="create", files=FILES, recipients=RECIPIENTS,
              options={"copied_contact": []})


def test_file_needs_exactly_one_source():
    with pytest.raises(McpError, match="exactly ONE source"):
        _call("signwell_document", op="create", recipients=RECIPIENTS,
              files=[{"name": "a.pdf", "file_url": "u", "file_base64": "b"}])


def test_duplicate_recipient_id_is_refused():
    with pytest.raises(McpError, match="repeated"):
        _call("signwell_document", op="create", files=FILES,
              recipients=RECIPIENTS + [{"id": "1", "email": "b@example.com"}])


# --- 1. créer = brouillon ---------------------------------------------------------

def test_create_is_a_draft_by_default():
    res, inst = _call("signwell_document",
                      setup=lambda i: setattr(i.create_document, "return_value",
                                              _doc(status="Draft")),
                      op="create", files=FILES, recipients=RECIPIENTS, text_tags=True)
    assert inst.create_document.call_args.kwargs["draft"] is True
    assert "next_step" in res


def test_explicit_send_on_create_needs_a_field():
    with pytest.raises(McpError, match="at least one field"):
        _call("signwell_document", op="create", files=FILES, recipients=RECIPIENTS,
              draft=False)


def test_template_document_is_a_draft_by_default():
    _, inst = _call("signwell_template",
                    setup=lambda i: setattr(i.create_document_from_template,
                                            "return_value", _doc(status="Draft")),
                    op="create_document", template_id="t1",
                    recipients=[{"id": "1", "placeholder_name": "Client",
                                 "email": "c@example.com"}])
    assert inst.create_document_from_template.call_args.kwargs["draft"] is True


# --- 2. la vue d'un document ------------------------------------------------------

def test_view_gives_one_link_and_explicit_emailed():
    res, _ = _call("signwell_document",
                   setup=lambda i: setattr(i.get_document, "return_value", _doc()),
                   op="get", document_id="doc-1")
    r = res["recipients"][0]
    assert r["signing_link"] == "https://www.signwell.com/docs/abc/"
    assert r["emailed"] is True
    assert res["fields_count"] == 2


def test_embedded_link_comes_from_the_other_key_and_is_not_emailed():
    doc = _doc(embedded_signing=True)
    doc["recipients"][0].update(signing_url=None,
                                embedded_signing_url="https://www.signwell.com/docs/emb/")
    res, _ = _call("signwell_document",
                   setup=lambda i: setattr(i.get_document, "return_value", doc),
                   op="get", document_id="doc-1")
    assert res["recipients"][0]["signing_link"].endswith("/emb/")
    assert res["recipients"][0]["emailed"] is False
    assert any("whoever" in n for n in res["notes"])


def test_test_mode_says_invites_go_to_the_owner():
    res, _ = _call("signwell_document",
                   setup=lambda i: setattr(i.get_document, "return_value",
                                           _doc(test_mode=True)),
                   op="get", document_id="doc-1")
    assert res["recipients"][0]["emailed"] is False
    assert any("HOLDER" in n for n in res["notes"])


def test_sending_status_is_not_reported_as_sent():
    res, _ = _call("signwell_document",
                   setup=lambda i: setattr(i.get_document, "return_value",
                                           _doc(status="Sending")),
                   op="get", document_id="doc-1")
    assert any("Sending" in n for n in res["notes"])


def test_full_returns_raw_payload():
    raw = _doc()
    res, _ = _call("signwell_document",
                   setup=lambda i: setattr(i.get_document, "return_value", raw),
                   op="get", document_id="doc-1", full=True)
    assert res == {"document": raw}


# --- 3. le PDF signé en lien ------------------------------------------------------

def test_completed_pdf_forces_url_only():
    res, inst = _call("signwell_document",
                      setup=lambda i: setattr(i.get_completed_pdf, "return_value",
                                              {"file_url": "https://www.signwell.com/signed/x.pdf"}),
                      op="completed_pdf", document_id="doc-1", audit_page=True)
    assert inst.get_completed_pdf.call_args.kwargs["url_only"] is True
    assert res["file_url"].endswith("x.pdf")


# --- 4. dry_run n'écrit jamais ------------------------------------------------------

@pytest.mark.parametrize("tool,kwargs,mutating", [
    ("signwell_document", dict(op="create", files=FILES, recipients=RECIPIENTS,
                               draft=False, text_tags=True), "create_document"),
    ("signwell_act", dict(op="send_document", document_id="doc-1"), "send_document"),
    ("signwell_act", dict(op="remind_document", document_id="doc-1"), "send_reminder"),
    ("signwell_document", dict(op="update_recipients", document_id="doc-1",
                               recipients=[{"id": "1", "name": "N", "email": "n@example.com"}]),
     "update_recipients"),
    ("signwell_document", dict(op="update_authentication", document_id="doc-1",
                               recipients=[{"id": "1", "passcode": "secret-code"}]),
     "update_authentication"),
    ("signwell_document", dict(op="delete", document_id="doc-1"), "delete_document"),
    ("signwell_template", dict(op="delete", template_id="t1"), "delete_template"),
    ("signwell_webhook", dict(op="create", callback_url="https://hook.example"),
     "create_webhook"),
    ("signwell_webhook", dict(op="delete", webhook_id="h1"), "delete_webhook"),
    ("signwell_account", dict(op="delete_api_application", application_id="a1"),
     "delete_api_application"),
])
def test_dry_run_never_calls_the_mutating_method(tool, kwargs, mutating):
    def setup(i):
        i.get_document.return_value = _doc()
        i.list_webhooks.return_value = []
    res, inst = _call(tool, setup=setup, dry_run=True, **kwargs)
    assert res["dry_run"] is True
    getattr(inst, mutating).assert_not_called()


def test_dry_run_recipient_change_is_a_real_diff():
    res, _ = _call("signwell_document",
                   setup=lambda i: setattr(i.get_document, "return_value", _doc()),
                   op="update_recipients", document_id="doc-1", dry_run=True,
                   recipients=[{"id": "1", "name": "Signataire", "email": "new@example.com"}])
    change = res["changes"][0]
    assert change["email"] == {"from": "signataire@example.com", "to": "new@example.com"}
    assert "name" not in change


def test_passcode_is_never_echoed_in_preview():
    res, _ = _call("signwell_document",
                   setup=lambda i: setattr(i.get_document, "return_value", _doc()),
                   op="update_authentication", document_id="doc-1", dry_run=True,
                   recipients=[{"id": "1", "passcode": "secret-code"}])
    assert "secret-code" not in repr(res)


def test_create_preview_never_echoes_base64():
    res, _ = _call("signwell_document", op="create", dry_run=True, recipients=RECIPIENTS,
                   files=[{"name": "a.pdf", "file_base64": "QUJDREVGRw=="}])
    assert "QUJDREVGRw==" not in repr(res)


# --- 5. envoi groupé : aperçu par défaut --------------------------------------------

def test_bulk_create_is_a_preview_by_default():
    res, inst = _call("signwell_bulk_send",
                      setup=lambda i: setattr(i.validate_bulk_send_csv, "return_value",
                                              {"ok": True}),
                      op="create", template_ids=["t1"],
                      csv="email,name\na@example.com,A\nb@example.com,B\n")
    assert res["dry_run"] is True and res["rows"] == 2
    inst.create_bulk_send.assert_not_called()
    sent_b64 = inst.validate_bulk_send_csv.call_args.args[1]
    assert base64.b64decode(sent_b64).decode().startswith("email,name")


def test_bulk_create_sends_only_with_explicit_false():
    _, inst = _call("signwell_bulk_send", op="create", template_ids=["t1"],
                    csv="email\na@example.com\n", dry_run=False)
    inst.create_bulk_send.assert_called_once()


def test_bulk_documents_page_is_projected():
    page = {"id": "b1", "current_page": 1, "total_pages": 1, "documents": [_doc()]}
    res, _ = _call("signwell_bulk_send",
                   setup=lambda i: setattr(i.get_bulk_send_documents, "return_value", page),
                   op="documents", bulk_send_id="b1")
    assert res["documents"][0]["recipients"][0]["signing_link"]
    assert "fields" not in res["documents"][0]
    assert "projection" in res


def test_webhook_list_is_wrapped_in_a_named_key():
    res, _ = _call("signwell_webhook",
                   setup=lambda i: setattr(i.list_webhooks, "return_value",
                                           [{"id": "h1", "callback_url": "https://x"}]),
                   op="list")
    assert res == {"webhooks": [{"id": "h1", "callback_url": "https://x"}]}


def test_401_is_translated_to_key_instruction():
    from oto.tools.common.errors import UpstreamHTTPError

    def setup(i):
        i.get_me.side_effect = UpstreamHTTPError(401, {"message": "Unauthorized"},
                                                 service="signwell")
    with pytest.raises(McpError, match="rejects this key"):
        _call("signwell_account", setup=setup, op="me")


def test_completed_pdf_404_names_the_unsigned_case():
    """Relevé en live : un document qui existe mais n'est pas complété rend 404."""
    from oto.tools.common.errors import UpstreamHTTPError

    def setup(i):
        i.get_completed_pdf.side_effect = UpstreamHTTPError(
            404, {"message": "Not found", "meta": {"messages": ["Couldn't find the document requested"]}},
            service="signwell")
    with pytest.raises(McpError, match="not yet signed"):
        _call("signwell_document", setup=setup, op="completed_pdf", document_id="doc-1")


def test_refusal_reads_meta_messages():
    from oto.tools.common.errors import UpstreamHTTPError
    from oto_mcp.tools.signwell_socle import refus
    e = UpstreamHTTPError(422, {"message": "Unprocessable", "meta": {
        "messages": ["recipients[0].email is invalid"]}}, service="signwell")
    assert "recipients[0].email is invalid" in refus(e)


@pytest.mark.parametrize("tool,kwargs", [
    ("signwell_document", dict(op="completed_pdf", document_id="d")),
    ("signwell_document", dict(op="delete", document_id="d")),
    ("signwell_template", dict(op="get", template_id="t")),
    ("signwell_template", dict(op="delete", template_id="t")),
    ("signwell_bulk_send", dict(op="list")),
    ("signwell_bulk_send", dict(op="get", bulk_send_id="b")),
])
def test_full_is_refused_where_it_changes_nothing(tool, kwargs):
    """`full` ne vaut que là où une vue resserrée existe ; ailleurs il laisserait
    croire qu'il a rendu davantage."""
    with pytest.raises(McpError, match="full"):
        _call(tool, full=True, **kwargs)


# --- corrections après fusion (oto-backend#984) ---------------------------------

def test_verify_refuses_an_empty_key_before_building_the_client():
    """Construit sur une clé vide, le client retombe sur le secret du SERVEUR
    (`SIGNWELL_API_KEY`) : la sonde validerait la clé de quelqu'un d'autre et dirait
    « connexion OK » à une carte vide."""
    with patch("oto.tools.signwell.SignWellClient") as cls:
        for vide in ({}, {"key": ""}, {"key": "   "}):
            with pytest.raises(ValueError, match="Empty"):
                signwell._verify(vide)
    cls.assert_not_called()


@pytest.mark.parametrize("tool,kwargs", [
    ("signwell_document", dict(op="completed_pdf", document_id="d", dry_run=False)),
    ("signwell_document", dict(op="get", document_id="d", dry_run=False)),
    ("signwell_template", dict(op="get", template_id="t", full=False)),
    ("signwell_bulk_send", dict(op="list", full=False)),
    ("signwell_webhook", dict(op="list", dry_run=False)),
    ("signwell_account", dict(op="me", dry_run=False)),
])
def test_explicit_false_is_refused_where_the_op_ignores_it(tool, kwargs):
    """Un `False` EXPLICITE est un argument donné : refusé là où l'`op` ne s'en sert
    pas, comme n'importe quel autre — seule l'omission (`None`) vaut absence."""
    with pytest.raises(McpError, match="does not take"):
        _call(tool, **kwargs)


def test_completed_pdf_judges_the_upstream_status_not_the_text():
    """Le cas « pas encore signé » se reconnaît au STATUT 404 amont. Un autre refus
    dont le texte contient « (404) » reste ce qu'il est."""
    from oto.tools.common.errors import UpstreamHTTPError

    def setup(i):
        i.get_completed_pdf.side_effect = UpstreamHTTPError(
            409, {"message": "Conflict", "meta": {"messages": ["audit page pending (404)"]}},
            service="signwell")
    with pytest.raises(McpError) as e:
        _call("signwell_document", setup=setup, op="completed_pdf", document_id="doc-1")
    assert "(409)" in str(e.value)
    assert "not yet signed" not in str(e.value)
