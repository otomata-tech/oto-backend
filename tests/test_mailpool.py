"""Mailpool connector — cold-email domains, DNS, mailboxes (wired 2026-10-05).

The generic tripwires already cover the registry, the publisher, the logo, the
served prose and the join to the oto-core client. This file locks what is
SPECIFIC to this module:

- **no credential reaches the caller**, from any tool and op, even when the
  client mock returns raw payloads with nested passwords (the allowlist is a
  second layer behind the client's own stripping);
- `mailpool_domain_fix`: dry run by default with a real diff, no write when
  nothing changes, the FULL set sent against the set it was planned from (the
  client re-reads, writes, reads back and restores), DMARC published through
  the DMARC-email call; refused before any write when the read is unusable or
  the change would add an audit error; after a write, named `INTERNAL_ERROR`s
  (`dns_outcome_unknown`, `dns_not_kept`, `partially_applied`) that never claim
  nothing changed;
- the DNS audit and plan rules (`mailpool_dns`);
- Mailpool's error envelopes translated (validations, 403, credits);
- arguments go through pydantic validation (`call_tool`, not `.fn`).
"""
import asyncio
import json
import re
from unittest.mock import patch

import pytest
from mcp.types import INTERNAL_ERROR, INVALID_PARAMS

from oto_mcp import providers
from oto_mcp.tools import mailpool_dns as dns


@pytest.fixture(autouse=True)
def _fake_key(monkeypatch):
    monkeypatch.setattr("oto_mcp.access.resolve_api_key",
                        lambda provider, account=None: ("mp_k", False))
    monkeypatch.setattr("oto_mcp.tools.mailpool.time.sleep", lambda _s: None)


def _server():
    from fastmcp import FastMCP
    from oto_mcp.tools import mailpool

    m = FastMCP("t")
    mailpool.register(m)
    return m


def _call(name, args):
    res = asyncio.run(_server().call_tool(name, args))
    sc = getattr(res, "structured_content", None)
    if isinstance(sc, dict) and set(sc) == {"result"}:
        return sc["result"]
    if sc is not None:
        return sc
    return json.loads(res.content[0].text)


def _err(name, args):
    """The tool's `McpError` (fastmcp wraps it in a `ToolError`)."""
    with pytest.raises(Exception) as e:
        _call(name, args)
    cause = e.value.__cause__
    assert cause is not None and hasattr(cause, "error"), e.value
    return cause.error


def _reason(err):
    return (err.data or {}).get("reason")


SECRET = re.compile(r"pass|secret|auth_?code|api_?key|token", re.I)

OWNER = {"id": 9, "company": "Example Co", "email": "owner@example.com",
         "firstName": "Ann", "phone": "+33100000000", "country": "FR",
         "streetAddress1": "1 Example Street"}
MAILBOX = {
    "id": 5301, "email": "anna@example-outreach.test", "firstName": "Anna",
    "type": "shared", "status": "active", "password": "p1", "imapHost": "imap.example.net",
    "imapPassword": "p2", "smtpPassword": "p3", "secret": "s1", "avatar": "https://x/a.png",
    "admin": {"email": "admin@example-outreach.test", "password": "p4", "secret": "s2"},
    "domain": {"id": 1201, "domain": "example-outreach.test", "type": "shared",
               "domainOwner": OWNER},
}
DOMAIN = {"id": 1201, "domain": "example-outreach.test", "type": "google", "status": "active",
          "expireAt": "2027-01-01T00:00:00Z", "redirectUrl": "example.com",
          "domainOwner": OWNER, "nameservers": None}
RECORDS = [
    {"key": None, "type": "MX", "value": "mx.example.net", "priority": 1},
    {"key": None, "type": "TXT", "value": "v=spf1 include:_spf.example.net ~all"},
    {"key": "google._domainkey", "type": "TXT", "value": "v=DKIM1; k=rsa; p=AAA"},
    {"key": "_dmarc", "type": "TXT", "value": (
        "v=DMARC1; p=quarantine; fo=1; pct=100; rf=afrf; ri=86400; sp=quarantine; "
        "aspf=s; adkim=s; rua=mailto:old@example.com; ruf=mailto:old@example.com")},
]
SPAM = {"id": 880011, "createdAt": "2026-10-05T10:00:00Z", "state": "completed",
        "fromEmail": "anna@example-outreach.test", "mailbox": MAILBOX,
        "result": {"score": 97, "SPF": {"status": "passed"}, "DKIM": {"status": "passed"},
                   "spamAssassin": {"score": 0.9, "status": "warning"}}}


@pytest.fixture()
def client():
    with patch("oto.tools.mailpool.client.MailpoolClient") as cls:
        c = cls.return_value
        c.list_domains.return_value = {"data": [DOMAIN], "total": 1}
        c.get_domain.return_value = DOMAIN
        c.get_domain_dns.return_value = [dict(r) for r in RECORDS]
        c.list_mailboxes.return_value = {"data": [MAILBOX], "total": 1}
        c.get_mailbox.return_value = MAILBOX
        c.create_spam_check.return_value = SPAM
        c.get_spam_check.return_value = SPAM
        c.list_spam_checks.return_value = {"data": [SPAM], "total": 1}
        c.list_domain_owners.return_value = [OWNER]
        c.get_subscription_slots.return_value = {"slots": {"google": 4}}
        c.list_warmup_inboxes.return_value = {"data": [], "total": 0}
        yield c


def _keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _keys(v)


# --- registry ---------------------------------------------------------------

def test_registry_entry():
    c = providers.REGISTRY["mailpool"]
    assert c.keyed and c.secret_kind == "api_key"
    assert c.auth_modes == {"byo_user", "byo_org"}


def test_tools_served_with_descriptions():
    tools = {t.name: t for t in asyncio.run(_server().list_tools())}
    assert set(tools) == {"mailpool_domains", "mailpool_domain_fix", "mailpool_mailboxes",
                          "mailpool_spam_checks", "mailpool_account"}
    assert all(t.description for t in tools.values())


def test_probe_reads_slots(client):
    from oto_mcp.tools.mailpool import _verify
    _verify({"key": "mp_k"})
    client.probe.assert_called_once()


# --- credentials ------------------------------------------------------------

@pytest.mark.parametrize("name,args", [
    ("mailpool_mailboxes", {"op": "list"}),
    ("mailpool_mailboxes", {"op": "get", "mailbox_id": 5301}),
    ("mailpool_spam_checks", {"op": "run", "mailbox_id": 5301}),
    ("mailpool_spam_checks", {"op": "get", "spam_check_id": 880011}),
    ("mailpool_spam_checks", {"op": "list"}),
    ("mailpool_domains", {"op": "list"}),
    ("mailpool_domains", {"op": "get", "domain_id": 1201}),
    ("mailpool_domains", {"op": "dns", "domain_id": 1201}),
    ("mailpool_domains", {"op": "audit"}),
    ("mailpool_domains", {"op": "owners"}),
    ("mailpool_domains", {"op": "availability", "domain": "example.com"}),
    ("mailpool_domains", {"op": "suggestions", "domain": "example"}),
    ("mailpool_domains", {"op": "google_workspace_check", "domain": "example.com"}),
    ("mailpool_domains", {"op": "microsoft_365_check", "domain": "example.com"}),
    ("mailpool_domain_fix", {"domain_id": 1201, "spf_hard_fail": True}),
    ("mailpool_spam_checks", {"op": "delete", "spam_check_id": 880011}),
    ("mailpool_account", {"op": "slots"}),
    ("mailpool_account", {"op": "warmup"}),
    ("mailpool_account", {"op": "warmup_inbox", "warmup_id": 77}),
    ("mailpool_account", {"op": "warmup_available"}),
])
def test_no_credential_reaches_the_caller(client, name, args):
    """Every op of every tool, with every client method returning payloads full
    of nested secrets — even ops that pass the response through."""
    for method in ("list_domain_owners", "get_domain_info", "get_domain_suggestions",
                   "check_google_workspace_availability", "check_microsoft_365_availability",
                   "delete_spam_check", "get_subscription_slots", "get_warmup_inbox"):
        getattr(client, method).return_value = {"data": [MAILBOX], **MAILBOX}
    for method in ("list_warmup_inboxes", "list_available_warmup_inboxes"):
        getattr(client, method).return_value = {"data": [MAILBOX, {"mailbox": MAILBOX}]}
    out = _call(name, args)
    assert [k for k in _keys(out) if SECRET.search(str(k))] == []
    assert "p1" not in json.dumps(out) and "s2" not in json.dumps(out)


def test_mailbox_is_an_allowlist(client):
    mb = _call("mailpool_mailboxes", {"op": "get", "mailbox_id": 5301})
    assert mb["email"] == "anna@example-outreach.test"
    assert mb["imapHost"] == "imap.example.net"
    assert mb["admin"] == {"email": "admin@example-outreach.test"}
    assert mb["domain"] == {"id": 1201, "domain": "example-outreach.test", "type": "shared"}
    assert "secret" not in mb and "password" not in mb


def test_spam_check_drops_the_embedded_mailbox(client):
    out = _call("mailpool_spam_checks", {"op": "run", "mailbox_id": 5301})
    assert "mailbox" not in out
    assert out["score"] == 97
    assert out["checks"] == {"SPF": "passed", "DKIM": "passed", "spamAssassin": "warning"}


def test_spam_check_run_waits_for_the_result(client):
    client.create_spam_check.return_value = {**SPAM, "state": "pending", "result": None}
    out = _call("mailpool_spam_checks", {"op": "run", "mailbox_id": 5301})
    assert out["state"] == "completed" and "next_step" not in out
    client.get_spam_check.assert_called_with(880011)


def test_spam_check_run_stays_within_its_budget(client, monkeypatch):
    """No poll is sent past the wait; the pending check is then returned with
    how to read it later."""
    now = [0.0]
    monkeypatch.setattr("oto_mcp.tools.mailpool.time.monotonic", lambda: now[0])
    monkeypatch.setattr("oto_mcp.tools.mailpool.time.sleep",
                        lambda s: now.__setitem__(0, now[0] + s))
    pending = {**SPAM, "state": "pending", "result": None}
    client.create_spam_check.return_value = pending
    client.get_spam_check.return_value = pending
    out = _call("mailpool_spam_checks", {"op": "run", "mailbox_id": 5301})
    assert out["state"] == "pending" and "op=\"get\"" in out["next_step"]
    from oto_mcp.tools.mailpool import SPAM_CHECK_WAIT_S
    assert now[0] <= SPAM_CHECK_WAIT_S and client.get_spam_check.call_count >= 1


def test_spam_check_failed_poll_returns_the_pending_check(client):
    import requests
    client.create_spam_check.return_value = {**SPAM, "state": "pending", "result": None}
    client.get_spam_check.side_effect = requests.ReadTimeout("slow")
    out = _call("mailpool_spam_checks", {"op": "run", "mailbox_id": 5301})
    assert out["id"] == 880011 and out["state"] == "pending" and "next_step" in out


# --- domain fix -------------------------------------------------------------

def test_fix_is_a_dry_run_by_default(client):
    out = _call("mailpool_domain_fix", {"domain_id": 1201, "spf_hard_fail": True,
                                        "dmarc_report_email": "dmarc@example.com"})
    assert out["dry_run"] is True and out["changed"] is True
    assert "TXT @ v=spf1 include:_spf.example.net -all" in out["added"]
    assert any("rua=mailto:dmarc@example.com; ruf=mailto:dmarc@example.com" in a
               and "fo=1" not in a for a in out["added"])
    client.update_domain_dns.assert_not_called()
    client.set_dmarc_email.assert_not_called()


def test_fix_applies_the_full_set_reads_back_and_publishes_dmarc(client):
    sent = {}

    def put(domain_id, records, **_kw):
        sent["records"] = records
        return records

    client.update_domain_dns.side_effect = put
    out = _call("mailpool_domain_fix", {
        "domain_id": 1201, "dmarc_report_email": "dmarc@example.com",
        "tracking_host": "track", "tracking_target": "custom.lemlist.com", "dry_run": False})
    assert out["applied"] == ["dns", "dmarc_published"]
    assert len(sent["records"]) == len(RECORDS) + 1
    assert {"type": "CNAME", "key": "track", "value": "custom.lemlist.com"} in sent["records"]
    assert any(r["type"] == "MX" for r in sent["records"])
    client.set_dmarc_email.assert_called_once_with(1201, "dmarc@example.com")


def test_fix_sends_nothing_when_already_matching(client):
    client.get_domain_dns.return_value = [
        {**r, "value": r["value"].replace("~all", "-all")} for r in RECORDS]
    out = _call("mailpool_domain_fix", {"domain_id": 1201, "spf_hard_fail": True,
                                        "dry_run": False})
    assert out["changed"] is False
    client.update_domain_dns.assert_not_called()


def test_fix_sends_the_set_it_planned_from(client):
    """The client refuses the write if the domain changed since this read."""
    _applying_put(client)
    _call("mailpool_domain_fix", {"domain_id": 1201, "spf_hard_fail": True, "dry_run": False})
    assert client.update_domain_dns.call_args.kwargs == {"expected": RECORDS}


def _not_kept(restored, restore_error=None):
    from oto.tools.mailpool.client import MailpoolDnsWriteError
    return MailpoolDnsWriteError(1201, [RECORDS[1]], RECORDS, restored, restore_error)


def test_fix_says_the_previous_set_was_restored(client):
    client.update_domain_dns.side_effect = _not_kept(True)
    err = _err("mailpool_domain_fix", {"domain_id": 1201, "spf_hard_fail": True,
                                       "dry_run": False})
    assert (err.code, _reason(err), err.data["restored"]) == (INTERNAL_ERROR, "dns_not_kept", True)
    assert err.data["missing"] == ["TXT @ v=spf1 include:_spf.example.net ~all"]
    client.set_dmarc_email.assert_not_called()


def test_fix_says_when_the_restore_failed(client):
    import requests
    client.update_domain_dns.side_effect = _not_kept(False, requests.ReadTimeout("slow"))
    err = _err("mailpool_domain_fix", {"domain_id": 1201, "spf_hard_fail": True,
                                       "dry_run": False})
    assert (err.code, _reason(err), err.data["restored"]) == (INTERNAL_ERROR, "dns_not_kept", False)
    assert "Nothing was changed" not in err.message and "did not answer" in err.message


def test_fix_reports_records_it_did_not_send(client):
    extra = {"key": "www", "type": "A", "value": "192.0.2.1"}
    client.update_domain_dns.side_effect = lambda d, records, **kw: records + [extra]
    out = _call("mailpool_domain_fix", {"domain_id": 1201, "spf_hard_fail": True,
                                        "dry_run": False})
    assert out["applied"] == ["dns"]
    assert out["unexpected"] == ["A www 192.0.2.1"]


# --- domain fix: refused before any write -----------------------------------

@pytest.mark.parametrize("read", [[], {}, "", [None], [{"value": "x"}]])
@pytest.mark.parametrize("dry_run", [True, False])
def test_fix_refuses_to_plan_from_an_unusable_read(client, read, dry_run):
    """A write replaces the whole set: planning from an empty read would wipe it."""
    client.get_domain_dns.return_value = read
    err = _err("mailpool_domain_fix", {"domain_id": 1201, "tracking_host": "track",
                                       "tracking_target": "custom.lemlist.com",
                                       "dry_run": dry_run})
    assert (err.code, _reason(err)) == (INVALID_PARAMS, "dns_unreadable")
    client.update_domain_dns.assert_not_called()


@pytest.mark.parametrize("args", [
    {"add_records": [{"type": "TXT", "key": None, "value": "v=spf1 include:x.test ~all"}]},
    {"add_records": [{"type": "CNAME", "key": None, "value": "elsewhere.test"}]},
    {"add_records": [{"type": "TXT", "key": "_dmarc", "value": "v=DMARC1; p=none"}]},
    {"remove_records": [RECORDS[0]]},
    {"remove_records": [RECORDS[2]]},
])
def test_fix_refuses_a_change_that_breaks_the_domain(client, args):
    for dry_run in (True, False):
        err = _err("mailpool_domain_fix", {"domain_id": 1201, "dry_run": dry_run, **args})
        assert (err.code, _reason(err)) == (INVALID_PARAMS, "dns_would_break")
    client.update_domain_dns.assert_not_called()


def test_fix_may_repair_an_existing_error(client):
    """Two SPF records are already an error: removing one is allowed."""
    second = {"key": None, "type": "TXT", "value": "v=spf1 include:old.test ~all"}
    client.get_domain_dns.return_value = RECORDS + [second]
    out = _call("mailpool_domain_fix", {"domain_id": 1201, "remove_records": [second]})
    assert out["removed"] == ["TXT @ v=spf1 include:old.test ~all"]


@pytest.mark.parametrize("email", ["dmarc@corp", "x@y.test; p=none", "a@b.test,c@d.test",
                                   "a b@c.test", "dmarc@example"])
def test_fix_refuses_a_loose_dmarc_address_before_writing(client, email):
    err = _err("mailpool_domain_fix", {"domain_id": 1201, "spf_hard_fail": True,
                                       "dmarc_report_email": email, "dry_run": False})
    assert (err.code, _reason(err)) == (INVALID_PARAMS, "invalid_argument")
    client.update_domain_dns.assert_not_called()


# --- domain fix: after a write, never "nothing changed" ---------------------

def _applying_put(client):
    client.update_domain_dns.side_effect = lambda d, records, **kw: records


def test_fix_lists_what_was_applied_when_dmarc_publishing_fails(client):
    from oto.tools.common.errors import UpstreamHTTPError
    _applying_put(client)
    client.set_dmarc_email.side_effect = UpstreamHTTPError(400, {"message": "bad"}, service="mailpool")
    err = _err("mailpool_domain_fix", {"domain_id": 1201, "spf_hard_fail": True,
                                       "dmarc_report_email": "dmarc@example.com",
                                       "dry_run": False})
    assert (err.code, _reason(err)) == (INTERNAL_ERROR, "partially_applied")
    assert err.data["applied"] == ["dns"] and err.data["not_done"] == ["dmarc_published"]


@pytest.mark.parametrize("exc", ["timeout", "502"])
def test_fix_write_without_answer_is_an_unknown_outcome(client, exc):
    import requests
    from oto.tools.common.errors import UpstreamHTTPError
    client.update_domain_dns.side_effect = (
        requests.ReadTimeout("slow") if exc == "timeout"
        else UpstreamHTTPError(502, "", service="mailpool"))
    err = _err("mailpool_domain_fix", {"domain_id": 1201, "spf_hard_fail": True,
                                       "dry_run": False})
    assert (err.code, _reason(err)) == (INTERNAL_ERROR, "dns_outcome_unknown")
    assert "may or may not have changed" in err.message


def test_fix_refused_write_is_a_plain_refusal(client):
    from oto.tools.common.errors import UpstreamHTTPError
    client.update_domain_dns.side_effect = UpstreamHTTPError(
        400, {"message": "invalid record"}, service="mailpool")
    err = _err("mailpool_domain_fix", {"domain_id": 1201, "spf_hard_fail": True,
                                       "dry_run": False})
    assert (err.code, _reason(err)) == (INVALID_PARAMS, "upstream_refused")


def test_fix_arguments_are_validated(client):
    for args in ({"domain_id": "abc"},
                 {"domain_id": 1201, "dmarc_policy": "reject"},        # not served
                 {"domain_id": 1201, "redirect_url": "example.com"},   # not served
                 {"domain_id": 1201, "set_records": []},               # renamed
                 {"domain_id": 1201, "add_records": [{"type": "SPF", "value": "x"}]},
                 {"domain_id": 1201, "add_records": [{"type": "TXT", "value": ""}]},
                 {"domain_id": 1201, "add_records": [{"type": "TXT", "value": "x", "ttl": 3}]}):
        with pytest.raises(Exception):
            _call("mailpool_domain_fix", args)
    err = _err("mailpool_domain_fix", {"domain_id": 1201, "tracking_host": "track"})
    assert (err.code, _reason(err)) == (INVALID_PARAMS, "invalid_argument")
    client.update_domain_dns.assert_not_called()


# --- dns rules --------------------------------------------------------------

def test_audit_findings():
    a = dns.audit("example-outreach.test", RECORDS)
    checks = {(f["level"], f["check"]) for f in a["findings"]}
    assert ("warning", "spf") in checks
    assert ("info", "tracking") in checks
    assert ("info", "dmarc") in checks          # rua on another domain
    assert not any("ruf" in f["message"] for f in a["findings"])  # Mailpool always sets it
    assert a["ok"] is True
    assert a["findings"][0]["level"] == "warning"


def test_audit_errors_and_tracking():
    a = dns.audit("example-outreach.test", [
        {"key": "track", "type": "CNAME", "value": "custom.lemlist.com"}])
    assert a["ok"] is False
    assert {f["check"] for f in a["findings"] if f["level"] == "error"} == {
        "mx", "spf", "dkim", "dmarc"}
    assert a["tracking"] == [{"host": "track", "target": "custom.lemlist.com", "tool": "lemlist"}]


def test_plan_converges_on_what_mailpool_publishes():
    """Mailpool republishes `_dmarc` with ruf = rua: a second plan on the
    published state must find nothing to change."""
    first = dns.plan(RECORDS, dmarc_report_email="dmarc@example.com", spf_hard_fail=True)
    again = dns.plan(first["records"], dmarc_report_email="dmarc@example.com", spf_hard_fail=True)
    assert again["changed"] is False


def test_plan_never_drops_unrelated_records():
    p = dns.plan(RECORDS, add_records=[{"type": "TXT", "key": None, "value": "verify=1"}])
    assert len(p["records"]) == len(RECORDS) + 1
    assert p["removed"] == [] and p["publish_dmarc_email"] is None


def test_plan_remove_needs_an_exact_match():
    with pytest.raises(ValueError):
        dns.plan(RECORDS, remove_records=[{"type": "TXT", "key": None, "value": "nope"}])


def test_plan_refuses_a_cname_next_to_other_records():
    records = RECORDS + [{"key": "track", "type": "TXT", "value": "verify=1"}]
    with pytest.raises(dns.PlanRefused, match="non-CNAME"):
        dns.plan(records, tracking_host="track", tracking_target="custom.lemlist.com")


def test_plan_says_it_creates_a_dmarc_policy():
    p = dns.plan(RECORDS[:3], dmarc_report_email="dmarc@example.com")
    assert any("p=quarantine; sp=quarantine; pct=100" in n for n in p["notes"])


def test_plan_keeps_srv_fields_and_tells_srv_records_apart():
    srv = {"key": "_sip._tls", "type": "SRV", "value": "sip.example.test", "priority": 10,
           "port": 443, "weight": 5}
    p = dns.plan(RECORDS + [srv], spf_hard_fail=True)
    assert srv in p["records"]
    other = {**srv, "port": 5061}
    assert not dns.plan(RECORDS + [srv], add_records=[other])["removed"]
    assert len(dns.plan(RECORDS + [srv], add_records=[other])["records"]) == len(RECORDS) + 2


def test_audit_flags_duplicates_and_shared_cnames():
    a = dns.audit("example-outreach.test", RECORDS + [
        {"key": "_dmarc", "type": "TXT", "value": "v=DMARC1; p=none"},
        {"key": None, "type": "CNAME", "value": "elsewhere.test"}])
    errors = {f["check"] for f in a["findings"] if f["level"] == "error"}
    assert {"dmarc", "cname"} <= errors


# --- errors and arguments ---------------------------------------------------

def test_timeout_is_a_clean_tool_error(client):
    import requests
    client.get_domain_info.side_effect = requests.ReadTimeout("slow")
    err = _err("mailpool_domains", {"op": "availability", "domain": "example.com"})
    assert (err.code, _reason(err)) == (INVALID_PARAMS, "timeout")


def test_owners_are_company_and_country_only(client):
    out = _call("mailpool_domains", {"op": "owners"})
    assert out == [{"id": 9, "company": "Example Co", "country": "FR"}]


def test_warmup_inbox_is_an_allowlist(client):
    client.get_warmup_inbox.return_value = {"id": 77, "email": "a@example-outreach.test",
                                            "dailyTarget": 20, "newField": "x"}
    out = _call("mailpool_account", {"op": "warmup_inbox", "warmup_id": 77})
    assert out == {"id": 77, "email": "a@example-outreach.test", "dailyTarget": 20}


def test_audit_page_says_what_is_left(client):
    client.list_domains.return_value = {"data": [DOMAIN], "total": 45}
    out = _call("mailpool_domains", {"op": "audit", "limit": 100, "offset": 20})
    assert client.list_domains.call_args.kwargs == {"limit": 20, "offset": 20}
    assert (out["count"], out["total"], out["next_offset"]) == (1, 45, 21)


def test_one_client_per_tool_call(client):
    from oto.tools.mailpool import client as client_module
    client.list_domains.return_value = {"data": [DOMAIN] * 3, "total": 3}
    _call("mailpool_domains", {"op": "audit"})
    assert client_module.MailpoolClient.call_count == 1


def test_lists_count_what_they_return(client):
    client.list_spam_checks.return_value = {"data": [SPAM], "total": 4}
    out = _call("mailpool_spam_checks", {"op": "list"})
    assert out["count"] == 1 and "total" not in out

def _upstream(status, body):
    from oto.tools.common.errors import UpstreamHTTPError
    return UpstreamHTTPError(status, body, service="mailpool")


def test_validation_envelope_names_the_field(client):
    client.get_domain_info.side_effect = _upstream(400, {"validations": [
        {"context": "domain", "constraints": {"isFqdn": "domain must be a valid domain"}}]})
    with pytest.raises(Exception, match="`domain`: domain must be a valid domain"):
        _call("mailpool_domains", {"op": "availability", "domain": "example.com"})


def test_feature_not_enabled_is_explained(client):
    client.check_google_workspace_availability.side_effect = _upstream(
        403, {"message": "Google Workspace domain-in-use check is not enabled for this workspace"})
    with pytest.raises(Exception, match="enabled per workspace"):
        _call("mailpool_domains", {"op": "google_workspace_check", "domain": "example.com"})


def test_ops_refuse_foreign_arguments(client):
    with pytest.raises(Exception, match="does not use `domain_id`"):
        _call("mailpool_domains", {"op": "list", "domain_id": 1201})
    with pytest.raises(Exception, match="does not use `mailbox_id`"):
        _call("mailpool_spam_checks", {"op": "get", "spam_check_id": 1, "mailbox_id": 2})
    with pytest.raises(Exception, match="`mailbox_id` is required"):
        _call("mailpool_mailboxes", {"op": "get"})


# --- mailbox creation ---------------------------------------------------------

NEW = {"email": "anna@example-outreach.test", "firstName": "Anna", "lastName": "Lee",
       "type": "google", "forwardTo": "anna@example.com"}
PLAN = {"mailboxes": [NEW], "slots": {"google": {"free": 3, "used_by_this": 1, "left_after": 2}},
        "domains": {"example-outreach.test": {"id": 1201, "type": "unassigned",
                                              "becomes": "google"}}}


def test_create_is_a_dry_run_by_default(client):
    client.plan_mailboxes.return_value = PLAN
    out = _call("mailpool_mailboxes", {"op": "create", "mailboxes": [NEW]})
    assert out["dry_run"] is True and out["slots"] == PLAN["slots"]
    client.plan_mailboxes.assert_called_once_with([NEW])
    client.create_mailboxes.assert_not_called()


def test_create_applies_and_serves_no_credential(client):
    client.create_mailboxes.return_value = {"created": [MAILBOX], "plan": PLAN}
    out = _call("mailpool_mailboxes", {"op": "create", "mailboxes": [NEW], "dry_run": False})
    client.create_mailboxes.assert_called_once_with([NEW])
    assert out["created"][0]["email"] == MAILBOX["email"]
    assert not [k for k in _keys(out) if SECRET.search(k)]
    assert "domainOwner" not in out["created"][0]["domain"]


@pytest.mark.parametrize("dry_run", [True, False])
def test_create_refusal_is_named(client, dry_run):
    from oto.tools.mailpool import MailpoolCreateRefused
    err = MailpoolCreateRefused(["2 google mailbox(es) asked, 1 google slot(s) free"])
    client.plan_mailboxes.side_effect = err
    client.create_mailboxes.side_effect = err
    e = _err("mailpool_mailboxes", {"op": "create", "mailboxes": [NEW], "dry_run": dry_run})
    assert _reason(e) == "create_refused" and "slot" in e.message


@pytest.mark.parametrize("exc", ["timeout", "5xx"])
def test_create_without_answer_is_an_unknown_outcome(client, exc):
    import requests
    from oto.tools.common.errors import UpstreamHTTPError
    client.create_mailboxes.side_effect = (
        requests.Timeout() if exc == "timeout"
        else UpstreamHTTPError(503, "down", service="mailpool"))
    e = _err("mailpool_mailboxes", {"op": "create", "mailboxes": [NEW], "dry_run": False})
    assert _reason(e) == "mailbox_outcome_unknown"


@pytest.mark.parametrize("args", [
    {"op": "create"},
    {"op": "create", "mailboxes": [NEW], "mailbox_id": 1},
    {"op": "list", "mailboxes": [NEW]},
    {"op": "create", "mailboxes": [dict(NEW, type="private")]},
    {"op": "create", "mailboxes": [dict(NEW, password="x")]},
])
def test_create_arguments_are_checked(client, args):
    with pytest.raises(Exception):
        _call("mailpool_mailboxes", args)
    client.create_mailboxes.assert_not_called()


def test_create_preview_errors_are_tool_errors(client):
    import requests
    client.plan_mailboxes.side_effect = requests.Timeout()
    assert _reason(_err("mailpool_mailboxes",
                        {"op": "create", "mailboxes": [NEW]})) == "timeout"
    client.plan_mailboxes.side_effect = ValueError("`mailboxes` is empty.")
    assert _reason(_err("mailpool_mailboxes",
                        {"op": "create", "mailboxes": []})) == "invalid_argument"


def test_create_partial_lists_what_exists(client):
    from oto.tools.mailpool import MailpoolCreatePartial
    client.create_mailboxes.side_effect = MailpoolCreatePartial(
        [MAILBOX], ["example-outreach.test"], ["other-outreach.test"], RuntimeError("down"))
    e = _err("mailpool_mailboxes", {"op": "create", "mailboxes": [NEW], "dry_run": False})
    assert _reason(e) == "partially_applied"
    assert e.data["done"] == ["example-outreach.test"]
    assert e.data["created"][0]["email"] == MAILBOX["email"]
    assert not [k for k in _keys(e.data) if SECRET.search(k)]


# --- exports --------------------------------------------------------------------

EXPORT_PLAN = {"platform": "lemlist", "mailboxes": [{"id": 5301, "email": MAILBOX["email"],
                                                     "type": "shared"}]}
BATCH = {"id": 42, "status": "in_progress", "platform": "lemlist",
         "exports": [{"id": 1, "mailboxId": 5301, "status": "in_progress"}], "errors": []}


def test_export_is_a_dry_run_by_default(client):
    client.plan_export.return_value = EXPORT_PLAN
    out = _call("mailpool_mailboxes", {"op": "export", "platform": "lemlist",
                                       "mailbox_ids": [5301]})
    assert out["dry_run"] is True and out["mailboxes"] == EXPORT_PLAN["mailboxes"]
    client.export_mailboxes.assert_not_called()


def test_export_applies_and_returns_the_batch(client):
    client.plan_export.return_value = EXPORT_PLAN
    client.export_mailboxes.return_value = BATCH
    out = _call("mailpool_mailboxes", {"op": "export", "platform": "lemlist",
                                       "mailbox_ids": [5301], "dry_run": False})
    client.export_mailboxes.assert_called_once_with("lemlist", [5301])
    assert out["batch"]["id"] == 42


def test_export_refusal_and_unknown_outcome(client):
    import requests
    from oto.tools.mailpool import MailpoolExportRefused
    client.plan_export.side_effect = MailpoolExportRefused(["no lemlist login is saved"])
    e = _err("mailpool_mailboxes", {"op": "export", "platform": "lemlist", "mailbox_ids": [1]})
    assert _reason(e) == "export_refused"
    client.plan_export.side_effect = None
    client.plan_export.return_value = EXPORT_PLAN
    client.export_mailboxes.side_effect = requests.Timeout()
    e = _err("mailpool_mailboxes", {"op": "export", "platform": "lemlist",
                                    "mailbox_ids": [5301], "dry_run": False})
    assert _reason(e) == "export_outcome_unknown"


def test_export_reads(client):
    client.export_credentials_status.return_value = {"platform": "lemlist", "saved": False,
                                                     "saved_at": None}
    client.list_export_batches.return_value = {"data": [BATCH], "total": 1}
    client.get_export_batch.return_value = BATCH
    assert _call("mailpool_mailboxes", {"op": "export_login",
                                        "platform": "lemlist"})["saved"] is False
    assert _call("mailpool_mailboxes", {"op": "export_batches"})["total"] == 1
    assert _call("mailpool_mailboxes", {"op": "export_batch", "batch_id": 42})["id"] == 42


@pytest.mark.parametrize("args", [
    {"op": "export", "platform": "lemlist"},
    {"op": "export", "mailbox_ids": [1]},
    {"op": "export", "platform": "gmail", "mailbox_ids": [1]},
    {"op": "export_batch"},
    {"op": "list", "platform": "lemlist"},
    {"op": "create", "mailboxes": [NEW], "mailbox_ids": [1]},
])
def test_export_arguments_are_checked(client, args):
    with pytest.raises(Exception):
        _call("mailpool_mailboxes", args)
    client.export_mailboxes.assert_not_called()
