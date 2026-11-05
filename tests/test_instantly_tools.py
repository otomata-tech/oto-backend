"""Connecteur Instantly — prospection par email à froid, servi en paire.

Les tripwires génériques couvrent déjà le registre, l'éditeur, le logo, la prose servie,
la paire lire/agir, la déclaration des effets et la jointure au client oto-core. Ce
fichier verrouille ce qui est PROPRE à ce module :

- la paire `instantly` (LECTURE) / `instantly_act` (ACTE), une op par fonction de la
  description `instantly`, sauf une : `pause_campaign` est servie `set_campaign_paused`
  (le verbe d'une op d'action doit être un verbe à effet de la garde) ;
- chaque op appelle la BONNE méthode, avec les bons arguments, la clé arrive au client ;
- aucun argument ignoré en silence, les arguments requis nommés, rien n'est appelé avant ;
- la vue resserrée des listes (et `full`) ;
- la traduction des refus d'Instantly, le 403 qui nomme le scope manquant ;
- la sonde de connexion.

Toutes les valeurs sont factices.
"""
import asyncio
from unittest.mock import patch

import pytest

from oto_mcp import providers
from oto_mcp.mcp_errors import McpError

KEY = "instantly_key_test"
CAMPAIGN = "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b"
LIST = "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5d"


@pytest.fixture(autouse=True)
def _fake_key(monkeypatch):
    monkeypatch.setattr("oto_mcp.access.resolve_api_key",
                        lambda provider, account=None, units=1: (KEY, False))


def _tools():
    from fastmcp import FastMCP
    from oto_mcp.tools import instantly

    m = FastMCP("t")
    instantly.register(m)
    return {t.name: t for t in asyncio.run(m._list_tools())}


def _read():
    return _tools()["instantly"].fn


def _act():
    return _tools()["instantly_act"].fn


@pytest.fixture()
def client():
    with patch("oto.tools.instantly.InstantlyClient") as cls:
        yield cls


def _upstream(status, body=None):
    from oto.tools.common.errors import UpstreamHTTPError
    return UpstreamHTTPError(status, body or {"message": "x"}, service="instantly")


# --- registre et outils servis ------------------------------------------------------------

def test_registry_entry():
    c = providers.REGISTRY["instantly"]
    assert c.keyed and c.secret_kind == "api_key"
    assert c.auth_modes == {"byo_user", "byo_org"}
    from oto_mcp.providers import instantly as declaration
    assert declaration.CATEGORY == "Prospecting" and declaration.PUBLISHER == "Instantly"
    assert declaration.LOGO_DOMAIN == "instantly.ai"
    assert [(f.name, f.secret) for f in c.credential_fields] == [("key", True)]
    assert "all:read" in c.credential_fields[0].help and "all:all" in c.credential_fields[0].help


def test_the_pair_is_served_read_and_act():
    tools = _tools()
    assert set(tools) == {"instantly", "instantly_act"}
    assert tools["instantly"].annotations.readOnlyHint is True
    act = tools["instantly_act"].annotations
    assert act.readOnlyHint is False and act.destructiveHint is True and act.openWorldHint is True


def test_one_op_per_function_of_the_description():
    from oto.tools.instantly import InstantlyClient
    from oto_mcp.tools import instantly as t
    assert len(t._READ_OPS) + len(t._ACT_OPS) == 34
    public = {n for n in dir(InstantlyClient)
              if not n.startswith("_") and callable(getattr(InstantlyClient, n))}
    assert len(public) == 34
    assert set(t._SCOPES) == set(t._READ_OPS) | set(t._ACT_OPS)
    for accepts, requires in ((t._ACCEPTS, t._REQUIRES), (t._ACT_ACCEPTS, t._ACT_REQUIRES)):
        assert all(set(req) <= accepts[op] for op, req in requires.items())


# --- lecture : ops -> méthodes ------------------------------------------------------------

def test_the_key_reaches_the_client(client):
    _read()(op="workspace")
    client.assert_called_once_with(api_key=KEY)
    client.return_value.get_workspace.assert_called_once_with()


def test_campaigns_and_analytics(client):
    c = client.return_value
    _read()(op="campaigns", status=1, search="webinar", limit=20, starting_after="cur")
    c.list_campaigns.assert_called_once_with(search="webinar", status=1, tag_ids=None,
                                             limit=20, starting_after="cur")
    _read()(op="campaign_analytics", campaign_ids=[CAMPAIGN], start_date="2026-11-01",
            exclude_total_leads_count=True)
    c.get_campaign_analytics.assert_called_once_with(
        campaign_id=None, campaign_ids=[CAMPAIGN], start_date="2026-11-01", end_date=None,
        exclude_total_leads_count=True)
    _read()(op="analytics_steps", campaign_id=CAMPAIGN, include_opportunities_count=True)
    c.get_campaign_steps_analytics.assert_called_once_with(
        campaign_id=CAMPAIGN, start_date=None, end_date=None, include_opportunities_count=True)
    _read()(op="lead_campaigns", email="jane.doe@example.com")
    c.search_campaigns_by_lead.assert_called_once_with("jane.doe@example.com", sort_order=None)


def test_leads_take_campaign_id_as_the_api_campaign(client):
    _read()(op="leads", campaign_id=CAMPAIGN, filter="FILTER_LEAD_INTERESTED", limit=50)
    client.return_value.list_leads.assert_called_once_with(
        campaign=CAMPAIGN, list_id=None, search=None, contacts=None, ids=None,
        filter="FILTER_LEAD_INTERESTED", in_campaign=None, in_list=None, distinct_contacts=None,
        limit=50, starting_after=None)


def test_emails_since_until_are_the_creation_bounds(client):
    _read()(op="emails", email_type="received", is_unread=True, since="2026-11-01",
            until="2026-11-04")
    kwargs = client.return_value.list_emails.call_args.kwargs
    assert kwargs["min_timestamp_created"] == "2026-11-01"
    assert kwargs["max_timestamp_created"] == "2026-11-04"
    assert kwargs["email_type"] == "received" and kwargs["is_unread"] is True


def test_a_stray_or_missing_argument_is_refused_before_any_call(client):
    with pytest.raises(McpError, match="does not take `campaign_id`"):
        _read()(op="workspace", campaign_id=CAMPAIGN)
    with pytest.raises(McpError, match="`lead_id` is required"):
        _read()(op="lead")
    with pytest.raises(McpError, match="`email` is required"):
        _read()(op="lead_campaigns")
    client.assert_not_called()


# --- vue resserrée --------------------------------------------------------------------------

def test_lists_drop_heavy_keys_by_default_and_full_returns_raw(client):
    rows = {"items": [{"id": "c-1", "name": "A", "sequences": [{"steps": []}]}],
            "next_starting_after": "c-1"}
    client.return_value.list_campaigns.return_value = rows
    lean = _read()(op="campaigns")
    assert lean["items"] == [{"id": "c-1", "name": "A"}]
    assert lean["next_starting_after"] == "c-1"
    assert lean["projection"]["omitted"] == ["sequences"]
    assert _read()(op="campaigns", full=True) == rows


def test_email_list_keeps_text_and_drops_html_by_default(client):
    rows = {"items": [{"id": "e-1", "body": {"text": "Hi", "html": "<p>Hi</p>"},
                       "to_address_json": [{"address": "a@example.com"}]}]}
    client.return_value.list_emails.return_value = rows
    lean = _read()(op="emails")
    assert lean["items"] == [{"id": "e-1", "body": {"text": "Hi"}}]
    assert sorted(lean["projection"]["omitted"]) == ["body.html", "to_address_json"]
    assert rows["items"][0]["body"]["html"] == "<p>Hi</p>"  # the answer itself is untouched
    assert _read()(op="emails", full=True) == rows


# --- actes : ops -> méthodes ----------------------------------------------------------------

def test_add_leads(client):
    leads = [{"email": "jane.doe@example.com", "first_name": "Jane"}]
    _act()(op="add_leads", campaign_id=CAMPAIGN, leads=leads, skip_if_in_workspace=True)
    client.return_value.add_leads.assert_called_once_with(
        leads, campaign_id=CAMPAIGN, list_id=None, blocklist_id=None,
        skip_if_in_workspace=True, skip_if_in_campaign=None, skip_if_in_list=None)


def test_interest_status_is_a_value_or_an_explicit_reset(client):
    c = client.return_value
    _act()(op="update_interest_status", email="jane.doe@example.com", interest_value=2,
           campaign_id=CAMPAIGN)
    c.update_lead_interest_status.assert_called_once_with(
        "jane.doe@example.com", 2, campaign_id=CAMPAIGN, list_id=None)
    _act()(op="update_interest_status", email="jane.doe@example.com", reset_interest=True)
    assert c.update_lead_interest_status.call_args.args == ("jane.doe@example.com", None)
    for bad in ({}, {"interest_value": 2, "reset_interest": True}):
        with pytest.raises(McpError, match="one of the two"):
            _act()(op="update_interest_status", email="jane.doe@example.com", **bad)


def test_campaign_switches_reply_thread_webhooks(client):
    c = client.return_value
    _act()(op="activate_campaign", campaign_id=CAMPAIGN)
    c.activate_campaign.assert_called_once_with(CAMPAIGN)
    _act()(op="set_campaign_paused", campaign_id=CAMPAIGN)
    c.pause_campaign.assert_called_once_with(CAMPAIGN)
    _act()(op="reply_email", email_id=LIST, eaccount="jane@example.com", subject="Re: Hi",
           text="Thanks", cc="a@example.com")
    c.reply_to_email.assert_called_once_with(LIST, "jane@example.com", "Re: Hi", text="Thanks",
                                             html=None, cc_address_email_list="a@example.com",
                                             bcc_address_email_list=None)
    _act()(op="set_thread_read", thread_id=LIST)
    c.mark_thread_as_read.assert_called_once_with(LIST)
    _act()(op="create_webhook", url="https://hooks.example.com/i", event_type="reply_received")
    c.create_webhook.assert_called_once_with("https://hooks.example.com/i",
                                             event_type="reply_received",
                                             custom_interest_value=None, campaign=None, name=None)
    _act()(op="add_blocklist_entry", value="example.com")
    c.add_block_list_entry.assert_called_once_with("example.com")


def test_move_leads_maps_the_source_campaign(client):
    _act()(op="move_leads", campaign_id=CAMPAIGN, to_list_id=LIST, copy_leads=True)
    kwargs = client.return_value.move_leads.call_args.kwargs
    assert kwargs["campaign"] == CAMPAIGN and kwargs["to_list_id"] == LIST
    assert kwargs["copy_leads"] is True


def test_the_act_tool_takes_no_secret_header():
    assert "headers" not in _tools()["instantly_act"].parameters["properties"]


# --- refus --------------------------------------------------------------------------------

def test_a_client_refusal_is_an_invalid_params_error(client):
    client.return_value.add_leads.side_effect = ValueError("Give exactly one of `campaign_id` and `list_id`.")
    with pytest.raises(McpError, match="exactly one"):
        _act()(op="add_leads", leads=[{"email": "a@example.com"}])


def test_a_403_names_the_missing_scope(client):
    client.return_value.reply_to_email.side_effect = _upstream(403)
    with pytest.raises(McpError) as caught:
        _act()(op="reply_email", email_id=LIST, eaccount="jane@example.com", subject="Re",
               text="x")
    message = str(caught.value)
    assert "emails:create" in message and "read-only key refuses every action" in message
    client.return_value.list_leads.side_effect = _upstream(403)
    with pytest.raises(McpError, match="leads:read"):
        _read()(op="leads")


@pytest.mark.parametrize("status, needle", [(401, "revoked"), (402, "paid plan"),
                                            (404, "not found"), (429, "rate limit"),
                                            (503, "may have gone through")])
def test_refusals_are_translated(client, status, needle):
    client.return_value.get_workspace.side_effect = _upstream(status)
    with pytest.raises(McpError, match=needle):
        _read()(op="workspace")


# --- sonde ----------------------------------------------------------------------------------

def test_the_probe_reads_the_workspace(client):
    from oto_mcp.connectors import verify
    from oto_mcp.tools import instantly
    client.return_value.get_workspace.return_value = {"id": "w-1", "name": "Team"}
    instantly._verify({"key": KEY})
    client.assert_called_once_with(api_key=KEY)
    client.return_value.get_workspace.side_effect = _upstream(403)
    with pytest.raises(verify.NonAutorise, match="workspaces:read"):
        instantly._verify({"key": KEY})
    client.return_value.get_workspace.side_effect = None
    client.return_value.get_workspace.return_value = {}
    with pytest.raises(RuntimeError):
        instantly._verify({"key": KEY})
