"""Connecteur HeyReach — prospection LinkedIn automatisée, servi en PAIRE.

Les tripwires génériques couvrent déjà le registre, l'éditeur, le logo, la prose
servie, la convention `<ns>`/`<ns>_act`, les effets déclarés et la jointure au client
oto-core. Ce fichier verrouille ce qui est PROPRE à ce module :

- deux outils, `heyreach` (lecture) et `heyreach_act` (acte), avec leurs ops ;
- chaque op appelle la BONNE méthode du client, avec les arguments de l'agent traduits
  (`sender_id`, `date_from`/`date_to`, `profile_url`/`linkedin_id`), et un `limit` par
  défaut sur les listes à offset ;
- la vue par défaut allège les lignes (fil d'une conversation, prose d'un lead, limites
  d'un expéditeur, réglages d'exclusion), nommé sous `projection` ; `full=True` rend le brut ;
- `add_campaign_leads` bâtit les paires expéditeur/lead ;
- la traduction des erreurs HeyReach (401 clé, 404 selon l'objet, 429) ;
- la sonde de connexion sur `check_api_key`.

Ne tournent qu'après installation d'un oto-core qui porte `oto.tools.heyreach`.
"""
import asyncio
from unittest.mock import patch

import pytest

from oto_mcp import providers
from oto_mcp.mcp_errors import McpError


@pytest.fixture(autouse=True)
def _fake_key(monkeypatch):
    monkeypatch.setattr("oto_mcp.access.resolve_api_key",
                        lambda provider, account=None: ("hr_k", False))


def _tools():
    from fastmcp import FastMCP
    from oto_mcp.tools import heyreach

    m = FastMCP("t")
    heyreach.register(m)
    return {t.name: t for t in asyncio.run(m._list_tools())}


def _fn(name):
    return _tools()[name].fn


@pytest.fixture()
def client():
    with patch("oto.tools.heyreach.HeyReachClient") as cls:
        yield cls.return_value


def _upstream(status, body=None):
    from oto.tools.common.errors import UpstreamHTTPError
    return UpstreamHTTPError(status, body if body is not None else {"errorMessage": "nope"},
                             service="heyreach")


# --- registre et surface -------------------------------------------------------

def test_registry_entry():
    c = providers.REGISTRY["heyreach"]
    assert c.keyed and c.secret_kind == "api_key"
    assert c.auth_modes == {"byo_user", "byo_org"}
    assert c.category == "Prospecting"


def test_the_pair_is_served_with_descriptions_and_annotations():
    tools = _tools()
    assert set(tools) == {"heyreach", "heyreach_act"}
    assert all(t.description for t in tools.values())
    assert tools["heyreach"].annotations.readOnlyHint is True
    assert tools["heyreach_act"].annotations.readOnlyHint is False


# --- lecture ---------------------------------------------------------------------

def test_offset_lists_get_a_default_limit(client):
    client.list_campaigns.return_value = {"totalCount": 0, "items": []}
    _fn("heyreach")(op="campaigns", statuses=["IN_PROGRESS"], sender_ids=[3001])
    client.list_campaigns.assert_called_once_with(
        keyword=None, statuses=["IN_PROGRESS"], account_ids=[3001], limit=50, offset=None)


def test_list_leads_maps_the_lead_and_the_period(client):
    client.list_list_leads.return_value = {"items": []}
    _fn("heyreach")(op="list_leads", list_id=2001, limit=1000, linkedin_id="58344979",
                    date_from="2026-11-02T00:00:00Z")
    client.list_list_leads.assert_called_once_with(
        2001, keyword=None, lead_profile_url=None, lead_linkedin_id="58344979",
        created_from="2026-11-02T00:00:00Z", created_to=None, limit=1000, offset=None)


def test_stats_send_empty_lists_for_all(client):
    client.get_overall_stats.return_value = {"overallStats": {}}
    _fn("heyreach")(op="stats", date_from="2026-10-01T00:00:00Z", date_to="2026-10-31T23:59:59Z")
    client.get_overall_stats.assert_called_once_with(
        "2026-10-01T00:00:00Z", "2026-10-31T23:59:59Z", account_ids=[], campaign_ids=[])


def test_stats_require_the_period(client):
    with pytest.raises(McpError, match="date_from"):
        _fn("heyreach")(op="stats", date_to="2026-10-31T23:59:59Z")
    client.get_overall_stats.assert_not_called()


def test_conversations_drop_the_thread_by_default_and_full_keeps_it(client):
    raw = {"totalCount": 1, "nextCursor": "C2", "hasNextPage": True, "items": [{
        "id": "c1", "linkedInAccountId": 3001, "lastMessageText": "Sounds good",
        "messages": [{"body": "…"}], "linkedInAccount": {"id": 3001},
        "correspondentProfile": {"firstName": "Jane", "about": "long prose"}}]}
    client.list_conversations.return_value = raw
    out = _fn("heyreach")(op="conversations", seen=False, sender_ids=[3001], cursor="C1")
    client.list_conversations.assert_called_once_with(
        None, "C1", last_message_from=None, last_message_to=None, linkedin_account_ids=[3001],
        campaign_ids=None, search=None, lead_linkedin_id=None, lead_profile_url=None, tags=None,
        latest_auto_tag_names=None, seen=False)
    row = out["items"][0]
    assert "messages" not in row and "linkedInAccount" not in row
    assert row["correspondentProfile"] == {"firstName": "Jane"}
    assert (out["nextCursor"], out["hasNextPage"]) == ("C2", True)
    assert out["projection"]["omitted"] == ["correspondentProfile.about", "linkedInAccount",
                                            "messages"]
    assert _fn("heyreach")(op="conversations", full=True) == raw


def test_a_thread_is_read_by_sender_and_conversation(client):
    client.get_conversation.return_value = {"id": "c1", "messages": []}
    out = _fn("heyreach")(op="conversation", sender_id=3001, conversation_id="c1")
    assert out == {"id": "c1", "messages": []}
    client.get_conversation.assert_called_once_with(3001, "c1")


def test_senders_drop_their_daily_limits(client):
    client.list_linkedin_accounts.return_value = {"items": [{"id": 1, "accountLimits": {}}]}
    out = _fn("heyreach")(op="senders")
    assert out["items"] == [{"id": 1}]


def test_check_key_answers_ok(client):
    client.check_api_key.return_value = {}
    assert _fn("heyreach")(op="check_key") == {"ok": True}


# --- acte ------------------------------------------------------------------------

def test_add_campaign_leads_builds_the_sender_lead_pairs(client):
    client.add_leads_to_campaign.return_value = {"addedLeadsCount": 2}
    _fn("heyreach_act")(op="add_campaign_leads", campaign_id=1001, sender_id=3001,
                        resume_paused=True, leads=[
                            {"profileUrl": "https://www.linkedin.com/in/a/"},
                            {"profileUrl": "https://www.linkedin.com/in/b/", "linkedInAccountId": 3002}])
    client.add_leads_to_campaign.assert_called_once_with(
        1001, [{"lead": {"profileUrl": "https://www.linkedin.com/in/a/"}, "linkedInAccountId": 3001},
               {"lead": {"profileUrl": "https://www.linkedin.com/in/b/"}, "linkedInAccountId": 3002}],
        resume_paused_campaign=True, resume_finished_campaign=None)


def test_without_a_sender_the_campaign_distributes(client):
    _fn("heyreach_act")(op="add_campaign_leads", campaign_id=1001,
                        leads=[{"profileUrl": "https://www.linkedin.com/in/a/"}])
    args = client.add_leads_to_campaign.call_args.args
    assert args[1] == [{"lead": {"profileUrl": "https://www.linkedin.com/in/a/"}}]


def test_send_message_needs_its_three_arguments(client):
    with pytest.raises(McpError, match="message"):
        _fn("heyreach_act")(op="send_message", sender_id=3001, conversation_id="c1")
    client.send_message.assert_not_called()
    _fn("heyreach_act")(op="send_message", sender_id=3001, conversation_id="c1", message="Hi")
    client.send_message.assert_called_once_with(3001, "c1", "Hi", subject=None)


def test_stop_a_lead_by_profile_or_member_id(client):
    _fn("heyreach_act")(op="set_campaign_lead_stopped", campaign_id=1001, linkedin_id="58344979")
    client.stop_lead_in_campaign.assert_called_once_with(
        1001, lead_member_id="58344979", lead_url=None)


def test_pause_and_resume(client):
    _fn("heyreach_act")(op="set_campaign_paused", campaign_id=1001)
    _fn("heyreach_act")(op="set_campaign_resumed", campaign_id=1001)
    client.pause_campaign.assert_called_once_with(1001)
    client.resume_campaign.assert_called_once_with(1001)


def test_seen_must_be_said(client):
    with pytest.raises(McpError, match="seen"):
        _fn("heyreach_act")(op="set_conversation_seen", sender_id=3001, conversation_id="c1")
    _fn("heyreach_act")(op="set_conversation_seen", sender_id=3001, conversation_id="c1",
                        seen=False)
    client.set_seen_status.assert_called_once_with(3001, "c1", False)


def test_tags_add_or_replace(client):
    _fn("heyreach_act")(op="add_lead_tags", profile_url="https://www.linkedin.com/in/a/",
                        tags=["hot"], create_missing_tags=True)
    _fn("heyreach_act")(op="set_lead_tags", linkedin_id="1", tags=["nurture"])
    client.add_lead_tags.assert_called_once_with(
        ["hot"], lead_profile_url="https://www.linkedin.com/in/a/", lead_linkedin_id=None,
        create_tag_if_not_existing=True)
    client.replace_lead_tags.assert_called_once_with(
        ["nurture"], lead_profile_url=None, lead_linkedin_id="1", create_tag_if_not_existing=None)


def test_create_webhook_listens_to_every_campaign_by_default(client):
    _fn("heyreach_act")(op="create_webhook", name="Replies", webhook_url="https://h.example.com/x",
                        event_type="EVERY_MESSAGE_REPLY_RECEIVED")
    client.create_webhook.assert_called_once_with(
        "Replies", "https://h.example.com/x", "EVERY_MESSAGE_REPLY_RECEIVED", campaign_ids=[])


def test_lists_and_webhook_deletion(client):
    _fn("heyreach_act")(op="create_list", name="Attendees")
    _fn("heyreach_act")(op="add_list_leads", list_id=2001, leads=[{"profileUrl": "u"}])
    _fn("heyreach_act")(op="delete_webhook", webhook_id=4001)
    client.create_empty_list.assert_called_once_with("Attendees", None)
    client.add_leads_to_list.assert_called_once_with(2001, [{"profileUrl": "u"}])
    client.delete_webhook.assert_called_once_with(4001)


# --- erreurs et sonde --------------------------------------------------------------

@pytest.mark.parametrize("status, needle", [
    (401, "refused the API key"), (404, "no campaign with this campaign_id"),
    (429, "300 requests per minute"), (400, "nope"), (500, "read it back")])
def test_upstream_errors_are_translated(client, status, needle):
    client.get_campaign.side_effect = _upstream(status)
    with pytest.raises(McpError, match=needle):
        _fn("heyreach")(op="campaign", campaign_id=1)


def test_a_local_refusal_says_nothing_was_done(client):
    client.list_campaigns.side_effect = ValueError("`limit` must be between 1 and 100; got 0.")
    with pytest.raises(McpError, match="Nothing was done"):
        _fn("heyreach")(op="campaigns", limit=0)


def test_verify_probes_the_key():
    from oto_mcp.connectors import verify as connector_verify
    from oto_mcp.tools import heyreach

    with patch("oto.tools.heyreach.HeyReachClient") as cls:
        cls.return_value.check_api_key.return_value = {}
        heyreach._verify({"key": "k"})
        cls.assert_called_once_with(api_key="k")
        cls.return_value.check_api_key.side_effect = _upstream(401)
        with pytest.raises(connector_verify.NonAutorise):
            heyreach._verify({"key": "k"})
