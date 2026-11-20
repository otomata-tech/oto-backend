"""Apollo.io — B2B prospection (organizations, people, job postings, contacts,
sequences, one-off emails, conversations).

Wraps `oto.tools.apollo.ApolloClient`. Two key regimes depending on what an
endpoint queries, NOT on read/write:

- **Shared Apollo database** (`mixed_companies/search`, `mixed_people/api_search`,
  `people/match`, `organizations/*`): `access.resolve_api_key("apollo")` — user
  key (`/account`) first, otherwise the platform key (free-tier, daily quota =
  `default_quota` per user/day). Any key returns the SAME database (~28M
  companies) → a pooled platform key is safe there. The metered platform quota =
  the **Apollo credits** (`people/match`, which reveals a
  contact); org/people search and job postings consume no credit →
  not metered. ⚠️ **Exhausted quota = NAMED refusal, never a silent fallback**
  (oto-backend#710, signals #311/#312/#313): `resolve_api_key` raises a McpError
  that states the counter (`used/limit`) and that it resets at midnight — and
  `apollo_match_person`, the only debtor of this quota, echoes `platform_quota`
  (`access.platform_quota_hint`, `oto_mcp/access/views.py`) in its response
  WHEN the key is the platform key, so that a batch worker can decide BEFORE the refusal
  instead of discovering it in the middle of a lead.
- **The key OWNER's WORKSPACE** (contacts, sequences, emails,
  connected mailboxes, conversations — EVERYTHING added in this module):
  `access.resolve_credential("apollo", want="byo")`, NEVER `resolve_api_key`.
  This is not a read/write distinction — `apollo_email(op="search")` in
  read mode returns `body_html`/`body_text` of the emails SENT BY the key owner;
  `apollo_email_accounts` returns THEIR mailboxes (HTML signature, deliverability
  score). A pooled platform key would expose its owner's private data
  to any other oto user. And for writing
  specifically (enrolling contacts, sending an email): a send on this
  key would also go out from THEIR mailbox, to THEIR contacts — same lock as
  Lightfield `send_email` (oto-core 97c53ce, authorized by the maintainer on
  19/08/2026 under two conditions: the connector only exists if an org sets ITS
  key, and the send goes out from a mailbox that the owner of this key has themselves
  connected — condition #2 carried here by the local lock
  `send_email_from_email_account_id` on the oto-core client side). Conversations
  (transcripts of real calls/video meetings) are byo-only for the same private
  space reason, plus a conditional credit cost (1 if AI insights, 0 otherwise)
  that cannot be metered a priori on the platform quota side. The **contacts** (`apollo_contact`)
  are the clearest case of this rule: a contact is the address book of the
  team that sets the key — hence byo-only on ALL THREE ops, READS INCLUDED,
  even though none costs a credit. Do not confuse them with `people/*`,
  which query the shared database: a person found there is a contact here
  only if the team has saved them.
- **The REVEALS** (`apollo_reveal_phone`, and `apollo_match_person(reveal_personal_emails=True)`):
  byo-only too, but for a THIRD reason — COST, not the data
  boundary. They do query the shared database, so nothing would prevent the
  common key; what prevents it is that the counter (`units`, settled per call) debits
  1 unit per call, the price of a bare match, whereas Apollo bills a surcharge
  on top (~9 credits for a phone; plan-dependent scale, not measured, for
  personal emails). `platform_quota` would lie by a factor we cannot
  name, and it is the only figure a batch worker stops on.

⚠️ Apollo doc (not verified from this environment, no key available here):
`add_contact_ids` and `/emailer_messages/{id}/activities` (email stats) require
a "Master" key and return 403 otherwise — to be confirmed with a real key.
The same requirement is documented on the THREE contacts endpoints
(`typed_custom_fields`, `contacts/{id}` for reads and PATCH): there, rather than
waiting, `apollo_contact` translates the 403 into a message that NAMES the prerequisite, and
its write remains possible without the catalog (degraded validation, announced).
"""
from __future__ import annotations

import logging
import warnings
from typing import Any, Literal, Optional

from fastmcp import FastMCP
from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, apollo_receiver, output_projection, session_org
from .lecture import ACTE, LECTURE
from ..datastore.identite import AdresseJson as Adresse

logger = logging.getLogger(__name__)


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def register(mcp: FastMCP) -> None:
    from oto.tools.apollo.client import ApolloClient, ApolloError

    def _client(units: int = 1) -> tuple[ApolloClient, bool]:
        # `units`: batch size, so that the common key's quota is checked
        # for the whole batch before the call (oto#168).
        key, is_platform = access.resolve_api_key("apollo", units=units)
        return ApolloClient(api_key=key), is_platform

    def _credits_billed(out: Any, tool: str, served: Optional[int]) -> int:
        """The Apollo CREDITS this call consumed, for the BILLED line
        (`tool_calls.quantity`) — same counter and same unit as `apollo_bulk_match`
        (oto#168), which keeps its own reading.

        Upstream's `credits_consumed` wins whenever it is a usable count. Otherwise
        `served` — what the RESPONSE shows was returned (a person, an organization,
        a number of organizations), never what was submitted and never 1 "for the
        call": the biller invoices what is traced, so a figure guessed upward would
        be invoiced. A field that is there but unreadable is SAID in the log, as in
        `apollo_bulk_match`; its absence is not — nothing shows these endpoints
        declare it, and a warning per call would say nothing. `served=None` (a
        response in which nothing can be counted) traces 0 and SAYS SO: nothing is
        billed on a response we could not read."""
        credits = out.get("credits_consumed") if isinstance(out, dict) else None
        if isinstance(credits, int) and not isinstance(credits, bool) and credits >= 0:
            return credits
        if credits is not None:
            logger.warning("%s: `credits_consumed` unreadable (%r) in Apollo's response "
                           "— quantity counted on what the response returned (%r)",
                           tool, credits, served)
        if served is None:
            logger.warning("%s: nothing countable in Apollo's response — quantity "
                           "traced 0, nothing will be billed for this call", tool)
            return 0
        return served

    _BYO_ESPACE_PRIVE = (
        "this ONLY concerns sequences/emails/conversations (your own "
        "data): Apollo search and enrichment remain usable "
        "without your own key.")

    # Apollo's REVEALS — phone AND personal emails — are byo-only for
    # a DIFFERENT reason than everything else in this module: it is not a data
    # boundary, it is COST. A reveal makes Apollo bill a
    # SURCHARGE on top of the match, while the platform counter
    # (`units`, settled per call) debits 1 unit per call — the price of a bare match,
    # whatever happens. Yet `platform_quota` exists precisely so that a batch worker
    # stops BEFORE the wall (oto-backend#710): letting it lie would break
    # the only measure it relies on.
    #
    # ⚠️ The SAME rule for both, deliberately. What separates them is
    # the size of the gap, not its nature: for the phone it is measured (~9
    # credits where a bare match costs 1); for personal emails it is not
    # — Apollo bills them on a separate pot whose scale depends on the
    # plan, and none of our measurements quantifies it. An unknown factor is not
    # a zero factor: opening the common key to the only reveal whose
    # multiplier we do not know would amount to saying the counter lies less when we do not
    # know by how much. The day the gap is measured AND the counter knows how to debit
    # it, that is when the rule can change — not before.
    _BYO_REVEAL_TELEPHONE = (
        "the phone reveal NEVER goes through the platform key (Apollo bills it "
        "~9 credits when a bare match costs 1): set your own Apollo key. "
        "Search and `apollo_match_person` keep working without it.")
    _BYO_REVEAL_EMAILS_PERSO = (
        "`reveal_personal_emails=True` NEVER goes through the platform key: "
        "Apollo bills this reveal ON TOP of the match, on a pot whose scale "
        "depends on the plan, while our counter can only debit a bare match "
        "— set your own Apollo key. Without it `apollo_match_person` still works, "
        "it simply does not return the personal emails.")

    def _client_byo(precision: str = _BYO_ESPACE_PRIVE) -> ApolloClient:
        """Client resolved WITHOUT a platform tier — for any call that writes
        (enrollment, send), reads sensitive data (conversations) or
        commits off-scale spending (the reveals: phone, personal
        emails).

        Apollo is the first connector to mix the two regimes in the
        SAME module (search/enrichment = platform_key_open, everything
        else = byo-only): the generic message of `resolve_credential`
        ("No `apollo` credential configured for you") would be misleading for a
        user who already sees apollo_search_organizations working via the platform
        key and would not understand why THIS call refuses it.
        Hence `precision`: the reason for the refusal is not the same everywhere, and
        serving "your own data" to someone who hits a COST wall would send them
        looking in the wrong place."""
        return ApolloClient(api_key=_cle_byo(precision).key)

    def _cle_byo(precision: str = _BYO_ESPACE_PRIVE):
        """The caller's BYO Apollo key, resolved (see `_client_byo`) — for whoever
        needs its SCOPE in addition to the client: a phone reveal is ordered
        and read back under the key that pays for it (`apollo_receiver.portee`)."""
        try:
            return access.resolve_credential("apollo", want="byo")
        except McpError as e:
            msg = e.error.message or ""
            if "credential configured for" in msg:
                # Only THIS generic message (total absence of a BYO credential) is
                # ambiguous here — the others (multi-account, account not found) are
                # already precise and have nothing to do with platform vs byo.
                raise McpError(ErrorData(
                    code=INVALID_PARAMS, message=f"{msg} — {precision}"))
            raise

    @mcp.tool(annotations=LECTURE)
    def apollo_search_organizations(
        name: Optional[str] = None,
        domain: Optional[str] = None,
        country: Optional[str] = None,
        employee_ranges: Optional[list[str]] = None,
        revenue_min: Optional[int] = None,
        revenue_max: Optional[int] = None,
        locations: Optional[list[str]] = None,
        keywords: Optional[list[str]] = None,
        technologies: Optional[list[str]] = None,
        org_ids: Optional[list[str]] = None,
        per_page: int = 10,
        page: int = 1,
        fields: Optional[list[str]] = None,
    ) -> dict:
        """Find companies by firmographics — the CHEAP way to qualify a list.

        Costs 1 Apollo credit per PAGE (up to 100 results), where enrichment costs
        1 credit per COMPANY: filter here, enrich only what you keep.

        ⚠️ Results carry revenue and headcount GROWTH, but NOT the headcount itself
        — that's why you filter by `employee_ranges` instead of reading a number.
        For the exact headcount and its per-department split, enrich (see
        apollo_enrich_organization / apollo_bulk_enrich_organizations).

        Args:
            name: company name.
            domain: company domain.
            country: HQ country (shorthand for `locations`).
            employee_ranges: headcount brackets "min,max", e.g. ["11,50", "51,200"].
            revenue_min / revenue_max: annual revenue bounds.
            locations: HQ cities/regions/countries.
            keywords: activity keywords.
            technologies: technology uids in use, e.g. ["salesforce"].
            org_ids: Apollo organization ids.
            per_page: results per page (≤100). page: page number.
            fields: keep ONLY these keys on each organization (e.g.
                ["name", "primary_domain", "linkedin_url"]); the envelope
                (pagination, totals) is kept. Use it when paginating: a full page
                at per_page=100 is ~113 000 characters, past some clients' cap.
                A requested key that NO organization of the page carries is
                listed in `missing_fields` — this endpoint does not return it,
                so do not write it as an empty column: the exact headcount,
                industry and country come from apollo_enrich_organization.
        """
        client, _ = _client()
        found = client.search_organizations(
            name=name, domain=domain, country=country, per_page=per_page, page=page,
            employee_ranges=employee_ranges, revenue_min=revenue_min,
            revenue_max=revenue_max, locations=locations, keywords=keywords,
            technologies=technologies, org_ids=org_ids)
        # The BILLED line, as for the enrichments below: unconditional, common key
        # OR own. Apollo charges this search 1 credit per PAGE, whatever its size
        # (docstring above). Without a figure from upstream, a page that came back
        # with at least one company — in `organizations` or in `accounts`, the two
        # lists of this endpoint — counts 1, and an empty page 0: nothing shows that
        # Apollo bills a page that returned nothing. The common key's quota
        # (`units`) is not debited by a search, and still is not.
        session_org.note_call_trace(quantity=_credits_billed(
            found, "apollo_search_organizations",
            1 if isinstance(found, dict) and any(
                isinstance(found.get(k), list) and found[k]
                for k in ("organizations", "accounts")) else 0))
        # OPT-IN projection (`fields` omitted ⇒ payload unchanged). The default is not
        # touched here: choosing it would require measuring which keys of an organization
        # record are never useful, as `_CONTACT_NOISE` was above.
        # What this batch fixes is the ABSENCE of an output: without `fields`, a page at
        # per_page=100 (~113,000 chars) exceeds the output limit of some MCP
        # clients and the call becomes unusable — signal #645.
        if not fields:
            return found
        # `mixed_companies/search` returns TWO lists — `organizations` and `accounts`
        # (the companies already present in the Apollo account). Projecting only the
        # first would leave `fields` with no visible effect on half the payload,
        # which is worse than no parameter at all. `project` is pure and tolerates
        # a missing path: chaining is safe both ways.
        out = output_projection.project(found, items_path="organizations",
                                        fields=fields)
        out = output_projection.project(out, items_path="accounts", fields=fields)
        # A requested key that no row carries used to be dropped without a word, and
        # the caller wrote it empty downstream (oto#174). We name it, with the
        # source that carries it — same pattern as HubSpot's `missing_properties`.
        absentes = output_projection.missing_fields(
            found, items_paths=("organizations", "accounts"), fields=fields)
        if absentes:
            out["missing_fields"] = absentes
            out["missing_fields_hint"] = (
                "No organization on this page carries these keys: Apollo search "
                "does not return them. The exact headcount (`estimated_num_employees`), "
                "the industry and the country come from `apollo_enrich_organization` "
                "(or `apollo_bulk_enrich_organizations`, 10 per call) — do not "
                "write them empty.")
        return out

    @mcp.tool(annotations=LECTURE)
    def apollo_enrich_organization(domain: str) -> dict:
        """Enrich a company from its domain (firmographics, size, industry…).

        Returns the exact `estimated_num_employees`, its per-department split
        (`departmental_head_count`), 6/12/24-month headcount growth, revenue,
        founding year and tech stack. Costs 1 Apollo credit. For several companies
        at once, prefer apollo_bulk_enrich_organizations (same cost, 10× fewer calls).
        """
        client, _ = _client()
        out = client.enrich_organization(domain)
        # The BILLED line, as in `apollo_bulk_match`: unconditional, common key OR
        # own (`key_mode`, set by the resolver, says whose key answered). 1 credit
        # when an organization came back, 0 when none did — a traced 0 says "nothing
        # billed", not "not measured".
        session_org.note_call_trace(quantity=_credits_billed(
            out, "apollo_enrich_organization",
            1 if isinstance(out, dict) and out.get("organization") else 0))
        return out

    @mcp.tool(annotations=LECTURE)
    def apollo_bulk_enrich_organizations(domains: list[str]) -> dict:
        """Enrich UP TO 10 companies in a single call — same fields as
        apollo_enrich_organization (headcount, per-department split, growth, revenue).

        Costs 1 Apollo credit per company (a batch saves CALLS, not credits: the
        enrich rate limit is 600/h, so batching divides your call budget by 10).
        Over 10 domains, split into batches yourself — the API refuses more.
        """
        client, _ = _client()
        out = client.bulk_enrich_organizations(domains)
        # The BILLED line: 1 credit per organization RETURNED, never per domain
        # submitted — a domain that brings no record back is not counted.
        orgs = out.get("organizations") if isinstance(out, dict) else None
        session_org.note_call_trace(quantity=_credits_billed(
            out, "apollo_bulk_enrich_organizations",
            sum(1 for o in orgs if o) if isinstance(orgs, list) else None))
        return out

    @mcp.tool(annotations=LECTURE)
    def apollo_search_people(
        domains: Optional[list[str]] = None,
        org_ids: Optional[list[str]] = None,
        titles: Optional[list[str]] = None,
        seniorities: Optional[list[str]] = None,
        person_locations: Optional[list[str]] = None,
        organization_locations: Optional[list[str]] = None,
        per_page: int = 25,
        page: int = 1,
    ) -> dict:
        """Search people by company domains/ids, titles, seniorities, location (net-new).

        Returns identities WITHOUT email/phone — reveal a contact with
        apollo_match_person (which costs an Apollo credit).

        ⚠️ LAST NAMES COME BACK OBFUSCATED here ("Vi***l"). To reveal someone you
        found, pass the `id` of the result as `person_id` to apollo_match_person —
        NEVER first name + company, which matches nobody: Apollo then mints an empty
        record and charges the credit anyway.

        ⚠️ A DOMAIN IS WORLDWIDE. On a subsidiary of an international group, the
        domain is shared across every country: franke.com returns 1887 profiles,
        verifone.com 3282, sonova.com 3147 — targeting the French entity by domain
        alone means revealing at random, one credit each, mostly on the wrong
        country. Add `person_locations=["France"]`. Same for the reverse case: a
        French head office with expatriates is `organization_locations`.

        Args:
            domains: company domains, e.g. ["acme.com"].
            org_ids: Apollo organization ids (from apollo_enrich_organization).
            titles: job-title keywords, e.g. ["directeur financier", "CFO"].
            seniorities: e.g. ["c_suite", "founder", "owner", "director", "manager"].
            person_locations: where the PERSON is — country, region or city as
                Apollo spells it, e.g. ["France"], ["Paris, France"]. THE filter
                for a national subsidiary of a global domain.
            organization_locations: where their EMPLOYER's site is (≠ the person's
                own location: a French-based employee of a German site matches
                person_locations=["France"], not organization_locations).
        """
        client, _ = _client()
        return client.search_people(
            domains=domains, org_ids=org_ids,
            titles=titles, seniorities=seniorities,
            person_locations=person_locations,
            organization_locations=organization_locations,
            per_page=per_page, page=page)

    # The weight of a match sits in the nested ORGANIZATION record, and in
    # FIVE of its keys: measured on a real match on 2026-09-11, `organization`
    # weighs 55,404 characters out of 60,701, of which `current_technologies` 32,321 alone.
    # The call EXCEEDED an MCP client's output limit for a SINGLE
    # person — same failure mode as signal #645 on
    # `apollo_search_organizations`, and on the tool that every list
    # building calls in a loop. Without this, sourcing 50 contacts = 3 M characters.
    #
    # Named DENYLIST, never an allowlist: `name`, `primary_domain`, `phone`,
    # `industry`, `estimated_num_employees`, `short_description` stay, and a
    # key that Apollo might add tomorrow stays visible (lesson `fr_get`/`liste_idcc`).
    # ⚠️ And `organization` is NOT removed wholesale, unlike
    # `_CONTACT_NOISE`: `people/match` returns no top-level `organization_name`
    # (verified on 2026-09-11), so removing it entirely would lose the
    # company name — which the contact record, for its part, keeps.
    _MATCH_ORG_NOISE = ("current_technologies", "technology_names",
                        "funding_events", "suborganizations", "keywords")

    def _light_org(person):
        """A person record whose organization has lost its heavy blocks.

        Returns `(record, lightened?)` — the boolean says whether there was anything to
        remove, so as not to announce a projection that did nothing."""
        if not isinstance(person, dict):
            return person, False
        org = person.get("organization")
        if not isinstance(org, dict):
            return person, False
        allege = {k: v for k, v in org.items() if k not in _MATCH_ORG_NOISE}
        if len(allege) == len(org):
            return person, False
        return {**person, "organization": allege}, True

    # The BATCH has its own measurement, and it is not the single-call one. Measured on
    # 2026-09-11 on the example response that Apollo documents for
    # `people/bulk_match` (real shape, records repeated up to 10): 88,740 chars
    # served raw, 85,941 after the organization cut alone — above the
    # 60,693 chars that already overflowed an MCP client for ONE person. A batch record
    # weighs ~6,300 chars, of which `employment_history` 2,525 and `account` 1,756 (the
    # COMPANY record of the caller's Apollo CRM, which duplicates `organization`). What a
    # list building comes looking for — the revealed name, the title, the email, the
    # LinkedIn, the employer — is in neither. A batch truncated by the client
    # is a lost batch, and a paid one. Named DENYLIST, as above; `full=True` returns everything.
    _LOT_PERSON_NOISE = ("employment_history", "account")

    def _light_match(person):
        """A BATCH record: the lightened organization (`_light_org`), then the two blocks
        that make up a batch's weight. Returns `(record, lightened?)`, like `_light_org`."""
        person, allegee = _light_org(person)
        if not isinstance(person, dict):
            return person, allegee
        reste = {k: v for k, v in person.items() if k not in _LOT_PERSON_NOISE}
        return (reste, True) if len(reste) < len(person) else (person, allegee)

    def _projection_bloc(lot: bool = False) -> dict:
        dropped = [f"organization.{k}" for k in _MATCH_ORG_NOISE]
        why = ("heavy blocks of the company record — 91% of the payload, and "
               "the call exceeded the output limit for ONE person")
        if lot:
            dropped = list(_LOT_PERSON_NOISE) + dropped
            why = ("employment history, Apollo CRM company record and heavy blocks "
                   "of the employer — a batch of 10 exceeded the output limit of an "
                   "MCP client")
        return {"dropped": dropped, "why": why, "how_to_get_everything": "full=True"}

    def _light_person(payload: dict) -> dict:
        """Lightens `person.organization` of the five heavy blocks, and SAYS so."""
        person, allegee = _light_org(payload.get("person"))
        if not allegee:
            return payload
        out = {**payload, "person": person}
        out["projection"] = _projection_bloc()
        return out

    # ⚠️ The reveal served its ENTIRE record — it was the only one of the three not
    # to be lightened, and it is the customer-report tool. Measured on a REAL call
    # in production on 2026-09-11 (org on a paid key, one person): **65,374
    # characters**, of which `person.organization` 59,244 and `employment_history` 4,396.
    # The MCP client REFUSED the response (`exceeds maximum allowed tokens`): the call
    # cost its credits, the numbers were ordered, and the agent could read nothing —
    # exactly the failure the projection exists to prevent, on the only
    # tool that had missed it. Lightened: 5,457 chars, i.e. 92% less.
    #
    # ⚠️ And `phone_enrichment.request_id` is NOT the polling identifier: Apollo
    # returns TWO (`6aa46cf…` internal, and the signed 64-bit `request_id` at the top
    # level) and its own message says to use "the top-level `request_id`".
    # Two identifiers of which only one works, in the same response, is a trap —
    # we remove the one that polls nothing and NAME it, Apollo's message staying
    # there to explain which one counts.
    _REVEAL_TRAP = ("phone_enrichment.request_id",)

    def _light_reveal(payload: dict) -> dict:
        person, allegee = _light_match(payload.get("person"))
        out = {**payload, "person": person} if allegee else dict(payload)

        piege = False
        pe = out.get("phone_enrichment")
        if isinstance(pe, dict) and "request_id" in pe:
            out["phone_enrichment"] = {k: v for k, v in pe.items() if k != "request_id"}
            piege = True

        if not (allegee or piege):
            return payload
        bloc = _projection_bloc(lot=True)
        bloc["dropped"] = ([*_REVEAL_TRAP] if piege else []) + (
            bloc["dropped"] if allegee else [])
        bloc["why"] = ("company record, employment history and Apollo CRM company "
                       "record — 65,374 chars measured on a real reveal, refused by the "
                       "client; plus the `phone_enrichment` identifier, which polls "
                       "NOTHING (it is the top-level `request_id` that polls)")
        out["projection"] = bloc
        return out

    # ⚠️ POLLING returns the same records as the reveal, but NOT in the same shape:
    # the webhook envelope, with a `webhook_result.people[]` array, and not a
    # top-level `person`. Reapplying `_light_reveal` as is would bite on
    # nothing (it reads `payload["person"]`) and pass for a fix — the
    # response would stay whole, ~15,000 chars per person, and a batch of 50 did not fit
    # in any context (otomata-tech/oto#186). So we project EACH element of
    # `people[]` with the batch cut, and we SAY so, on the real path.
    def _light_reveal_result(result: dict) -> tuple[dict, Optional[dict]]:
        """`(envelope, projection block | None)` — `None`: nothing was removed."""
        wr = result.get("webhook_result")
        people = wr.get("people") if isinstance(wr, dict) else None
        if not isinstance(people, list):
            return result, None
        allegees = [_light_match(p) for p in people]
        if not any(a for _, a in allegees):
            return result, None
        out = {**result, "webhook_result": {**wr, "people": [p for p, _ in allegees]}}
        bloc = _projection_bloc(lot=True)
        bloc["dropped"] = [f"result.webhook_result.people[].{d}" for d in bloc["dropped"]]
        return out, bloc

    def _stringify_request_id(payload: dict) -> dict:
        """`request_id` as a STRING — Apollo returns one on EVERY match, reveal or not.

        ⚠️ It is a SIGNED 64-bit integer (~7.2e17, often negative): it exceeds
        the precision of a JavaScript number, and a tool's response travels as
        JSON all the way to clients made of it. Measured in prod on 2026-09-11,
        a bare match returned `-4604290848231370000` — four trailing zeros, a
        value that float64 has already rewritten. An agent that passes this id to
        `apollo_reveal_phone_result` polls an identifier that does not exist.
        `apollo_reveal_phone` already serialized it; the match did not.
        """
        rid = payload.get("request_id")
        if rid is None or isinstance(rid, str):
            return payload
        return {**payload, "request_id": str(rid)}

    @mcp.tool()
    def apollo_match_person(
        person_id: Optional[str] = None,
        linkedin_url: Optional[str] = None,
        email: Optional[str] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        name: Optional[str] = None,
        domain: Optional[str] = None,
        org_name: Optional[str] = None,
        reveal_personal_emails: Optional[bool] = None,
        full: bool = False,
    ) -> dict:
        """Match a single person (enrichment). Returns {} if no match.

        Pass the strongest identifier you have. Coming from apollo_search_people, that
        is `person_id` = the `id` of the search result — search obfuscates last names,
        so the id is the ONLY reliable handle on someone you just found. Otherwise:
        email or linkedin_url, or a FULL name (first + last) with the company.

        ⚠️ Costs 1 Apollo credit per call, charged even when nothing matches: a weak
        identifier (first name + company) makes Apollo mint an EMPTY record rather than
        return nothing. Such an answer carries `person._stub: true` — treat it as a
        failure, not as data. Calls with no usable identifier are refused before the
        credit is spent.

        ⚠️ ON THE SHARED PLATFORM KEY, the response carries `platform_quota`
        (`used`/`limit`/`remaining` TODAY, on THIS key) — check it while working
        through a batch to stop BEFORE the next call hits the wall, instead of
        finding out mid-lead. Absent on a BYO key (no ceiling applies) or when the
        platform quota is unlimited for your org. Once `remaining` reaches 0, the
        NEXT call fails outright (no partial/degraded match) — either pose your own
        key, or fall back for THIS lead to hunter_email_finder (email) and
        kaspr_enrich_linkedin / fullenrich_enrich_linkedin (phone, LinkedIn history):
        different source, no credit burned on a call that would just fail.

        ⚠️ NO MOBILE OR DIRECT DIAL HERE: Apollo never returns those synchronously.
        apollo_reveal_phone orders them, on your own Apollo key.

        ⚠️ `reveal_personal_emails=True` needs YOUR OWN Apollo key too: Apollo bills
        that reveal ON TOP of the match, and the shared key's meter can only charge a
        plain match. Same rule as apollo_reveal_phone.

        The employer's heaviest blocks (tech stack, funding, sub-orgs, keywords) are
        dropped by default — they were 91% of the payload and overflowed MCP output
        limits on ONE person. `projection` names them; `full=True` returns them.

        Args:
            reveal_personal_emails: also return PERSONAL emails (your own Apollo key;
                withheld in GDPR regions, so empty is an answer, not a failure).
            full: return Apollo's payload untouched, tech stack and all. Costs the
                same — this is about size, not data you are missing.
        """
        # A reveal never goes out on the common key — see `_BYO_REVEAL_*`
        # above. It is the ACTION that switches, not the tool: `apollo_match_person`
        # stays open to the platform tier as long as no reveal is requested.
        if reveal_personal_emails:
            client, is_platform = _client_byo(_BYO_REVEAL_EMAILS_PERSO), False
        else:
            client, is_platform = _client()
        try:
            result = client.match_person(
                person_id=person_id, linkedin_url=linkedin_url, email=email,
                first_name=first_name, last_name=last_name, name=name,
                domain=domain, org_name=org_name,
                reveal_personal_emails=reveal_personal_emails) or {}
        except ValueError as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))
        if is_platform:
            session_org.note_call_trace(units=1)
            quota = access.platform_quota_hint("apollo")
            if quota is not None:
                result = {**result, "platform_quota": quota}
        # The BILLED line (`tool_calls.quantity`) is ANOTHER counter than the quota
        # above, which stays 1 per call: same rule as `apollo_bulk_match` —
        # unconditional, common key OR own, in Apollo CREDITS. Without a figure from
        # upstream, a returned `person` counts 1 — the `_stub` shell included, Apollo
        # mints it and bills it — and no person counts 0.
        # ⚠️ Two cases this UNDER-counts, for lack of a figure in the response: a
        # non-match Apollo may still have billed (nothing has ever shown it is
        # refunded), and the surcharge of `reveal_personal_emails`.
        session_org.note_call_trace(quantity=_credits_billed(
            result, "apollo_match_person", 1 if result.get("person") else 0))
        result = _stringify_request_id(result)
        return result if full else _light_person(result)

    # ------------------------------------------------------------------
    # Direct phone — the only action of this module that does NOT return its
    # result. Apollo verifies the numbers on its side and POSTs them to a URL
    # a few minutes later; the immediate response only carries a
    # `request_id`. That URL is now OURS, generated on each reveal
    # (`apollo_receiver.py`): the agent no longer supplies an address — a URL outside
    # its environment, as a tool argument, which an MCP client may refuse.
    # `apollo_reveal_phone_result` reads what Apollo delivered to us, and only polls
    # Apollo (`webhook_result/{id}`, 0 credit, 30 days) as a fallback.
    #
    # Submit + poll form: that of `fullenrich_enrich_linkedin`/
    # `fullenrich_result`, born from signal #252 (an in-process poll of 131-147 s
    # survived no MCP client, and the credits were already spent).
    # ------------------------------------------------------------------

    # `webhook_url` is REMOVED from the served schema but still ACCEPTED
    # (`exclude_args`): a procedure written before this batch still passes it, and
    # refusing it ("Unexpected keyword argument") would break a reveal that worked.
    # It is IGNORED — Apollo's URL is ours — and the response says so.
    # ⚠️ `exclude_args` has been deprecated since FastMCP 2.14; the day it disappears,
    # `test_apollo_receveur.py` goes red on the call that still carries it.
    _WEBHOOK_URL_RETIREE = (
        "`webhook_url` is no longer used and was ignored: oto receives Apollo's "
        "numbers itself. Collect them with apollo_reveal_phone_result.")
    # The choice is MADE and written here: the deprecation warning, emitted on
    # each mount, would say nothing more and would drown out the others. Filter to the WORD
    # — any other FastMCP warning stays visible.
    warnings.filterwarnings("ignore", message=r"The `exclude_args` parameter is deprecated",
                            category=DeprecationWarning)

    def _avec_avis(out: dict, webhook_url: Optional[str]) -> dict:
        return {**out, "deprecation": _WEBHOOK_URL_RETIREE} if webhook_url else out

    @mcp.tool(exclude_args=["webhook_url"])
    def apollo_reveal_phone(
        person_id: Optional[str] = None,
        linkedin_url: Optional[str] = None,
        email: Optional[str] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        name: Optional[str] = None,
        domain: Optional[str] = None,
        org_name: Optional[str] = None,
        full: bool = False,
        webhook_url: Optional[str] = None,
    ) -> dict:
        """Order someone's phone numbers, mobile and direct dial included (ASYNC).

        Same identifiers as apollo_match_person — the surest is `person_id` from
        apollo_search_people.

        ⚠️ THE NUMBERS ARE NOT IN THIS RESPONSE. Apollo delivers them to oto minutes
        later; what comes back here is a `request_id`. Pass it to
        apollo_reveal_phone_result — free, kept 30 days — and KEEP IT where the next
        agent will look (a datastore row, the run journal). Lose it and the credits
        are spent with nothing to collect.

        ⚠️ Your own Apollo key only: a reveal costs ~9 Apollo credits where a plain
        match costs 1, so it never runs on the shared platform key.

        Args:
            full: keep the employer's tech stack, the employment history and the
                Apollo CRM account record. Off by default: a real reveal came back
                at 65 374 characters and the client refused it outright. Same price.
        """
        cle = _cle_byo(_BYO_REVEAL_TELEPHONE)
        client = ApolloClient(api_key=cle.key)
        # The order is born BEFORE the call: Apollo may POST before handing control
        # back to us. If it cannot be born, nothing is paid yet — we raise.
        jeton, destination = apollo_receiver.commander(cle)
        try:
            out = client.match_person(
                person_id=person_id, linkedin_url=linkedin_url, email=email,
                first_name=first_name, last_name=last_name, name=name,
                domain=domain, org_name=org_name,
                reveal_phone_number=True, webhook_url=destination) or {}
        except ValueError as e:
            apollo_receiver.abandonner(jeton)
            raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))
        except BaseException:
            apollo_receiver.abandonner(jeton)
            raise

        # ⚠️ APOLLO FOUND NOBODY ≠ APOLLO ACCEPTED THE REVEAL. The oto-core client
        # translates the 404 into `None` (and a body can come back without
        # `person`): without this branch, both cases fell into the same
        # "accepted, but without request_id" — a lie in the most
        # costly direction, the one that REASSURES. The agent would wait for a POST that will
        # never leave, treat the lead as handled, and not retry with a
        # stronger identifier. Same rule as everywhere here: not found says
        # "not found", never a silent fallback.
        #
        # ⚠️ And this refusal asserts NOTHING about cost. Apollo bills the CALL, not the
        # result: it bills the empty shell it mints itself (~12
        # credits for zero data, oto-core `44acc08`), and nothing has ever shown
        # that a non-match is refunded. Announcing "no credit spent" then
        # "retry" presented a SECOND billed call as free — the text served
        # drives the agent, and that one pushed it to pay again believing it was making up
        # for a free error. Here we do not measure billing, so we do not
        # promise: we say it is not free and we quantify the retry.
        if not out.get("person"):
            apollo_receiver.abandonner(jeton)
            return _avec_avis({"matched": False, "next_step": (
                "No Apollo match for these identifiers: no numbers will ever arrive "
                "— apollo_reveal_phone_result will have nothing to collect. Do "
                "NOT read that as free: Apollo bills the CALL, not the result (it "
                "charges for the empty records it mints itself), and nothing has "
                "ever shown a non-match to be refunded. Trying again is a SECOND "
                "billed call — only worth it with a genuinely stronger identifier "
                "(person_id from apollo_search_people), never the same one again.")},
                webhook_url)

        # `request_id` is a signed 64-bit integer (~7.2e17): it EXCEEDS the
        # precision of a JavaScript number (2^53), and a tool's response
        # travels as JSON all the way to clients made of it. Returning it as a
        # number is making it wrong by one or two units without anything saying so —
        # and a wrong id polls nothing. We serve it as a STRING.
        rid = out.get("request_id")
        result = {k: v for k, v in out.items() if k != "request_id"}
        if rid is None:
            # Apollo did return a person, but no id: the numbers
            # will arrive at oto, but nothing makes it possible to designate them. We say so —
            # a `next_step` that promises an unusable tool is worse than no
            # `next_step` at all.
            result["next_step"] = (
                "Apollo matched this person and accepted the reveal, but returned "
                "no request_id: the numbers cannot be looked up without it. "
                "Nothing to poll.")
            return _avec_avis(result if full else _light_reveal(result), webhook_url)
        apollo_receiver.lier(jeton, rid)
        result["request_id"] = str(rid)
        result["next_step"] = (
            f"Reveal ordered. Call apollo_reveal_phone_result('{rid}') in ~1-2min "
            "(0 Apollo credits per check, result kept 30 days).")
        return _avec_avis(result if full else _light_reveal(result), webhook_url)

    @mcp.tool()
    def apollo_reveal_phone_result(
        request_id: str,
        full: bool = False,
        datastore: Optional[Adresse] = None,
        row_id: Optional[str] = None,
        match_column: Optional[str] = None,
        phone_column: str = "phone",
    ) -> dict:
        """Collect the numbers ordered with apollo_reveal_phone or apollo_bulk_match.
        0 Apollo credits. Readable through the Apollo key that paid for the reveal:
        whoever reaches the connector with that same key, nobody else.

        `done: false` carries `retry_after_seconds` — wait that long, call again.
        When done, the numbers are at `result.webhook_result.people[].phone_numbers[]`
        (nested: `result` is Apollo's envelope). An order Apollo closed without
        numbers comes back as `delivered: false` with its `reason` (`out_of_credits`
        when the account ran dry): nothing more will arrive for it. Each number carries `sanitized_number`, `type_cd`
        ("mobile"/"work_direct") and `dnc_status_cd` (do-not-call: read it before
        dialling). Kept 30 DAYS, then gone.

        With `datastore`, numbers go straight into your table (first mobile in
        `phone_column`, type and `dnc_status_cd` in its `comment`); only counts return.

        Args:
            request_id: the id from apollo_reveal_phone, AS A STRING — a signed
                64-bit integer, too large for a JSON number to carry exactly.
            full: keep, on each `people[]` record, the employer's tech stack, the
                employment history and the Apollo CRM account record. Off by
                default (same cut as apollo_reveal_phone): ~15 000 characters per
                person otherwise, and a batch of 50 fits in no context. Numbers
                and person identity are kept either way. Still 0 credits.
            datastore: write the numbers into this table (its NUMBER, `ns_id` —
                a name is refused) and return counts only.
            row_id: with `datastore`, the row of the ONE person revealed.
            match_column: with `datastore`, for a batch: the column holding each
                person's Apollo id — every row whose value matches gets the number.
            phone_column: with `datastore`, the column the number is written to.
        """
        # Connector access FIRST: reading a received reveal resolves the Apollo key
        # that paid for it, and nobody else (`apollo_receiver.portee`). Then what
        # Apollo delivered TO US, with no call; Apollo polling is only the fallback
        # — POST not yet arrived, refused, or order placed before this batch.
        cle = _cle_byo(_BYO_REVEAL_TELEPHONE)
        result = apollo_receiver.resultat_recu(request_id, cle)
        if result is None:
            client = ApolloClient(api_key=cle.key)
            try:
                out = client.poll_webhook_result(request_id)
            except ValueError as e:
                raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))
            if not out.get("done"):
                wait = out.get("retry_after_seconds")
                return {
                    "done": False,
                    "retry_after_seconds": wait,
                    "next_step": ("Still verifying — call apollo_reveal_phone_result "
                                  f"again in ~{wait or 10}s."),
                }
            result = out.get("result") or {}
        # ⚠️ DONE IS NOT DELIVERED. Apollo closes an order it could not honour with a
        # `failure_reason` — "you ran out of mobile number credits" on an empty
        # account (signal #1283) — and an envelope with no number. Served as is, the
        # reason sat nested under `result` while the response opened on `done: true`:
        # an agent reads "finished", finds no number, and orders again. We say it at
        # the top, with what to do.
        echec = (result.get("failure_reason") or "").strip() if isinstance(
            result.get("failure_reason"), str) else ""
        if echec:
            a_sec = "credit" in echec.lower()
            return {"done": True, "delivered": False,
                    "reason": "out_of_credits" if a_sec else "not_delivered",
                    "failure_reason": echec,
                    "next_step": (
                        "Apollo closed this order WITHOUT numbers: the Apollo account "
                        "behind your key is out of credits. Nothing more will arrive "
                        "for this request_id; do not order again until the account "
                        "is topped up with Apollo." if a_sec else
                        "Apollo closed this order WITHOUT numbers (reason above). "
                        "Nothing more will arrive for this request_id.")}
        if datastore is not None:
            return {"done": True, "written": apollo_receiver.ecrire_numeros(
                result, datastore=datastore, row_id=row_id,
                match_column=match_column, phone_column=phone_column)}
        # ⚠️ Apollo RE-ECHOES the identifier in its envelope, as a NUMBER — and it
        # therefore arrives damaged, as everywhere else. Measured on a real poll in
        # production on 2026-09-12: polled with `-8351464734221602674`, the envelope
        # returned `-8351464734221603000` — 326 off, the signature of float64.
        # `_stringify_request_id` only covered the TOP level of the `match`/`reveal`
        # responses; the poll's nested echo escaped it. An agent that re-reads
        # `result.request_id` (to re-poll later, or to store it in a table row)
        # stores an identifier that polls nothing.
        # Third time the same trap shows up at a different level: it
        # is closed where the value LEAVES, not where we saw it last time.
        result = _stringify_request_id(result)
        if full:
            return {"done": True, "result": result}
        result, bloc = _light_reveal_result(result)
        return ({"done": True, "result": result, "projection": bloc} if bloc
                else {"done": True, "result": result})

    _BYO_REVEAL_LOT = (
        "a batch that REVEALS (personal emails or phones) never goes through "
        "the platform key: Apollo bills these reveals on top of the match, and per "
        "PERSON — set your own Apollo key. Without it, the batch still works, "
        "it simply returns the records without these reveals.")

    @mcp.tool(exclude_args=["webhook_url"])
    def apollo_bulk_match(
        people: list[dict],
        reveal_personal_emails: bool = False,
        reveal_phone_number: bool = False,
        full: bool = False,
        webhook_url: Optional[str] = None,
    ) -> dict:
        """Match UP TO 10 people in one call — the way a list actually gets built.

        apollo_search_people returns hundreds of people with obfuscated last names
        and no email. This is how you resolve them: 10 per call instead of one, so
        300 people cost 30 calls, not 300.

        ⚠️ A LOT DOES NOT SAVE CREDITS — Apollo bills PER PERSON, exactly as if you
        had called apollo_match_person ten times. What it saves is calls, and the
        rate limit that comes with them. Ask only for the reveals you need.

        `matches` comes back one entry PER PERSON, in the order you sent them, with
        `null` where nothing matched. An entry carrying `_stub: true` is an empty
        record Apollo minted and CHARGED for — count it as a failure, not as data.

        Each match drops `employment_history`, the Apollo CRM `account` record and
        the employer's heaviest blocks by default — a lot of 10 overflowed MCP output
        limits. `projection` names them; `full=True` returns everything.

        ⚠️ Phone numbers are not in this response: with `reveal_phone_number` Apollo
        delivers them to oto minutes later and hands back a `request_id` — pass it
        to apollo_reveal_phone_result. Either reveal needs your own Apollo key; a
        plain match works on the shared one.

        Args:
            people: 1-10 entries. Each takes the same identifiers as
                apollo_match_person — `id` (surest, from apollo_search_people),
                `email`, `linkedin_url`, or a FULL name (`first_name` +
                `last_name`) with `domain`/`organization_name`. A weak entry is
                refused by INDEX before the whole lot is billed.
            reveal_personal_emails: also return PERSONAL emails (your own key).
            reveal_phone_number: order phone numbers (your own key); they are
                collected with apollo_reveal_phone_result, not returned here.
            full: return every match untouched (employment history, CRM account,
                employer tech stack). Same price — this is about size.
        """
        revele = bool(reveal_personal_emails) or bool(reveal_phone_number)
        cle = None
        if revele:
            cle = _cle_byo(_BYO_REVEAL_LOT)
            client, is_platform = ApolloClient(api_key=cle.key), False
        else:
            client, is_platform = _client(units=len(people))
        # The receiving URL is OURS, and only when phones are
        # ordered (Apollo refuses a `webhook_url` without them). Born before the call,
        # under the key that pays.
        jeton = destination = None
        if reveal_phone_number:
            jeton, destination = apollo_receiver.commander(cle)
        try:
            out = client.bulk_match_people(
                people,
                reveal_personal_emails=reveal_personal_emails or None,
                reveal_phone_number=reveal_phone_number or None,
                webhook_url=destination) or {}
        except ValueError as e:
            if jeton:
                apollo_receiver.abandonner(jeton)
            raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))
        except BaseException:
            if jeton:
                apollo_receiver.abandonner(jeton)
            raise

        # What APOLLO billed: its response carries `credits_consumed` (0 if nothing
        # billable was found, no credit for a person without a
        # match; +8 for a mobile). It is the figure of BOTH counters
        # below — never 1 for the call, never `len(people)` when upstream states
        # its own (oto#168). Silent response: we count what we submitted, the safest
        # for the quota, but SAYING SO in the log — a silent fallback would make
        # an unexpected response pass for a correct count.
        credits = out.get("credits_consumed")
        if not isinstance(credits, int) or isinstance(credits, bool) or credits < 0:
            logger.warning("apollo_bulk_match: `credits_consumed` missing or unreadable "
                           "(%r) in Apollo's response — quota and quantity counted "
                           "on the %d people submitted", credits, len(people))
            credits = len(people)
        # The common key's quota, in the credits deducted: 0 debits nothing.
        if is_platform:
            session_org.note_call_trace(units=credits)
            quota = access.platform_quota_hint("apollo")
            if quota is not None:
                out = {**out, "platform_quota": quota}
        # The BILLED line (`tool_calls.quantity`, read by the usage lens and by a
        # partner's biller) is ANOTHER counter than the quota above.
        # Unconditional, common key OR own, like `fullenrich`: it is
        # `key_mode`, set by the resolver, that says whether there is anything to bill.
        # Without it `quantity` stays NULL, which the consumer reads as 1. Its unit is the
        # Apollo CREDIT (oto#168), as `serper` carries its credits: a traced 0 says
        # "nothing billed", not "not measured".
        session_org.note_call_trace(quantity=credits)

        out = _stringify_request_id(out)
        matches = out.get("matches")
        if not full and isinstance(matches, list):
            allegees = [_light_match(m) for m in matches]
            if any(flag for _, flag in allegees):
                out = {**out, "matches": [m for m, _ in allegees],
                       "projection": _projection_bloc(lot=True)}
        if reveal_phone_number:
            rid = out.get("request_id")
            if rid:
                apollo_receiver.lier(jeton, rid)
            out["next_step"] = (
                f"Phone reveal ordered for {len(people)} people. Call "
                f"apollo_reveal_phone_result('{rid}') in ~1-2min."
                if rid else
                "Apollo accepted the reveal but returned no request_id: the numbers "
                "cannot be looked up without it. Nothing to poll.")
        return _avec_avis(out, webhook_url)

    @mcp.tool(annotations=LECTURE)
    def apollo_job_postings(org_id: str) -> dict:
        """List active job postings for an Apollo organization id (hiring signal)."""
        client, _ = _client()
        return client.get_job_postings(org_id)

    # ------------------------------------------------------------------
    # Contacts — the data boundary shifts HERE. Everything above
    # queries Apollo's SHARED database (`mixed_*`, `people/match`,
    # `organizations/*`); a CONTACT is a person saved in the
    # workspace of the KEY OWNER, with the values THEIR
    # team wrote there (stage, owner, lists, custom fields).
    # Same rule as sequences and emails, therefore: `_client_byo()` on
    # ALL THREE ops, including the two reads. A pooled platform
    # key would return someone else's address book here.
    #
    # The three endpoints cost 0 credit — this is precisely what makes
    # `op="get"` useful: re-reading a contact we already own has no
    # reason to pay again the credit of `apollo_match_person`.
    # ------------------------------------------------------------------

    # Excluded from the default view of the field catalog: CRM sync and
    # display plumbing. Named DENYLIST, never an allowlist — a key that Apollo
    # might add tomorrow must stay visible, not vanish silently
    # (lesson `fr_get`/`liste_idcc`, docs/conventions.md).
    _FIELD_NOISE = (
        "finder_view_ids", "finder_views", "icon_class", "project_workspace_id",
        "mapped_crm_field", "additional_mapped_crm_field",
        "is_readonly_mapped_crm_field", "picklist_options_last_synced_at",
        "picklist_value_set_id", "context", "group", "meta", "parent",
    )

    # LIST view of `op="search"`: two NESTED blocks that Apollo copies into
    # EACH record and that, at 25 rows, weigh more than everything else combined. What
    # is useful for choosing (`organization_name`, `account_id`, `title`, `email`,
    # `typed_custom_fields`) stays — and `full=True` returns the raw. Named DENYLIST:
    # a key that Apollo might add tomorrow stays visible (lesson `fr_get`).
    _CONTACT_NOISE = ("organization", "account")

    # What a 422 means DEPENDS on the op, and teaching the wrong lesson is worse than
    # saying nothing: on a read it can only designate the id; on a PATCH it
    # equally designates a refused VALUE (a nonexistent stage, a malformed
    # date). Serving "this is not a contact id" to someone who just wrote
    # a bad value sends them looking in the wrong place.
    _WRONG_ID_422 = (
        "Apollo cannot find this contact in your workspace (nonexistent, "
        "deleted, or belonging to another team). ⚠️ An id returned by "
        "apollo_search_people/apollo_match_person is a PERSON id from the "
        "shared database, NOT a contact id: a person your team never "
        "saved has no contact here.")
    _REFUSED_WRITE_422 = (
        "Apollo refused this modification. Two possible causes, and the message "
        "above settles it: either a VALUE is invalid (unknown contact_stage_id, "
        "malformed date, nonexistent picklist option), or "
        "`contact_id` does not designate a contact of your workspace — an id "
        "from apollo_search_people is a PERSON id, not a contact id.")

    def _contact_run(fn, *, on_422: str = _WRONG_ID_422):
        """Translates the two PREDICTABLE refusals of this family into an actionable error.

        The module has no global error table (ApolloError bubbles up as is,
        upstream message included) and that is fine for search. Here two statuses
        have a precise cause, which the upstream message does not state:

        - **403** = non-Master Apollo key. This is the NORMAL case of a scoped key, not
          an outage — and "Apollo 403 on contacts/…" teaches nothing to whoever does not
          know these endpoints have this prerequisite.
        - **422** = see `on_422`, which depends on the op (read vs write).

        Everything else bubbles up INTACT: Apollo's upstream message names the
        refused field, and that is what makes a 400 fixable.
        """
        try:
            return fn()
        except ValueError as e:
            raise _bad(str(e))
        except ApolloError as e:
            if e.status_code == 403:
                raise _bad(
                    f"{e} — these endpoints (custom fields, reading and writing "
                    "a contact) require an Apollo **Master** key, or the matching named "
                    "scope. A standard Apollo key authenticates but "
                    "returns 403 here. Regenerate it as Master in Apollo → Settings → "
                    "Integrations → API.")
            if e.status_code == 422:
                raise _bad(f"{e} — {on_422}")
            raise

    def _custom_fields() -> dict:
        """Catalog of the custom fields of THIS Apollo team (0 credit).

        ⚠️ Goes through `typed_custom_fields`, which Apollo marks deprecated in favor
        of `GET /fields` — **deliberately**. The two do not return the same id
        shape: this one returns the BARE ObjectId, the exact key that `PATCH /contacts`
        expects; `/fields` returns an id PREFIXED with its modality
        (`"account.6940…"`), which no doc allows us to split. The "modern"
        catalog would therefore make us write keys that Apollo ignores while returning 200.
        """
        return _client_byo().list_typed_custom_fields() or {}

    def _field_index(catalog: Any) -> Optional[dict]:
        """`{id: definition}` of the catalog's fields — **`None` if the shape
        surprises**, `{}` if the team declares none.

        The two are not equivalent and confusing them is costly both ways.
        Unreadable shape = we know NOTHING: refusing would block a
        legitimate write at the first Apollo change. Catalog read and empty = we know
        the id sent does not exist: letting it through means letting Apollo
        swallow the write while returning 200."""
        rows = catalog.get("typed_custom_fields") if isinstance(catalog, dict) else None
        if not isinstance(rows, list):
            return None
        return {r["id"]: r for r in rows
                if isinstance(r, dict) and isinstance(r.get("id"), str)}

    def _is_contact_field(definition: dict) -> bool:
        """A field with no declared `modality` is treated as a contact field
        — same permissive default everywhere, otherwise the list of "valid" ids and the
        check that refuses do not speak about the same set."""
        return (definition.get("modality") or "contact") == "contact"

    def _check_custom_field_ids(values: dict) -> Optional[str]:
        """Refuses a field id that this team does not declare — naming the valid
        ids, otherwise the agent retries at random.

        Returns a NOTE when validation could not take place (unreadable
        catalog), never None silently: `GET typed_custom_fields` requires a Master
        key and returns 403 otherwise, so "not validated" is the NORMAL case
        of a scoped key — and a write that claims to be verified without being so is
        worse than no verification at all.
        """
        try:
            index = _field_index(_custom_fields())
        except McpError:
            raise
        # noqa: SILENT — the "ids not verified" warning is returned to the agent
        except Exception as e:  # noqa: BLE001 — the catalog is a CONVENIENCE, not a lock
            return (f"ids not verified: the field catalog could not be read "
                    f"({type(e).__name__}: {e}). `GET typed_custom_fields` requires "
                    "an Apollo Master key; the write itself went out as "
                    "is — re-read the contact with op=\"get\" to see what was "
                    "actually saved.")
        if index is None:
            return ("ids not verified: the field catalog does not have the expected "
                    "shape (Apollo may have changed it). The write went out as "
                    'is — re-read the contact with op="get" to verify it.')

        def _describe(i: str) -> str:
            d = index[i]
            return f'{i} ("{d.get("name") or d.get("label")}")'

        unknown = sorted(set(values) - set(index))
        if unknown:
            valid = [{"id": i, "name": d.get("name") or d.get("label"),
                      "type": d.get("type")}
                     for i, d in index.items() if _is_contact_field(d)]
            raise _bad(
                f"custom fields unknown to this Apollo team: {unknown}. "
                f"Valid CONTACT fields: {valid or 'none'}. "
                "`typed_custom_fields` is keyed by ID (not by name) — read them with "
                'apollo_contact(op="fields").')

        misfiled = sorted(i for i in values if not _is_contact_field(index[i]))
        if misfiled:
            detail = [f'{_describe(i)} → {index[i].get("modality")}' for i in misfiled]
            raise _bad(
                f"these fields do not belong to the contact object: {detail}. "
                "A custom field is attached to ONE Apollo object; set on a "
                "contact it is not \"almost right\", it is ignored without an error.")

        # A picklist only accepts the ID of one of its options. Sending the
        # LABEL is the trap that the tool description names as "the only
        # error Apollo swallows silently" — the catalog we just read
        # already carries what is needed to refuse it, not using it would be documenting it
        # without closing it. We only check WHAT the catalog declares: a
        # picklist whose options are absent is not checked, not refused.
        wrong: list[str] = []
        for i, value in values.items():
            d = index[i]
            if d.get("type") not in ("picklist", "multi_select"):
                continue
            opts = d.get("picklist_values")
            if not isinstance(opts, list) or not opts:
                continue
            ids = {o.get("id") for o in opts if isinstance(o, dict)}
            if not ids:
                continue
            given = value if isinstance(value, list) else [value]
            off = [v for v in given if v is not None and v not in ids]
            if off:
                names = [{"id": o.get("id"), "name": o.get("name")}
                         for o in opts if isinstance(o, dict)]
                wrong.append(f'{_describe(i)}: {off} — valid options {names}')
        if wrong:
            raise _bad(
                "invalid picklist values: " + " ; ".join(wrong) + ". "
                "An Apollo picklist is written with the option's `id`, never with "
                "its label — the label is apparently accepted then ignored.")
        return None

    @mcp.tool()
    def apollo_contact(
        op: Literal["fields", "create_field", "search", "get", "update"],
        contact_id: Optional[str] = None,
        label: Optional[str] = None,
        field_type: str = "string",
        max_length: Optional[int] = None,
        q_keywords: Optional[str] = None,
        contact_stage_ids: Optional[list[str]] = None,
        contact_label_ids: Optional[list[str]] = None,
        sort_by_field: Optional[str] = None,
        sort_ascending: Optional[bool] = None,
        per_page: int = 25,
        page: int = 1,
        typed_custom_fields: Optional[dict] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        title: Optional[str] = None,
        email: Optional[str] = None,
        organization_name: Optional[str] = None,
        account_id: Optional[str] = None,
        website_url: Optional[str] = None,
        label_names: Optional[list[str]] = None,
        contact_stage_id: Optional[str] = None,
        present_raw_address: Optional[str] = None,
        direct_phone: Optional[str] = None,
        corporate_phone: Optional[str] = None,
        mobile_phone: Optional[str] = None,
        home_phone: Optional[str] = None,
        other_phone: Optional[str] = None,
        modality: Literal["contact", "account", "opportunity", "all"] = "contact",
        dry_run: bool = False,
        full: bool = False,
    ) -> dict:
        """Read and edit a CONTACT — a person saved in YOUR Apollo workspace, with
        the values your team wrote on them. BYO key only, all three ops, and all
        three cost 0 Apollo credits.

        ⚠️ A CONTACT IS NOT A PERSON, AND THE TWO IDS ARE DIFFERENT OBJECTS.
        `apollo_search_people` returns PERSON ids, which every op here REJECTS. Two
        things hand you a real contact id: `apollo_match_person`, nested at
        `person.contact.id` — present once that person is a contact of your
        workspace, and free because you already paid for the match — and op="search"
        below. Never pass a person id: it fails, it does not fall back.

        ⚠️ op="get" IS THE CHEAP WAY TO READ SOMEONE BACK. `apollo_match_person`
        costs a credit and returns the SHARED record — not your team's stage, owner,
        lists or custom values. To re-read a contact you already own, use this.

        ⚠️ CUSTOM FIELDS ARE KEYED BY ID, NEVER BY NAME. `typed_custom_fields` looks
        like `{"60c39ed82bd02f01154c470a": "2026-08-07"}` — call op="fields" FIRST to
        get the ids. Ids are validated against your team's catalogue before the write,
        and an unknown one is refused with the valid ids named.

        ⚠️ FOR A PICKLIST FIELD, THE VALUE IS THE OPTION'S ID, NOT ITS LABEL. op="fields"
        returns `picklist_values` for those — send `picklist_values[].id`. Sending the
        human label is the one mistake Apollo swallows silently.

        ⚠️ A NEW FIELD MEANT TO HOLD A SENTENCE MUST BE `field_type="textarea"`.
        `string` is length-capped (120 characters by default) and Apollo truncates
        past it without complaining — a personalised opener would arrive cut mid-word.

        ⚠️ THE THREE ENDPOINTS NEED AN APOLLO **MASTER** API KEY (or the matching
        scope) and answer 403 otherwise. When op="fields" cannot be read, op="update"
        still writes — but it says so in `field_validation`, it never pretends the ids
        were checked.

        ⚠️ `label_names` REPLACES list membership instead of adding to it — sending
        one list removes the contact from every other. Every other field is a true
        PATCH: what you omit is left untouched.

        Args by op:
        - `fields`: the custom field definitions of your team — `id` (the bare id the
          write expects), `name`, `type`, `modality` and, for picklists,
          `picklist_values`. `modality` filters which object's fields you get
          (default "contact"; "account", "opportunity", or "all" for every object).
          `full=True` returns the raw catalogue instead of the projected one.
        - `create_field`: declare a NEW custom field — the API equivalent of Apollo's
          Settings → Custom Fields, for when you have no access to that UI. `label`
          (required), `field_type` (`string`, `textarea`, `number`, `date`,
          `datetime`, `boolean`), `max_length`, `modality`. Returns the field with its
          id, ready to use as a `typed_custom_fields` key. `dry_run=True` echoes
          without creating. A SETUP gesture — run it once per field, not per lead.
        - `search`: find the contact ids you need. `q_keywords` (name, title,
          employer or email), `contact_stage_ids`, `contact_label_ids`,
          `sort_by_field` (`contact_last_activity_date`,
          `contact_email_last_opened_at`, `contact_email_last_clicked_at`,
          `contact_created_at`, `contact_updated_at`) + `sort_ascending`,
          `per_page` (Apollo caps at 100), `page` (Apollo stops at 500 pages —
          past 50 000 records, filter instead of paging). Searches YOUR saved
          contacts only, never Apollo's database.
        - `get`: `contact_id` (required). Returns the contact and its labels.
        - `update`: `contact_id` (required) + at least one field among
          `typed_custom_fields`, `first_name`, `last_name`, `title`, `email`,
          `organization_name`, `account_id`, `website_url`, `label_names`,
          `contact_stage_id`, `present_raw_address`, `direct_phone`,
          `corporate_phone`, `mobile_phone`, `home_phone`, `other_phone`.
          `dry_run=True` validates the custom field ids and echoes the exact payload
          without writing.
        """
        if op not in ("fields", "create_field", "search", "get", "update"):
            raise _bad(f'unknown op "{op}" — expected: fields, create_field, search, '
                       'get, update')

        if op == "fields":
            catalog = _contact_run(_custom_fields)
            if full:
                return catalog
            index = _field_index(catalog)
            if index is None:
                raise _bad(
                    "the custom field catalog does not have the expected shape "
                    f"(Apollo may have changed it) — raw: {str(catalog)[:400]}")
            rows = [r for r in index.values()
                    if modality == "all"
                    or (r.get("modality") or "contact") == modality]
            return {
                "fields": output_projection.project(
                    {"fields": rows}, items_path="fields",
                    item_drop=_FIELD_NOISE)["fields"],
                "count": len(rows),
                "modality": modality,
                "projection": {
                    "dropped": list(_FIELD_NOISE),
                    "filtered_on": f"modality={modality}",
                    "how_to_get_all_columns": "full=True (returns the raw catalog)",
                    "how_to_get_all_objects": 'modality="all"',
                },
                "how_to_use": ('the `id`s above are the keys of '
                               '`typed_custom_fields` on op="update"'),
            }

        if op == "create_field":
            if not (label or "").strip():
                raise _bad('op=create_field: `label` required (the name of the field).')
            # Apollo does NOT deduplicate on the label: a second call creates a
            # second field with the same name, without signaling anything. Both show up in the
            # catalog, a sequence variable designates ONE, and writes
            # aimed at the other appear nowhere. We look first — and
            # if the catalog is unreadable (non-Master key), we do not block: we SAY
            # so, as everywhere else here.
            existing, dup_note = [], None
            try:
                index = _field_index(_custom_fields())
                if index is None:
                    dup_note = ("duplicates not verified: unreadable catalog.")
                else:
                    existing = [
                        {"id": i, "name": d.get("name") or d.get("label"),
                         "type": d.get("type")}
                        for i, d in index.items()
                        if (d.get("name") or d.get("label")) == label
                        and (d.get("modality") or "contact") == modality]
            except McpError:
                raise
            # noqa: SILENT — the "duplicates not verified" warning is returned to the agent
            except Exception as e:  # noqa: BLE001
                dup_note = (f"duplicates not verified: the catalog could not be "
                            f"read ({type(e).__name__}: {e}).")
            if existing:
                raise _bad(
                    f'a field "{label}" already exists on this object: {existing}. '
                    "Apollo would create a SECOND one, with the same name, that nothing distinguishes — "
                    "and a write aimed at the wrong one would appear nowhere. "
                    "Reuse the id above, or choose another label.")
            if dry_run:
                out = {"dry_run": True, "action": "create_field", "label": label,
                       "modality": modality, "field_type": field_type,
                       "max_length": max_length}
                if dup_note:
                    out["field_validation"] = dup_note
                return out
            created = _contact_run(lambda: _client_byo().create_custom_field(
                label=label, modality=modality, field_type=field_type,
                max_length=max_length))
            if dup_note and isinstance(created, dict):
                created = {**created, "field_validation": dup_note}
            return created

        if op == "search":
            found = _contact_run(lambda: _client_byo().search_contacts(
                q_keywords=q_keywords, contact_stage_ids=contact_stage_ids,
                contact_label_ids=contact_label_ids, sort_by_field=sort_by_field,
                sort_ascending=sort_ascending, per_page=per_page, page=page))
            if full or not isinstance(found, dict):
                return found
            out = output_projection.project(
                found, items_path="contacts", item_drop=_CONTACT_NOISE)
            out["projection"] = {
                "dropped": list(_CONTACT_NOISE),
                "why": ("two nested blocks that weigh more than the whole record; "
                        "`organization_name` and `account_id` stay, enough to "
                        "link without reloading them"),
                "how_to_get_everything": "full=True, or op=\"get\" on an id",
            }
            return out

        if not contact_id:
            raise _bad(f"contact_id required for op={op}")

        if op == "get":
            return _contact_run(lambda: _client_byo().get_contact(contact_id))

        payload: dict[str, Any] = {
            k: v for k, v in (
                ("first_name", first_name), ("last_name", last_name),
                ("title", title), ("email", email),
                ("organization_name", organization_name),
                ("account_id", account_id), ("website_url", website_url),
                ("label_names", label_names),
                ("contact_stage_id", contact_stage_id),
                ("present_raw_address", present_raw_address),
                ("direct_phone", direct_phone), ("corporate_phone", corporate_phone),
                ("mobile_phone", mobile_phone), ("home_phone", home_phone),
                ("other_phone", other_phone),
                ("typed_custom_fields", typed_custom_fields),
            # `{}` and `[]` are discarded like `None`: an EMPTY `typed_custom_fields`
            # expresses no modification, and letting it through produced
            # a no-op PATCH returned as a successful write.
            ) if v is not None and v != {} and v != []
        }
        if not payload:
            raise _bad(
                "op=update: no field to modify — pass at least one field "
                '(typed_custom_fields, title, email, contact_stage_id…). Custom field '
                'ids are read with apollo_contact(op="fields").')
        if typed_custom_fields is not None and not isinstance(typed_custom_fields, dict):
            raise _bad("typed_custom_fields must be an object {field_id: value}, "
                       'keyed by the ids returned by apollo_contact(op="fields").')

        note = _check_custom_field_ids(typed_custom_fields) if typed_custom_fields else None

        if dry_run:
            out = {"dry_run": True, "action": "update", "contact_id": contact_id,
                   "payload": payload}
            if note:
                out["field_validation"] = note
            return out
        result = _contact_run(
            lambda: _client_byo().update_contact(contact_id, **payload),
            on_422=_REFUSED_WRITE_422)
        if not note:
            return result
        # The note must not depend on the SHAPE of Apollo's return: "I could not
        # verify" is information about the call, not about the response.
        return ({**result, "field_validation": note} if isinstance(result, dict)
                else {"result": result, "field_validation": note})

    # ------------------------------------------------------------------
    # Email accounts & schedules — read prerequisite, 0 credit, but BYO
    # ONLY: the list returns the KEY OWNER's mailboxes/schedules
    # (HTML signature, deliverability score, daily thresholds...) — the platform
    # key belongs to someone else, its mailbox has no business here.
    # ------------------------------------------------------------------

    @mcp.tool()
    def apollo_email_accounts() -> dict:
        """List the mailboxes connected to THIS Apollo account (BYO key only —
        this is the key owner's own inbox configuration, never the platform key's).

        Get the `id` to pass as `send_email_from_email_account_id` to
        apollo_sequence_contacts(op="add").
        """
        client = _client_byo()
        return client.list_email_accounts()

    @mcp.tool()
    def apollo_email_schedules() -> dict:
        """List the send schedules configured on THIS Apollo team (BYO key only).

        Get the `id` to pass as `emailer_schedule_id` to
        apollo_sequence(op="create") — required, creation fails without it.
        """
        client = _client_byo()
        return client.list_email_schedules()

    # ------------------------------------------------------------------
    # Sequences
    # ------------------------------------------------------------------

    @mcp.tool()
    def apollo_sequence(
        op: Literal["search", "create", "update", "activate", "deactivate", "archive"],
        sequence_id: Optional[str] = None,
        name: Optional[str] = None,
        emailer_schedule_id: Optional[str] = None,
        active: Optional[bool] = None,
        label_names: Optional[list[str]] = None,
        folder_id: Optional[str] = None,
        max_emails_per_day: Optional[int] = None,
        cc_emails: Optional[str] = None,
        bcc_emails: Optional[str] = None,
        emailer_steps: Optional[list[dict]] = None,
        per_page: int = 25,
        page: int = 1,
        dry_run: bool = False,
    ) -> dict:
        """Manage Apollo sequences (search/create/update/activate/deactivate/archive).

        Args by op:
        - `search`: `name` (partial match), `per_page`, `page`. BYO key only — this
          lists the key owner's OWN sequences (names + open/reply rates).
        - `create`: `name` + `emailer_schedule_id` REQUIRED (get one from
          apollo_email_schedules — the API refuses creation without it). Optional
          `active` (default False — prefer op="activate" once steps/templates are
          reviewed), `label_names`, `folder_id`, `max_emails_per_day`, `emailer_steps`
          (nested step/template definitions — see Apollo docs). BYO key only.
        - `update`: `sequence_id` + any of `name`, `active`, `emailer_schedule_id`,
          `label_names`, `max_emails_per_day`, `cc_emails`, `bcc_emails`,
          `emailer_steps` (include `id` per step to MODIFY it, omit to CREATE one).
          BYO key only.
        - `activate` / `deactivate`: `sequence_id`. BYO key only.
        - `archive`: `sequence_id`. BYO key only. Supports `dry_run=True` (no
          get-by-id exists on this endpoint — the preview cannot show a real diff,
          just echoes the intended action).

        `dry_run=True` on `create`/`update`/`activate`/`deactivate`/`archive`
        validates but skips the actual API call.
        """
        if op not in ("search", "create", "update", "activate", "deactivate", "archive"):
            raise _bad(f'unknown op "{op}" — expected: search, create, update, activate, '
                       'deactivate, archive')

        client = _client_byo()
        if op == "search":
            return client.search_sequences(name=name, per_page=per_page, page=page)

        try:
            if op == "create":
                if not name or not emailer_schedule_id:
                    raise ValueError("name and emailer_schedule_id required to create a sequence")
                if dry_run:
                    return {"dry_run": True, "action": "create", "name": name,
                            "emailer_schedule_id": emailer_schedule_id,
                            "active": bool(active), "label_names": label_names or []}
                return client.create_sequence(
                    name=name, emailer_schedule_id=emailer_schedule_id,
                    active=bool(active), label_names=label_names, folder_id=folder_id,
                    max_emails_per_day=max_emails_per_day, emailer_steps=emailer_steps)

            if not sequence_id:
                raise ValueError("sequence_id required")

            if op == "update":
                fields: dict[str, Any] = {}
                if name is not None:
                    fields["name"] = name
                if active is not None:
                    fields["active"] = active
                if emailer_schedule_id is not None:
                    fields["emailer_schedule_id"] = emailer_schedule_id
                if label_names is not None:
                    fields["label_names"] = label_names
                if max_emails_per_day is not None:
                    fields["max_emails_per_day"] = max_emails_per_day
                if cc_emails is not None:
                    fields["cc_emails"] = cc_emails
                if bcc_emails is not None:
                    fields["bcc_emails"] = bcc_emails
                if emailer_steps is not None:
                    fields["emailer_steps"] = emailer_steps
                if dry_run:
                    return {"dry_run": True, "action": "update", "sequence_id": sequence_id,
                            "changes": fields, "current_available": False}
                return client.update_sequence(sequence_id, **fields)

            if op == "activate":
                if dry_run:
                    return {"dry_run": True, "action": "activate", "sequence_id": sequence_id}
                return client.activate_sequence(sequence_id)

            if op == "deactivate":
                if dry_run:
                    return {"dry_run": True, "action": "deactivate", "sequence_id": sequence_id}
                return client.deactivate_sequence(sequence_id)

            if op == "archive":
                if dry_run:
                    return {"dry_run": True, "action": "archive", "sequence_id": sequence_id,
                            "current_available": False}
                return client.archive_sequence(sequence_id)
        except ValueError as e:
            raise _bad(str(e))

    @mcp.tool()
    def apollo_sequence_contacts(
        op: Literal["add", "update_status", "activity"],
        sequence_id: Optional[str] = None,
        contact_ids: Optional[list[str]] = None,
        label_names: Optional[list[str]] = None,
        send_email_from_email_account_id: Optional[str] = None,
        send_email_from_email_address: Optional[str] = None,
        status: Optional[str] = None,
        emailer_campaign_ids: Optional[list[str]] = None,
        mode: Optional[Literal["mark_as_finished", "remove", "stop"]] = None,
        contact_id: Optional[str] = None,
        per_page: int = 50,
        dry_run: bool = False,
    ) -> dict:
        """Enroll/update/inspect contacts in Apollo sequences. BYO key only for
        `add`/`update_status` (starts or stops an automated campaign to real
        people); `activity` (read) accepts the platform key.

        Args by op:
        - `add`: `sequence_id` + `send_email_from_email_account_id` (id of a
          CONNECTED mailbox — get one from apollo_email_accounts; REQUIRED, refused
          locally without it) + `contact_ids` or `label_names` (at least one). The
          HIGHEST-RISK call here: it starts a multi-step automated campaign to real
          people, not a single send. Supports `dry_run=True` (echoes the request,
          no get-by-id available to show a real diff).
        - `update_status`: `emailer_campaign_ids` + `contact_ids` + `mode`
          (`mark_as_finished`, `remove`, or `stop`). Supports `dry_run=True`.
        - `activity`: `contact_id` (required) + optional `sequence_id` filter +
          `per_page` (1-50, most recent events, not pagination). 0 credit, but BYO
          key only — the events are the key owner's OWN sequences.
        """
        if op not in ("add", "update_status", "activity"):
            raise _bad(f'unknown op "{op}" — expected: add, update_status, activity')

        if op == "activity":
            if not contact_id:
                raise _bad("contact_id required for op=activity")
            client = _client_byo()
            try:
                return client.get_contact_sequence_activity(
                    contact_id, sequence_id=sequence_id, per_page=per_page)
            except ValueError as e:
                raise _bad(str(e))

        client = _client_byo()
        try:
            if op == "add":
                if not sequence_id:
                    raise ValueError("sequence_id required")
                if not send_email_from_email_account_id:
                    raise ValueError(
                        "send_email_from_email_account_id required — get it via "
                        "apollo_email_accounts()")
                if not contact_ids and not label_names:
                    raise ValueError("contact_ids or label_names required")
                if dry_run:
                    return {"dry_run": True, "action": "add", "sequence_id": sequence_id,
                            "send_email_from_email_account_id": send_email_from_email_account_id,
                            "contact_ids": contact_ids or [], "label_names": label_names or []}
                return client.add_contacts_to_sequence(
                    sequence_id, send_email_from_email_account_id,
                    contact_ids=contact_ids, label_names=label_names,
                    send_email_from_email_address=send_email_from_email_address,
                    status=status)

            if op == "update_status":
                if not emailer_campaign_ids:
                    raise ValueError("emailer_campaign_ids required")
                if not contact_ids:
                    raise ValueError("contact_ids required")
                if mode not in ("mark_as_finished", "remove", "stop"):
                    raise ValueError('mode must be "mark_as_finished", "remove" or "stop"')
                if dry_run:
                    return {"dry_run": True, "action": "update_status", "mode": mode,
                            "emailer_campaign_ids": emailer_campaign_ids,
                            "contact_ids": contact_ids}
                return client.update_sequence_contact_status(
                    emailer_campaign_ids, contact_ids, mode)
        except ValueError as e:
            raise _bad(str(e))

    # ------------------------------------------------------------------
    # One-off emails
    # ------------------------------------------------------------------

    @mcp.tool()
    def apollo_email(
        op: Literal["draft", "status", "search", "content", "stats"],
        contact_id: Optional[str] = None,
        subject: Optional[str] = None,
        body_html: Optional[str] = None,
        recipients: Optional[list[dict]] = None,
        in_response_to_emailer_message_id: Optional[str] = None,
        emailer_template_id: Optional[str] = None,
        attachment_ids: Optional[list[str]] = None,
        enable_tracking: Optional[bool] = None,
        outreach_task_id: Optional[str] = None,
        message_id: Optional[str] = None,
        stats: Optional[list[str]] = None,
        reply_classes: Optional[list[str]] = None,
        sequence_ids: Optional[list[str]] = None,
        exclude_sequence_ids: Optional[list[str]] = None,
        keywords: Optional[str] = None,
        date_range_mode: Optional[str] = None,
        date_min: Optional[str] = None,
        date_max: Optional[str] = None,
        per_page: int = 25,
        page: int = 1,
        ids: Optional[list[str]] = None,
        body_format: str = "plain",
    ) -> dict:
        """One-off emails (outside sequences): prepare, follow, read. SENDING is
        `apollo_act(op="send_email")` — another tool, the one your client asks you to
        confirm: `draft` here never reaches anyone. BYO key only on EVERY op: `search`/`content`/`stats` read the key owner's OWN
        sent mailbox (bodies included), not Apollo's shared database — unlike
        organization/people search, a platform key here would leak someone else's inbox.

        Args by op:
        - `draft`: `contact_id` required UNLESS `in_response_to_emailer_message_id`
          is set (thread reply). Optional `subject`, `body_html`, `recipients`
          (`[{"email":, "contact_id":, "recipient_type_cd": "to"|"cc"|"bcc"}]`),
          `emailer_template_id`, `attachment_ids`, `enable_tracking`,
          `outreach_task_id`. Does NOT send: `apollo_act(op="send_email",
          message_id=…)` does.
        - `status`: `message_id` — poll send status.
        - `search`: `stats`, `reply_classes`, `sequence_ids`, `exclude_sequence_ids`,
          `keywords`, `date_range_mode` (`due_at`/`completed_at`), `date_min`/`date_max`
          (`YYYY-MM-DD`), `per_page`, `page`.
        - `content`: `ids` (up to 10 SENT email ids) + `body_format` (`plain`/`html`).
        - `stats`: `message_id` — opens/clicks. ⚠️ Apollo doc says this needs a
          Master API key, not verified from this environment.
        """
        if op not in ("draft", "status", "search", "content", "stats"):
            raise _bad(f'unknown op "{op}" — expected: draft, status, search, content, '
                       'stats (sending is apollo_act)')

        client = _client_byo()
        try:
            if op == "draft":
                if not contact_id and not in_response_to_emailer_message_id:
                    raise ValueError(
                        "contact_id required, except when replying to a thread "
                        "(in_response_to_emailer_message_id)")
                return client.create_email_draft(
                    contact_id=contact_id, subject=subject, body_html=body_html,
                    recipients=recipients,
                    in_response_to_emailer_message_id=in_response_to_emailer_message_id,
                    emailer_template_id=emailer_template_id,
                    attachment_ids=attachment_ids, enable_tracking=enable_tracking,
                    outreach_task_id=outreach_task_id)

            if op == "status":
                if not message_id:
                    raise ValueError("message_id required")
                return client.check_email_send_status(message_id)
            if op == "search":
                return client.search_emails(
                    stats=stats, reply_classes=reply_classes, sequence_ids=sequence_ids,
                    exclude_sequence_ids=exclude_sequence_ids, keywords=keywords,
                    date_range_mode=date_range_mode, date_min=date_min, date_max=date_max,
                    per_page=per_page, page=page)
            if op == "content":
                if not ids:
                    raise ValueError("ids required (at least one)")
                return client.get_email_content(ids, body_format=body_format)
            if op == "stats":
                if not message_id:
                    raise ValueError("message_id required")
                return client.get_email_stats(message_id)
        except ValueError as e:
            raise _bad(str(e))

    @mcp.tool(annotations=ACTE)
    def apollo_act(
        op: Literal["send_email"],
        message_id: str,
        surface: Optional[str] = None,
        dry_run: bool = False,
    ) -> dict:
        """⚠️ ACTS at Apollo, in the key owner's name: `send_email` sends a one-off
        email prepared by `apollo_email(op="draft")` — IRREVERSIBLE, it reaches a real
        person. Preparing, following and reading emails is `apollo_email`.

        Args:
            op: "send_email".
            message_id: the draft's id (from `apollo_email(op="draft")`'s response).
            surface: optional sending surface.
            dry_run: True validates the call and sends nothing.
        """
        if op != "send_email":
            raise _bad(f'unknown op "{op}" — expected: send_email')
        if not message_id:
            raise _bad("op='send_email': message_id required")
        if dry_run:
            return {"dry_run": True, "action": "send_email", "message_id": message_id}
        return _client_byo().send_email_now(message_id, surface=surface)

    # ------------------------------------------------------------------
    # Conversations — byo-only on ALL ops: transcripts of real calls/video meetings
    # + conditional credit cost (unpredictable, not meterable a priori).
    # ------------------------------------------------------------------

    @mcp.tool()
    def apollo_conversation(
        op: Literal["search", "get", "export", "export_status"],
        conversation_id: Optional[str] = None,
        conversation_type: Optional[str] = None,
        account_id: Optional[str] = None,
        contact_ids: Optional[list[str]] = None,
        tag_ids: Optional[list[str]] = None,
        tracker_ids: Optional[list[str]] = None,
        organization_ids: Optional[list[str]] = None,
        date_range: Optional[dict] = None,
        per_page: int = 25,
        page: int = 1,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        email: Optional[str] = None,
        export_id: Optional[str] = None,
    ) -> dict:
        """Recorded conversations (calls/video meetings) — transcripts, recordings,
        participants. BYO key only (all ops): real recorded conversations, and the
        credit cost is conditional (1 if the conversation has AI insights, 0
        otherwise) so it can't be metered against a platform quota up front.

        Args by op:
        - `search`: `conversation_type` (`video_conference`/`phone_call`),
          `account_id`, `contact_ids`, `tag_ids`, `tracker_ids`, `organization_ids`,
          `date_range` (`{"start":, "end":}` ISO 8601), `per_page`, `page`.
        - `get`: `conversation_id`. ⚠️ 1 Apollo credit IF the conversation has AI
          insights, 0 otherwise — not knowable before the call.
        - `export`: `start_time` + `end_time` (ISO 8601, GMT) + `email` (team member
          to notify). ASYNC — returns `export_id`; does not wait for completion.
          Poll with `export_status`.
        - `export_status`: `export_id` (from `export`) — returns `redirect_url`
          once ready.
        """
        if op not in ("search", "get", "export", "export_status"):
            raise _bad(f'unknown op "{op}" — expected: search, get, export, export_status')

        client = _client_byo()
        try:
            if op == "search":
                return client.search_conversations(
                    conversation_type=conversation_type, account_id=account_id,
                    contact_ids=contact_ids, tag_ids=tag_ids, tracker_ids=tracker_ids,
                    organization_ids=organization_ids, date_range=date_range,
                    per_page=per_page, page=page)
            if op == "get":
                if not conversation_id:
                    raise ValueError("conversation_id required")
                return client.get_conversation(conversation_id)
            if op == "export":
                if not start_time or not end_time or not email:
                    raise ValueError("start_time, end_time and email required")
                return client.export_conversations(start_time, end_time, email)
            if op == "export_status":
                if not export_id:
                    raise ValueError("export_id required")
                return client.get_conversations_export(export_id)
        except ValueError as e:
            raise _bad(str(e))
