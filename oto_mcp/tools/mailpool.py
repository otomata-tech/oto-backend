"""Mailpool tools — cold-email infrastructure: domains, DNS, mailboxes, spam
checks, warmup.

Wraps `oto.tools.mailpool.client.MailpoolClient` (API v1, `X-Api-Authorization`).
Five tools, one per business object:

- `mailpool_domains` — list, read, DNS records, deliverability audit,
  availability and suggestions for a new name, saved owners;
- `mailpool_domain_fix` — the only tool that edits DNS: SPF hard fail, DMARC
  report address, tracking CNAME, single records. `dry_run=True` by default,
  with a real diff;
- `mailpool_mailboxes` — list, read, create (on slots already paid for;
  `dry_run=True` by default), export to a sending platform (Mailpool carries
  the passwords; `dry_run=True` by default) and its batches;
- `mailpool_spam_checks` — run (free), read, list, delete;
- `mailpool_account` — subscription slots, warmup.

⚠️ **No credential ever reaches the caller.** Three layers: the client strips
every credential key, `_run` scrubs every result again, and mailboxes are
served through the client's ALLOWLIST (`project_mailbox`, its domain reduced to
id/name/type), spam checks without their embedded mailbox, warmup inboxes and
registrant owners through allowlists of their own (an owner's name, address and
phone are not served) — a field Mailpool adds tomorrow is not served until it
is listed.

⚠️ **Nothing billed is BOUGHT**: registering, transferring or renewing a domain,
buying slots, enrolling in warmup, inbox placement are not served. Creating a
mailbox is, because it only takes a slot the workspace already pays for: the
client (`plan_mailboxes`) reads the slots, domains and existing addresses and
refuses before sending anything when a slot is missing, a domain is not active
or is of another type, or an address exists. A create that gets no answer
(`mailbox_outcome_unknown`) may have landed: list the mailboxes before retrying.

DNS behaviour (observed live on 2026-10-05, encoded in `mailpool_dns`): a DNS
write REPLACES the whole record set — the fix tool reads, plans, and hands the
full set to `update_domain_dns(…, expected=<the read>)`, which refuses it if the
set changed since, refuses to strip a host of its MX/SPF/DKIM, reads back and
restores the previous set if anything went missing. What stays HERE is the
business guard: no plan may add an audit error (`mailpool_dns`). The `_dmarc`
record only goes live through the DMARC-email call, which also writes `ruf`.
Changes reach public DNS within ~5 minutes.

⚠️ **After a write, an error never says "nothing changed" unless it is known.**
Three named outcomes, all `INTERNAL_ERROR` (not "invalid argument": re-running
the same call is exactly what must not happen blindly):

- `dns_outcome_unknown` — the DNS write itself got no answer (timeout, 5xx):
  re-read with `op="dns"` before anything else;
- `dns_not_kept` — Mailpool lost records on the write; the client restored
  the previous set and read it back, or the error says it could not;
- `partially_applied` — the DNS write landed, a later step did not; the error
  lists what was applied.

The DMARC policy and redirect-URL settings are not served: not verified live
yet (a later report-address change may republish the stored `p=`).

One client per tool call (`c = _client()`): the key is resolved once. Client
calls are written out (`c.get_domain_dns(…)`) so the version-skew probe
(`test_tools_client_methods_exist`) can check them.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Literal, Optional

from fastmcp import FastMCP
from mcp.types import ErrorData, INTERNAL_ERROR, INVALID_PARAMS
from pydantic import BaseModel, ConfigDict, Field

from .. import access
from ..connectors import verify as connector_verify
from ..mcp_errors import McpError
from . import mailpool_dns


def _bad(msg: str, reason: str = "invalid_argument") -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg, data={"reason": reason}))


def _after_write(reason: str, msg: str, **data) -> McpError:
    """A write may have landed: `INTERNAL_ERROR`, so it is not read as "fix the
    argument and call again"."""
    return McpError(ErrorData(code=INTERNAL_ERROR, message=msg,
                              data={"reason": reason, **data}))


class DnsRecord(BaseModel):
    """One record, Mailpool's shape."""
    model_config = ConfigDict(extra="forbid")

    type: Literal["A", "AAAA", "CNAME", "MX", "TXT"]
    key: Optional[str] = Field(None, description="host label; null = apex")
    value: str = Field(min_length=1)
    priority: Optional[int] = Field(None, description="MX only")


class NewMailbox(BaseModel):
    """One mailbox to create."""
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, description="full address, on a workspace domain")
    firstName: str = Field(min_length=1)
    lastName: str = Field(min_length=1)
    type: Literal["google", "outlook", "shared"]
    forwardTo: Optional[str] = Field(None, description="address replies are forwarded to")
    signature: Optional[str] = None


def _refuse_ignored(op: str, **provided) -> None:
    """A provided argument THIS op does not use is an intent error."""
    for name, value in provided.items():
        if value is not None and value is not False:
            raise _bad(f"op={op!r} does not use `{name}`.")


DOMAIN_FIELDS = ("id", "domain", "type", "status", "expireAt", "redirectUrl")

#: Spam checks complete within seconds. `run` sends no new read past this
#: much time after the creation started (the REST tool-invoke path cuts at
#: 45 s). A read already sent is bounded by the client's own per-call budget
#: (`CALL_BUDGET_S`), not by what remains here: the client takes no timeout.
SPAM_CHECK_WAIT_S = 20
SPAM_CHECK_POLL_S = 4

#: `op="audit"` without `domain_id` reads one DNS set per domain.
AUDIT_MAX_DOMAINS = 20


def _slim_domain(d: Any) -> Any:
    if not isinstance(d, dict):
        return d
    out = {k: d.get(k) for k in DOMAIN_FIELDS if d.get(k) is not None}
    owner = d.get("domainOwner")
    if isinstance(owner, dict) and owner.get("company"):
        out["owner"] = owner.get("company")
    return out


def _slim_mailbox(m: Any) -> Any:
    """The client's allowlist, plus the embedded domain cut to id/name/type:
    its registrant (name, address, phone) has nothing to do here."""
    from oto.tools.mailpool.client import project_mailbox
    out = project_mailbox(m)
    if isinstance(out, dict) and isinstance(out.get("domain"), dict):
        out["domain"] = {k: out["domain"].get(k) for k in ("id", "domain", "type")}
    return out


def _slim_spam_check(check: Any, full: bool) -> Any:
    if not isinstance(check, dict):
        return check
    result = check.get("result") if isinstance(check.get("result"), dict) else {}
    out = {k: check.get(k) for k in ("id", "createdAt", "state", "fromEmail")
           if check.get(k) is not None}
    if result:
        out["score"] = result.get("score")
        out["checks"] = {k: v.get("status") for k, v in result.items()
                         if isinstance(v, dict) and "status" in v}
        if full:
            out["result"] = result
    return out


WARMUP_FIELDS = ("id", "mailboxId", "email", "fullName", "type", "status",
                 "createdAt")
#: One enrolled inbox (`op="warmup_inbox"`) also serves its settings and counters.
WARMUP_INBOX_FIELDS = WARMUP_FIELDS + ("dailyTarget", "replyRate", "duration", "counters")
#: A saved registrant: the company only — name, email, address and phone are
#: personal data the caller has no use for here.
OWNER_FIELDS = ("id", "company", "country", "gtldVerified")


def _slim_fields(obj: Any, fields) -> Any:
    if not isinstance(obj, dict):
        return obj
    return {k: obj.get(k) for k in fields if obj.get(k) is not None}


def _slim_warmup(w: Any) -> Any:
    """A warmup or available inbox, as an allowlist (spec `WarmupInbox` /
    `AvailableWarmupInbox`)."""
    return _slim_fields(w, WARMUP_FIELDS)


def _slim_owners(res: Any) -> Any:
    if isinstance(res, list):
        return [_slim_fields(o, OWNER_FIELDS) for o in res]
    if isinstance(res, dict) and isinstance(res.get("data"), list):
        return [_slim_fields(o, OWNER_FIELDS) for o in res["data"]]
    return _slim_fields(res, OWNER_FIELDS)


def _page(res: Any, slim) -> Any:
    """`{count, items}`. Mailpool's own `total` is not served: on spam checks it
    still counts deleted ones (seen live: total 4, one item)."""
    if isinstance(res, dict) and isinstance(res.get("data"), list):
        items = [slim(x) for x in res["data"]]
        return {"count": len(items), "items": items}
    return res


def _upstream_message(e) -> str:
    """Mailpool refusals: `{message, error, statusCode}` or, on validation,
    `{validations: [{context, constraints}]}` — the field names are kept."""
    status = e.status_code
    body = e.body if isinstance(e.body, dict) else {}
    detail = body.get("message") or e.body
    if isinstance(body.get("validations"), list):
        detail = "; ".join(
            f"`{v.get('context')}`: {', '.join((v.get('constraints') or {}).values())}"
            for v in body["validations"] if isinstance(v, dict))
    if status == 401:
        return ("Mailpool rejected the key (401) — check the workspace API key set on "
                "this connector (Mailpool → Settings → API Keys).")
    if status == 403:
        return (f"Mailpool refused (403): {detail} — this feature is enabled per "
                "workspace, on Mailpool's side.")
    if status == 404:
        return f"Mailpool: not found (404) — {detail}."
    if status == 400 and "credit" in str(detail).lower():
        return f"Mailpool: {detail} (400) — this action is billed in credits; nothing was done."
    if status == 429:
        return "Mailpool: too many requests (429) — retry later."
    if status in (500, 502, 503, 504):
        return f"Mailpool is temporarily unavailable (HTTP {status}) — retry later."
    return f"Mailpool refused the request (HTTP {status}): {detail}"


def _verify(fields: dict, config: dict | None = None) -> None:  # noqa: ARG001
    """"Test connection" probe: the subscription slots — no parameter, no
    personal data, a clean 401 if the key is wrong."""
    from oto.tools.mailpool.client import MailpoolClient
    MailpoolClient(api_key=fields["key"]).probe()


def register(mcp: FastMCP) -> None:
    import requests
    from oto.tools.common.errors import UpstreamHTTPError
    from oto.tools.mailpool import (MailpoolCreatePartial, MailpoolCreateRefused,
                                    MailpoolExportRefused)
    from oto.tools.mailpool.client import MailpoolClient, MailpoolDnsWriteError, strip_secrets

    connector_verify.register("mailpool", _verify)

    def _client() -> MailpoolClient:
        key, _ = access.resolve_api_key("mailpool")
        return MailpoolClient(api_key=key)

    def _run(fn):
        """Every client call goes through here: errors become tool errors, and
        the result is scrubbed of secret-looking keys a second time — the single
        choke point, whatever the op does with it afterwards."""
        try:
            return strip_secrets(fn())
        except ValueError as e:
            raise _bad(str(e))
        except UpstreamHTTPError as e:
            raise _bad(_upstream_message(e), reason="upstream_refused")
        except (requests.Timeout, requests.ConnectionError):
            raise _bad("Mailpool did not answer in time — retry later.", reason="timeout")

    def _cause(e: Exception) -> str:
        if isinstance(e, UpstreamHTTPError):
            return _upstream_message(e)
        if isinstance(e, (requests.Timeout, requests.ConnectionError)):
            return "Mailpool did not answer in time"
        return str(e)

    def _need(value, name: str, op: str):
        if value is None or value == "":
            raise _bad(f"op={op!r}: `{name}` is required.")
        return value

    # --- domains ---------------------------------------------------------------

    @mcp.tool()
    def mailpool_domains(
        op: Literal["list", "get", "dns", "audit", "availability", "suggestions",
                    "google_workspace_check", "microsoft_365_check", "owners"] = "list",
        domain_id: Optional[int] = None,
        domain: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Any:
        """Mailpool domains: read them, audit their deliverability setup, look up a new name.

        Ops:
        - `list` — the workspace's domains (type google/outlook/shared/private,
          status, expiry, redirect). `limit` (1-100) / `offset` paginate.
        - `get` — one domain (`domain_id`).
        - `dns` — the DNS records Mailpool holds for a domain (`domain_id`), as
          `{type, key, value, priority}`; `key` is the host label, null = apex.
        - `audit` — findings on SPF, DKIM, DMARC, MX, CNAME conflicts and custom
          tracking domain, worst first, for one domain (`domain_id`) or a page of
          domains (at most 20 per call: `total` and `next_offset` say what is
          left). Read from Mailpool's stored configuration, not a live DNS
          lookup. Fix what it finds with `mailpool_domain_fix`.
        - `availability` — can `domain` be registered, and at what price.
          Nothing is bought (registration is not served by this connector).
        - `suggestions` — available names close to `domain`, with price.
        - `google_workspace_check` / `microsoft_365_check` — is `domain` already
          used by a Google Workspace / Microsoft 365 account (enabled per
          Mailpool workspace, otherwise refused).
        - `owners` — saved registrant contacts (company and country only).

        Args:
            op: what to do (see above).
            domain_id: Mailpool domain id (from `op="list"`).
            domain: domain name, for availability/suggestions/checks.
            limit: page size, 1-100 (at most 20 for `audit`).
            offset: items to skip.
        """
        if op in ("list", "suggestions", "owners"):
            _refuse_ignored(op, domain_id=domain_id)
        c = _client()
        if op == "list":
            _refuse_ignored(op, domain=domain)
            return _page(_run(lambda: c.list_domains(limit=limit, offset=offset)),
                         _slim_domain)
        if op == "get":
            _refuse_ignored(op, domain=domain)
            return _slim_domain(_run(lambda: c.get_domain(_need(domain_id, "domain_id", op))))
        if op == "dns":
            _refuse_ignored(op, domain=domain)
            return {"domain_id": domain_id,
                    "records": _run(lambda: c.get_domain_dns(_need(domain_id, "domain_id", op))),
                    "source": "Mailpool's stored configuration, not a live DNS lookup"}
        if op == "audit":
            _refuse_ignored(op, domain=domain)
            return _audit(c, domain_id, limit, offset)
        if op == "availability":
            return _run(lambda: c.get_domain_info(_need(domain, "domain", op)))
        if op == "suggestions":
            return _run(lambda: c.get_domain_suggestions(
                _need(domain, "domain", op), limit=limit, offset=offset))
        if op == "google_workspace_check":
            return _run(lambda: c.check_google_workspace_availability(_need(domain, "domain", op)))
        if op == "microsoft_365_check":
            return _run(lambda: c.check_microsoft_365_availability(_need(domain, "domain", op)))
        _refuse_ignored(op, domain=domain)
        return _slim_owners(_run(lambda: c.list_domain_owners()))

    def _audit(c: MailpoolClient, domain_id: Optional[int], limit: int, offset: int) -> Any:
        def one(d: dict) -> dict:
            records = _run(lambda: c.get_domain_dns(d["id"]))
            return {"domain_id": d["id"], **mailpool_dns.audit(d["domain"], records)}

        if domain_id is not None:
            return one(_run(lambda: c.get_domain(domain_id)))
        size = min(limit, AUDIT_MAX_DOMAINS)
        page = _run(lambda: c.list_domains(limit=size, offset=offset))
        targets = page.get("data", []) if isinstance(page, dict) else []
        total = page.get("total") if isinstance(page, dict) else None
        more = (offset + len(targets) < total) if isinstance(total, int) else len(targets) == size
        return {"domains": [one(d) for d in targets], "count": len(targets), "total": total,
                "next_offset": offset + len(targets) if more and targets else None}

    @mcp.tool()
    def mailpool_domain_fix(
        domain_id: int,
        spf_hard_fail: bool = False,
        dmarc_report_email: Optional[str] = None,
        tracking_host: Optional[str] = None,
        tracking_target: Optional[str] = None,
        add_records: Optional[List[DnsRecord]] = None,
        remove_records: Optional[List[DnsRecord]] = None,
        dry_run: bool = True,
    ) -> Any:
        """Edit a Mailpool domain's DNS records — preview first, then apply.

        `dry_run=True` (the default) reads the current records and returns the
        exact diff (`added`, `removed`) without writing anything. Re-call with
        `dry_run=False` to apply. Nothing is sent when there is no difference.

        Changes, combinable:
        - `spf_hard_fail=True` — end the apex SPF record in `-all` instead of
          `~all`. Only once every service sending as this domain is listed.
        - `dmarc_report_email` — where DMARC reports go. The record is also
          normalised: `fo`, `rf`, `ri` are dropped, policy and alignment kept.
          Mailpool always publishes the address as `rua` AND `ruf`. ⚠️ A domain
          without `_dmarc` gets one with p=quarantine; sp=quarantine; pct=100.
        - `tracking_host` + `tracking_target` — a custom tracking domain, e.g.
          `track` → `custom.lemlist.com` (the target comes from the sending tool).
          Replaces any CNAME already on that host.
        - `add_records` — NEW records next to the existing ones, e.g. a
          verification TXT a sending tool asks for. Never a second SPF: to
          change SPF, use `spf_hard_fail` or remove the old record and add the
          new one in the same call.
        - `remove_records` — records to delete, matched exactly.

        Safety: a change that would add an audit error (second SPF or `_dmarc`,
        MX, SPF or DKIM gone, CNAME next to other records) is refused, preview
        included. A DNS write replaces the WHOLE record set, so the full set is
        always sent, read back, and the previous set restored if a record went
        missing. Public DNS follows within ~5 minutes. After `dry_run=False`, an
        error whose reason is `dns_outcome_unknown`, `dns_not_kept` or
        `partially_applied` means records may have changed: re-read them with
        `mailpool_domains(op="dns")` before calling again.

        Args:
            domain_id: Mailpool domain id.
            spf_hard_fail: switch SPF to `-all`.
            dmarc_report_email: DMARC report address.
            tracking_host: tracking subdomain label (e.g. `track`).
            tracking_target: its CNAME target, given by the sending tool.
            add_records: records to add next to the existing ones.
            remove_records: records to delete (exact match).
            dry_run: preview only (default True).
        """
        c = _client()
        current = _run(lambda: c.get_domain_dns(domain_id))
        try:
            p = mailpool_dns.plan(
                current, spf_hard_fail=spf_hard_fail, dmarc_report_email=dmarc_report_email,
                tracking_host=tracking_host, tracking_target=tracking_target,
                add_records=[r.model_dump() for r in add_records or []],
                remove_records=[r.model_dump() for r in remove_records or []])
        except mailpool_dns.PlanRefused as e:
            raise _bad(str(e), reason=e.reason)
        preview = {"domain_id": domain_id, "added": p["added"], "removed": p["removed"],
                   "notes": p["notes"]}
        if not p["changed"]:
            return {**preview, "dry_run": dry_run, "changed": False,
                    "message": "Nothing to change: the domain already matches."}
        if dry_run:
            return {**preview, "dry_run": True, "changed": True}

        try:
            after = c.update_domain_dns(domain_id, p["records"], expected=current)
        except MailpoolDnsWriteError as e:
            raise _not_kept(e)
        except UpstreamHTTPError as e:
            if e.status_code < 500:
                raise _bad(_upstream_message(e), reason="upstream_refused")
            raise _unknown(domain_id, e)
        except (requests.Timeout, requests.ConnectionError) as e:
            raise _unknown(domain_id, e)
        except ValueError as e:
            raise _bad(str(e))
        applied = ["dns"]
        if p["publish_dmarc_email"]:
            try:
                c.set_dmarc_email(domain_id, p["publish_dmarc_email"])
            except (UpstreamHTTPError, requests.Timeout, requests.ConnectionError, ValueError) as e:
                raise _partial(domain_id, applied, ["dmarc_published"], e)
            applied.append("dmarc_published")
        out = {**preview, "dry_run": False, "changed": True, "applied": applied,
               "message": "Applied. Public DNS follows within ~5 minutes."}
        unexpected = mailpool_dns.missing_after(p["records"], after)["unexpected"]
        if unexpected:
            out["unexpected"] = unexpected
            out["message"] += (" Mailpool also holds records this call did not send "
                               "(`unexpected`): check them with op=\"dns\".")
        return out

    def _unknown(domain_id: int, e: Exception) -> McpError:
        return _after_write(
            "dns_outcome_unknown",
            f"Outcome unknown: the DNS write got no usable answer ({_cause(e)}). The "
            f"records of domain {domain_id} may or may not have changed — re-read them "
            "with mailpool_domains(op=\"dns\") before calling again.",
            applied=[])

    def _partial(domain_id: int, applied: List[str], pending: List[str], e: Exception) -> McpError:
        return _after_write(
            "partially_applied",
            f"Partially applied on domain {domain_id}: done {applied}, not done {pending} "
            f"({_cause(e)}). Re-read with mailpool_domains(op=\"dns\") before calling again.",
            applied=applied, not_done=pending)

    def _not_kept(e: "MailpoolDnsWriteError") -> McpError:
        """Mailpool lost records on the write; the client tried to put the
        previous set back. Say which of the two states the domain is in."""
        missing = [mailpool_dns.label(r) for r in e.missing]
        if e.restored:
            msg = (f"Mailpool did not keep the full record set (missing {missing}); the "
                   "previous records were restored and read back. Nothing was changed.")
        else:
            why = f" ({_cause(e.restore_error)})" if e.restore_error else ""
            msg = (f"Mailpool did not keep the full record set (missing {missing}) and "
                   f"restoring the previous set failed{why}: the domain may be left without "
                   "these records. Re-read with mailpool_domains(op=\"dns\") and fix it in "
                   "Mailpool.")
        return _after_write("dns_not_kept", msg, restored=e.restored, missing=missing)

    # --- mailboxes -------------------------------------------------------------

    @mcp.tool()
    def mailpool_mailboxes(
        op: Literal["list", "get", "create", "export", "export_login",
                    "export_batches", "export_batch"] = "list",
        mailbox_id: Optional[int] = None,
        domain_id: Optional[int] = None,
        mailboxes: Optional[List[NewMailbox]] = None,
        platform: Optional[Literal["instantly", "smartlead", "reachinbox", "reply", "saleshandy",
                                   "warmbox", "emelia", "lemlist", "plusvibe"]] = None,
        mailbox_ids: Optional[List[int]] = None,
        batch_id: Optional[int] = None,
        dry_run: bool = True,
        limit: int = 50,
        offset: int = 0,
    ) -> Any:
        """Mailpool mailboxes: address, name, type, status, domain, IMAP/SMTP hosts.

        Credentials (mailbox, IMAP/SMTP and admin passwords) are never returned;
        they stay in the Mailpool dashboard.

        Ops:
        - `list` — the workspace's mailboxes, optionally of one `domain_id`.
          `limit` (1-100) / `offset` paginate.
        - `get` — one mailbox (`mailbox_id`).
        - `create` — new mailboxes (`mailboxes`, up to 50), each taking one
          subscription slot ALREADY PAID for (nothing is bought).
          `dry_run=True` (the default) checks everything and sends nothing:
          free slots per type, each domain active and of the same type (an
          `unassigned` domain takes the type of its first mailbox; one domain
          = one type), addresses not taken. Re-call with `dry_run=False` to
          create (one Mailpool call per domain). Google/Outlook mailboxes come
          back `pending` while the provider provisions them, and their MX, SPF
          and DKIM appear once provisioned. On error reason
          `mailbox_outcome_unknown` or `partially_applied` some mailboxes may
          exist: `op="list"` before calling again.
        - `export` — hand `mailbox_ids` to a sending `platform` (e.g. lemlist):
          Mailpool connects them there itself, passwords included, so none
          passes through here. `dry_run=True` (default) checks only: the
          platform login is saved in Mailpool, each mailbox exists and is
          `active`. Asynchronous: follow the returned batch with
          `op="export_batch"`. Types the platform refuses are listed in
          `errors`. On `export_outcome_unknown`, `op="export_batches"` before
          calling again.
        - `export_login` — is a `platform` login saved in Mailpool (never its
          value). Saving it is done in the Mailpool dashboard.
        - `export_batches` / `export_batch` (`batch_id`) — export progress.

        Args:
            op: list, get or create.
            mailbox_id: Mailpool mailbox id (op="get").
            domain_id: only the mailboxes of this domain (op="list").
            mailboxes: mailboxes to create (op="create").
            platform: sending platform (op="export"/"export_login").
            mailbox_ids: mailboxes to export (op="export").
            batch_id: export batch id (op="export_batch").
            dry_run: op="create"/"export" — preview only (default True).
            limit: page size, 1-100.
            offset: items to skip.
        """
        c = _client()
        if op.startswith("export"):
            _refuse_ignored(op, mailbox_id=mailbox_id, domain_id=domain_id,
                            mailboxes=mailboxes or None)
            return _export_op(c, op, platform, mailbox_ids, batch_id, dry_run, limit, offset)
        _refuse_ignored(op, platform=platform, mailbox_ids=mailbox_ids or None,
                        batch_id=batch_id)
        if op == "create":
            _refuse_ignored(op, mailbox_id=mailbox_id, domain_id=domain_id)
            return _create_mailboxes(c, _need(mailboxes, "mailboxes", op), dry_run)
        _refuse_ignored(op, mailboxes=mailboxes or None)
        if op == "get":
            _refuse_ignored(op, domain_id=domain_id)
            return _slim_mailbox(_run(lambda: c.get_mailbox(
                _need(mailbox_id, "mailbox_id", op))))
        _refuse_ignored(op, mailbox_id=mailbox_id)
        return _page(_run(lambda: c.list_mailboxes(
            limit=limit, offset=offset, domain_id=domain_id)), _slim_mailbox)

    def _create_mailboxes(c: MailpoolClient, mailboxes: List[NewMailbox], dry_run: bool) -> Any:
        items = [m.model_dump(exclude_none=True) for m in mailboxes]
        if dry_run:
            def _plan():
                try:
                    return c.plan_mailboxes(items)
                except MailpoolCreateRefused as e:
                    raise _bad(str(e), reason="create_refused")
            plan = _run(_plan)  # upstream, timeout and input errors as tool errors
            return {"dry_run": True, **plan,
                    "message": f"Ready to create {len(plan['mailboxes'])} mailbox(es); "
                               "nothing was sent. Re-call with dry_run=False."}
        try:
            out = strip_secrets(c.create_mailboxes(items))
        except MailpoolCreatePartial as e:
            raise _after_write(
                "partially_applied",
                f"Partially applied: mailboxes created on {', '.join(e.done)}, not on "
                f"{', '.join(e.not_done)} ({_cause(e.cause)}). List the mailboxes of the "
                "domains not done before calling again with only those.",
                created=[_slim_mailbox(m) for m in strip_secrets(e.created)],
                done=e.done, not_done=e.not_done)
        except MailpoolCreateRefused as e:
            raise _bad(str(e), reason="create_refused")
        except UpstreamHTTPError as e:
            if e.status_code < 500:
                raise _bad(_upstream_message(e), reason="upstream_refused")
            raise _create_unknown(e)
        except (requests.Timeout, requests.ConnectionError) as e:
            raise _create_unknown(e)
        except ValueError as e:
            raise _bad(str(e))
        created = [_slim_mailbox(m) for m in out.get("created") or []]
        return {"dry_run": False, "created": created, "slots": out["plan"]["slots"],
                "domains": out["plan"]["domains"],
                "message": f"Created {len(created)} mailbox(es). Passwords stay in the "
                           "Mailpool dashboard; pending ones turn active on their own."}

    def _export_op(c: MailpoolClient, op: str, platform, mailbox_ids, batch_id,
                   dry_run: bool, limit: int, offset: int) -> Any:
        if op == "export_batch":
            _refuse_ignored(op, platform=platform, mailbox_ids=mailbox_ids or None)
            return _run(lambda: c.get_export_batch(_need(batch_id, "batch_id", op)))
        _refuse_ignored(op, batch_id=batch_id)
        if op == "export_batches":
            _refuse_ignored(op, platform=platform, mailbox_ids=mailbox_ids or None)
            return _run(lambda: c.list_export_batches(limit=limit, offset=offset))
        p = _need(platform, "platform", op)
        if op == "export_login":
            _refuse_ignored(op, mailbox_ids=mailbox_ids or None)
            return _run(lambda: c.export_credentials_status(p))
        ids = _need(mailbox_ids, "mailbox_ids", op)

        def _plan():
            try:
                return c.plan_export(p, ids)
            except MailpoolExportRefused as e:
                raise _bad(str(e), reason="export_refused")
        plan = _run(_plan)
        if dry_run:
            return {"dry_run": True, **plan,
                    "message": f"Ready to export {len(plan['mailboxes'])} mailbox(es) to "
                               f"{p}; nothing was sent. Re-call with dry_run=False."}
        try:
            batch = strip_secrets(c.export_mailboxes(p, ids))
        except MailpoolExportRefused as e:
            raise _bad(str(e), reason="export_refused")
        except UpstreamHTTPError as e:
            if e.status_code < 500:
                raise _bad(_upstream_message(e), reason="upstream_refused")
            raise _export_unknown(e)
        except (requests.Timeout, requests.ConnectionError) as e:
            raise _export_unknown(e)
        except ValueError as e:
            raise _bad(str(e))
        return {"dry_run": False, "batch": batch,
                "message": "Export scheduled: follow it with op=\"export_batch\"."}

    def _export_unknown(e: Exception) -> McpError:
        return _after_write(
            "export_outcome_unknown",
            f"Outcome unknown: the export got no usable answer ({_cause(e)}). A batch may "
            "exist — read mailpool_mailboxes(op=\"export_batches\") before calling again.")

    def _create_unknown(e: Exception) -> McpError:
        return _after_write(
            "mailbox_outcome_unknown",
            f"Outcome unknown: the create got no usable answer ({_cause(e)}). The "
            "mailboxes may or may not exist — list them with mailpool_mailboxes("
            "op=\"list\", domain_id=…) before calling again.")

    # --- spam checks -----------------------------------------------------------

    @mcp.tool()
    def mailpool_spam_checks(
        op: Literal["run", "get", "list", "delete"] = "list",
        mailbox_id: Optional[int] = None,
        spam_check_id: Optional[int] = None,
        wait: bool = True,
        limit: int = 20,
        offset: int = 0,
    ) -> Any:
        """Mailpool spam checks: send a test email from a mailbox and score it.

        Ops:
        - `run` — check `mailbox_id`. Free. With `wait=True` (default) waits up
          to ~20 s for the result (usually a few seconds); a check still
          `pending` after that is returned as is, to read later with
          `op="get"`. Some mailbox types are refused by Mailpool (404).
        - `get` — one check (`spam_check_id`) with its detailed results.
        - `list` — past checks with score and per-check status.
        - `delete` — remove one check (`spam_check_id`) from the history.

        Results: `score` (0-100) and `checks` — SPF, DKIM, DMARK (DMARC),
        domainAge, reverseDNS, ipBlackList, domainBlackList, spamAssassin,
        shortUrls, brokenLinks — each passed / warning / error.

        Args:
            op: run, get, list or delete.
            mailbox_id: mailbox to check (op="run").
            spam_check_id: check id (op="get"/"delete").
            wait: op="run" — wait for the result.
            limit: page size, 1-100 (op="list").
            offset: items to skip (op="list").
        """
        c = _client()
        if op == "list":
            _refuse_ignored(op, mailbox_id=mailbox_id, spam_check_id=spam_check_id)
            return _page(_run(lambda: c.list_spam_checks(limit=limit, offset=offset)),
                         lambda x: _slim_spam_check(x, full=False))
        if op == "run":
            _refuse_ignored(op, spam_check_id=spam_check_id)
            return _run_spam_check(c, _need(mailbox_id, "mailbox_id", op), wait)
        _refuse_ignored(op, mailbox_id=mailbox_id)
        sid = _need(spam_check_id, "spam_check_id", op)
        if op == "get":
            return _slim_spam_check(_run(lambda: c.get_spam_check(sid)), full=True)
        _run(lambda: c.delete_spam_check(sid))
        return {"deleted": sid}

    def _run_spam_check(c: MailpoolClient, mailbox_id: int, wait: bool) -> Any:
        """Create, then poll until `SPAM_CHECK_WAIT_S`. A poll that fails or
        runs out of time is not a tool failure: the check exists, it is
        returned pending with how to read it later."""
        deadline = time.monotonic() + SPAM_CHECK_WAIT_S
        try:
            check = strip_secrets(c.create_spam_check(mailbox_id))
        except (requests.Timeout, requests.ConnectionError):
            raise _bad("Mailpool did not answer in time: a spam check may have been created "
                       "anyway — see mailpool_spam_checks(op=\"list\") before running another.",
                       reason="timeout")
        except UpstreamHTTPError as e:
            raise _bad(_upstream_message(e), reason="upstream_refused")
        except ValueError as e:
            raise _bad(str(e))
        note = None
        while wait and isinstance(check, dict) and check.get("state") == "pending":
            if time.monotonic() + SPAM_CHECK_POLL_S >= deadline:
                note = "still pending after the wait: read it later with op=\"get\""
                break
            time.sleep(SPAM_CHECK_POLL_S)
            try:
                check = strip_secrets(c.get_spam_check(check["id"]))
            except (UpstreamHTTPError, requests.Timeout, requests.ConnectionError) as e:
                note = f"re-read failed ({_cause(e)}): read it later with op=\"get\""
                break
        out = _slim_spam_check(check, full=True)
        if note and isinstance(out, dict):
            out["next_step"] = note
        return out

    # --- account ---------------------------------------------------------------

    @mcp.tool()
    def mailpool_account(
        op: Literal["slots", "warmup", "warmup_inbox", "warmup_available"] = "slots",
        warmup_id: Optional[int] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Any:
        """Mailpool workspace: subscription slots and warmup.

        Ops:
        - `slots` — mailboxes used vs paid, per type, and warmup slots. A new
          mailbox needs a free slot (buying slots is not served here).
        - `warmup` — inboxes enrolled in Mailpool warmup, with status.
        - `warmup_inbox` — one enrolled inbox (`warmup_id`): settings, counters.
        - `warmup_available` — active mailboxes not enrolled yet.

        Args:
            op: what to read.
            warmup_id: warmup inbox id (op="warmup_inbox").
            limit: page size, 1-100.
            offset: items to skip.
        """
        c = _client()
        if op == "warmup_inbox":
            inbox = _run(lambda: c.get_warmup_inbox(_need(warmup_id, "warmup_id", op)))
            return _slim_fields(inbox, WARMUP_INBOX_FIELDS)
        _refuse_ignored(op, warmup_id=warmup_id)
        if op == "warmup":
            return _page(_run(lambda: c.list_warmup_inboxes(limit=limit, offset=offset)),
                         _slim_warmup)
        if op == "warmup_available":
            return _page(_run(lambda: c.list_available_warmup_inboxes(
                limit=limit, offset=offset)), _slim_warmup)
        return _run(lambda: c.get_subscription_slots())
