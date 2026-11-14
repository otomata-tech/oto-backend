"""Folk CRM — groups, people, companies, deals, notes, interactions, tasks, webhooks.

Wraps `oto.tools.folk.FolkClient` (public API https://developer.folk.app).
Key resolved per call via `access.resolve_api_key("folk")` — byo-only provider
(user key set on /account, or shared credential of the active org). No
platform key.

**Consolidated surface (ADR 0047 §Amendment, applied to the folk connector)**: one
tool per business OBJECT, the verb as an `op` parameter — 17 tools → 4. What was
merged, and what was NOT (the criterion is parameter homogeneity,
not the count):

- **`folk_record`** (search/get/create/update/delete/add_to_group) — the six
  verbs share the SAME set of parameters (`entity`, `group_id`,
  `object_type`, `id`/`ids`, `dry_run`); only the data carrier changes
  (`filters` for reads, `item`/`items` for creation, `fields` for updates).
  `entity` plays the role that `module` plays at Zoho: person | company | deal |
  note | interaction | task | reminder. The former `folk_list_deals` /
  `folk_list_notes` / `folk_list_reminders` / `folk_get_reminder` fold in WITHOUT
  adding a parameter: they are `op="search"` / `op="get"` on another
  `entity`. `mark_done`/`mark_todo` are the only two ops that apply to
  ONE entity (the task): at Folk, completion is a separate endpoint
  (`POST /tasks/{id}/mark-as-done`), rejected in a PATCH — folding it into
  `op="update"` would have misrepresented what the call does.
- **`folk_group`** (list/create/update/custom_fields/get_custom_field/
  create_custom_field/update_custom_field/members/add_member/remove_member/
  update_member) — stays separate: neither `id`/`ids` (never bulk — a workspace has
  few groups), nor `entity` (`entity_type` qualifies a custom field
  schema, it does not designate an object to write). The `*_member` ops joined
  it by the same homogeneity criterion (anchored on `group_id`, like the
  seven previous ops) rather than a separate `folk_group_member` tool whose
  only required parameter would have been... `group_id`. Folk has **no
  delete endpoint** for a group or a custom field (checked against the
  docs — only list/create/update exist): neither can be removed
  via the API, only from the Folk app — a **member**, however,
  can be removed via the API (`op="remove_member"`).
- **`folk_user`** (list/get) — stays separate: a workspace member is not a CRM
  record (no `entity`, no `group_id`/`object_type`, no writes).
  Its `user_id` parameter reappears on `folk_group` (`*_member` ops) — same
  id space (a group member IS a workspace user), not a naming
  coincidence.
- **`folk_webhook`** (list/create/update) — stays separate: GLOBAL workspace
  resource (no `entity`, no `group_id`/`object_type`, no bulk mode — a
  workspace has few), with its own event vocabulary validated on
  input.

Surface: read/write **per entity** (`op="search"`/`"get"` take
`entity` = person|company|deal[|note|reminder]). `op="create"`/`"update"`/
`"delete"`/`"add_to_group"` also cover note/reminder (and interaction for
create), and are **solo OR bulk depending on the param passed**: a singular
(`item`/`id`) for ONE record → direct result; a plural (`items`/`ids`, ≤50)
for several → lightweight receipt (count + per-item errors, never N full
response bodies). For person, company and deal, `create`/`update` in bulk go
through Folk's native batch endpoints (`/…/batch/create|update`, shipped
October 2026): ONE request for the whole batch, one result per record
(`_creer_par_lot`, `_maj_par_lot`). The other entities, `delete`,
`add_to_group`, `dry_run`, and a client that predates the batch methods loop
over the single-record methods, in PARALLEL and at a capped rate
(`_bulk_run`) — the cap counts HTTP REQUESTS, not records (an update that
re-reads its record makes two).

⚠️ **Two different field vocabularies coexist**: `op="create"` takes
Python snake_case keys (`first_name`, `company_id`...); `op="update"` takes
the raw Folk API field names in camelCase (`jobTitle`,
`customFieldValues`...). Do not transpose one into the other — see the docstring
of `folk_record`. Two words ALSO change meaning between create and update, and
are explicitly rejected rather than returned as an opaque 422: `type` becomes
`activityType` in an interaction PATCH, and `completedAt` (writable at
task creation) is not patchable — use `op="mark_done"`.

**Interactions: reading, not only writing.** The connector long exposed
only `create_interaction`, hence the belief — written in black and white in
this connector's docs — that what had been said with a contact could not be
READ back. That was true of the connector, not of Folk: `GET /interactions/past`,
`/upcoming`, `/{id}` (+ PATCH and DELETE) exist, in open beta. They are
now wired to `op="search"/"get"/"update"/"delete"`. An interaction
is NOT addressable on its own: `entity.id` (the owning person/company) is
required in the query on listing/get/delete — hence the `entity_id` parameter.

**Reminders → tasks.** Folk deprecated `/reminders` on 2026-08-13 (removal
announced for February 2027) in favor of `/tasks`, which does strictly more:
markdown `description`, real filters (due date, assignee, completed or not), and
completion tracking that reminders never had. `entity="reminder"`
keeps working — deprecated is not broken — but nothing new should be
wired to it. What is NOT documented by Folk and remains to be verified:
whether reminders already set show up in `list_tasks` or not.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from fastmcp import FastMCP

from ..connectors import verify as connector_verify
from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from oto.tools.common.errors import UpstreamHTTPError

from .. import access


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _need(value, name: str, op: str):
    """Required argument for THIS op — actionable error that NAMES the op and
    the missing argument, never a silent fallback (the write ops of this
    module touch real data: guessing in the caller's place costs a
    record)."""
    if value is None:
        raise _bad(f"op='{op}' requires {name}")
    return value


# Folk's date-times (`completedAt` of mark-as-done, `dateTime` of an
# interaction) are `format: date-time` in its OpenAPI, but the validator behind
# them only takes the UTC form with a trailing `Z`: an offset such as `+02:00`,
# a perfectly valid ISO 8601, comes back as a 422 `Invalid datetime` that does
# not say which form it wanted (usage signal 1476). The interaction field is
# also capped at 24 characters — exactly `YYYY-MM-DDTHH:MM:SS.mmmZ`.
_UTC_Z = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z")
_ISO_DATETIME = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})"
    r"(?:[Tt ](\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d+))?)?"
    r"\s*(?:([Zz])|([+-])(\d{2}):?(\d{2}))?)?")
_DATETIME_EXAMPLE = "2026-11-13T09:00:00Z"


def _folk_datetime(value, field: str) -> str:
    """`value` in the only date-time form Folk accepts: UTC, trailing `Z`.

    - already `…Z` (uppercase, `T` separator): returned AS IS — what worked
      keeps going out byte for byte;
    - with an offset (`+02:00`, `-0500`, `+00:00`) or a lowercase `z`: converted
      to the SAME instant in UTC, `YYYY-MM-DDTHH:MM:SS.mmmZ` (sub-millisecond
      digits dropped);
    - without a timezone (or a bare date): REFUSED, naming the form. Guessing
      a timezone for the caller would silently move the instant.
    """
    if isinstance(value, str) and _UTC_Z.fullmatch(value):
        return value
    expected = (f"`{field}` must be an ISO 8601 date-time in UTC with a "
                f"trailing 'Z', e.g. {_DATETIME_EXAMPLE}")
    m = _ISO_DATETIME.fullmatch(value.strip()) if isinstance(value, str) else None
    if m is None:
        raise _bad(f"{expected} — got {value!r}.")
    (year, month, day, hour, minute, second, frac,
     zulu, sign, off_h, off_m) = m.groups()
    if hour is None or not (zulu or sign):
        raise _bad(
            f"{expected} — got {value!r}, which has no timezone: Folk refuses "
            "it, and guessing one would move the instant. Convert it to UTC, "
            "or give its offset (e.g. +02:00) and it is converted for you.")
    try:
        offset = timedelta(hours=int(off_h), minutes=int(off_m)) if sign else timedelta(0)
        local = datetime(int(year), int(month), int(day), int(hour),
                         int(minute), int(second or 0),
                         tzinfo=timezone(-offset if sign == "-" else offset))
    except ValueError:
        raise _bad(f"{expected} — got {value!r}, which is not a valid date-time.")
    millis = int((frac or "0")[:3].ljust(3, "0"))
    utc = local.astimezone(timezone.utc)
    return f"{utc:%Y-%m-%dT%H:%M:%S}.{millis:03d}Z"


_CUSTOM_FIELD_RESERVED_KEYS = {"group_id", "entity_type", "custom_field_name"}


def _reject_reserved_keys(d: dict, param_name: str, op: str) -> None:
    """`create_group_custom_field`/`update_group_custom_field` splat the caller's
    dict (**field / **fields) onto NAMED parameters (group_id,
    entity_type, custom_field_name) — if the dict carries one of these keys,
    `TypeError: got multiple values for keyword argument` surfaces as an opaque
    error instead of an actionable refusal. Same family of collision as
    `_create_one` (folk_record): a business field eaten by a same-named
    parameter."""
    collide = _CUSTOM_FIELD_RESERVED_KEYS & set(d or {})
    if collide:
        raise _bad(
            f"op='{op}': {param_name} must not carry {sorted(collide)} — "
            "these are tool parameters (group_id/entity_type/"
            "custom_field_name), not fields of the custom field API.")


_AVAILABLE_ENTITY_TYPES_RE = re.compile(r"Available entity types are:\s*(.+)")
_FIXED_ENTITY_TYPES = {"person", "company"}


def _resolve_deal_object_type(c, group_id: str) -> str:
    """`entity="deal"`'s `object_type` long defaulted to `"deals"` — but
    the deal object is a CUSTOM OBJECT that each Folk customer names themselves
    ("Deals" is just the name chosen BY THIS workspace; confirmed live, another
    workspace may call it something else, or capitalize "deals"). Probe rather than
    guess: try "deals" (the historical value), and if Folk answers 404, its own message
    lists the group's REAL entity_types
    (`"Available entity types are: ..."`) — we take the one that is neither
    "person" nor "company". Ambiguous (several custom objects, e.g. Deals/Events/
    Projects) or no candidate: actionable error rather than a guess
    that would write to the wrong place.
    """
    try:
        c.get_group_custom_fields(group_id, entity_type="deals")
        return "deals"
    except UpstreamHTTPError as e:
        if e.status_code != 404:
            raise
        message = (e.body or {}).get("error", {}).get("message", "") \
            if isinstance(e.body, dict) else ""
        match = _AVAILABLE_ENTITY_TYPES_RE.search(message)
        if not match:
            raise  # 404 of another nature (e.g. group_id not found) — don't guess on it
        available = re.findall(r'"([^"]+)"', match.group(1))
        candidates = [t for t in available if t not in _FIXED_ENTITY_TYPES]
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            raise _bad(
                f"group_id {group_id!r} has no deal object — available objects: "
                f"{sorted(available)}. Pass `object_type` explicitly if one "
                "of them fits (see folk_group(op='custom_fields') for details).")
        raise _bad(
            f"group_id {group_id!r} has several custom objects {sorted(candidates)} — "
            "cannot guess which one designates the deals. Pass `object_type` "
            "explicitly.")


def _merge_group_ids(current_groups, add, remove) -> list[dict]:
    """Merges a Folk record's group list and returns the COMPLETE list
    in API format (`[{"id": ...}]`).

    The Folk API is *replace-all* on list fields (a `groups` PATCH
    overwrites the whole list): to add/remove a group without losing the
    others, you must re-read the current groups and send back the resulting union.
    Preserves order and deduplicates.
    """
    remove_set = set(remove or [])
    result: list[str] = []
    for g in (current_groups or []):
        gid = g.get("id") if isinstance(g, dict) else g
        if gid and gid not in remove_set and gid not in result:
            result.append(gid)
    for gid in (add or []):
        if gid not in remove_set and gid not in result:
            result.append(gid)
    return [{"id": gid} for gid in result]


def _forme_ecriture(valeur):
    """A value that was READ back, brought to the form Folk accepts on write (#866, #834).

    Folk returns a user field as `{id, fullName, email}`, a contact or object field as
    `{id, fullName, entityType}`, but only accepts `{id}` on write
    (or `{email}` for a user): sending the read form back as is
    makes the WHOLE call fail with a 422 ("either an id or an email, not both"), even
    for a field of a group the call did not target. Only lists of
    dicts that carry an `id` are touched; everything else passes through unchanged."""
    if isinstance(valeur, list) and valeur and all(
            isinstance(d, dict) and d.get("id") for d in valeur):
        return [{"id": d["id"]} for d in valeur]
    return valeur


def _merge_custom_fields(current_cfv, patch: dict) -> dict:
    """Merges `customFieldValues` and returns the COMPLETE object expected by the API.

    Same fault as `groups` just above, and far costlier: the Folk API is
    *replace-all* on this object too. Passing a single custom field used to erase all
    the others of that group on the record — silently, with `succeeded: 1` returned.
    Measured on 2026-09-04: **four fields lost in one call**, including an
    operational instruction (oto-backend, signal 714).

    ⚠️ The tool documentation SAYS that `groups` is a replacement and offers
    `add_to_groups`/`remove_from_groups` to avoid it. It is that visible precaution
    on the neighboring field that misleads: it makes people conclude that merging is the default
    elsewhere. A remedy that only handled `groups` would thus leave intact the field
    where the same flaw costs the most.

    **What is provided wins, what is absent survives.** The granularity is the FIELD,
    not the group: the record's other groups are kept as is, and in the
    targeted group the keys not cited stay in place.

    ⚠️ **An explicitly provided value is written as is, `None` and `""`
    included** — that is how a field is CLEARED, and it must remain possible:
    the founding incident was repaired by a second call that reset it to empty. Merging
    "except empty values" would remove the only erasing gesture available.
    """
    out: dict = {}
    for gid, champs in (current_cfv or {}).items():
        out[str(gid)] = ({k: _forme_ecriture(v) for k, v in champs.items()}
                         if isinstance(champs, dict) else champs)
    for gid, champs in (patch or {}).items():
        gid = str(gid)
        ancien = out.get(gid)
        if isinstance(ancien, dict) and isinstance(champs, dict):
            ancien.update(champs)
        else:
            # Group absent from the record, or unexpected shape on one side: we write what
            # the caller provided. Nothing is overwritten that we could have preserved.
            out[gid] = dict(champs) if isinstance(champs, dict) else champs
    return out


# --- per-entity dispatch, shared between singular and bulk modes ------------
#
# `_create_one`/`_update_one`/`_delete_one` carry the op's logic on ONE
# record: we extract it so that bulk mode calls it item by item without
# duplicating/diverging from the validation. All three accept `dry_run`
# (oto convention — cf. `email_send`, LinkedIn `send_message`/`connect`): the
# validation runs normally, only the final mutating call is skipped, replaced
# by a preview.

# Dispatch axes of `folk_record`, DECLARED in the schema (`Literal` → JSON `enum`).
# Since the consolidation, the verb is no longer in the tool NAME: without an enum, the
# allowed values exist only in the docstring prose, and nothing constrains
# the client. `_Entity` bounds the UNION of entities (= everything at least one op
# accepts); the subset allowed PER op is still guarded by the tuples below.
_Entity = Literal["person", "company", "deal", "note", "interaction", "task",
                  "reminder"]
_RecordOp = Literal["search", "get", "create", "update", "delete",
                    "add_to_group", "mark_done", "mark_todo"]

_SEARCH_ENTITIES = ("person", "company", "deal", "note", "interaction", "task",
                    "reminder")
_GET_ENTITIES = ("person", "company", "deal", "interaction", "task", "reminder")
_CREATE_ENTITIES = ("person", "company", "deal", "note", "interaction", "task",
                    "reminder")
_UPDATE_ENTITIES = ("person", "company", "deal", "note", "interaction", "task",
                    "reminder")
_DELETE_ENTITIES = ("person", "company", "deal", "note", "interaction", "task",
                    "reminder")
_GROUP_ENTITIES = ("person", "company")
# `mark_done`/`mark_todo` only exist on the task: at Folk, completion
# is a SEPARATE call (`POST /tasks/{id}/mark-as-done`), never a PATCH — a
# task does not complete by itself, unlike a reminder which marks itself
# "triggered" on its own schedule.
_MARK_ENTITIES = ("task",)

# Entities whose id is addressable ONLY through the parent entity: Folk requires
# `entity.id` on BOTH listing endpoints, on get and delete (in the
# query), and on the PATCH (in the body) — there is no plain "read
# interaction lit_…". The PATCH was verified live on
# 2026-08-27: the OpenAPI spec does not mark `entity` as required, yet Folk answers
# 422 `path: ['entity'], Required` without it.
_ENTITY_ID_REQUIRED = ("interaction",)

# Fields accepted by `op="create"` per entity — mirror of the named parameters
# of the `FolkClient.create_*` methods (Python snake_case, NOT the camelCase Folk API
# field names used by `op="update"`/`fields`). Hardcoded
# rather than introspected via `inspect.signature`: `create_person`/
# `create_company` accept `**kwargs` on the client side, so without this explicit
# allow-list a misspelled/miscased field (e.g. `firstName` instead of
# `first_name`) would be SILENTLY swallowed into the payload sent to
# Folk under the wrong name, rather than raising an error. A hardcoded list
# is also testable against a mocked `FolkClient` (signature introspection
# does not work on a Mock without `autospec`).
_CREATE_FIELDS = {
    # `group_ids` → `groups`, `custom_fields` → `customFieldValues` (keyed by
    # group id; each group is added to `groups`, which Folk requires): see
    # `_vers_client`. Folk's create takes them in the same request.
    "person": {"first_name", "last_name", "emails", "phones", "job_title",
               "company_name", "company_id", "group_ids", "urls", "description",
               "custom_fields"},
    "company": {"name", "emails", "industry", "description", "phones", "urls",
                "group_ids", "custom_fields"},
    "deal": {"name", "people_ids", "company_ids", "custom_fields"},
    "note": {"entity_id", "content", "visibility"},
    "interaction": {"entity_id", "type", "title", "content", "date_time"},
    "task": {"entity_id", "title", "due_at", "due_time", "description",
             "recurrence_frequency", "assigned_users", "is_public"},
    "reminder": {"entity_id", "name", "recurrence_rule", "visibility"},
}

# Fields that an `op="update"` must REJECT, with the path to take instead.
# Two traps inherited from asymmetries in the Folk API itself — without this guard,
# each yields an opaque 422 where the caller merely used the wrong word:
#   - `type` is the field name at interaction CREATION, but the PATCH
#     calls it `activityType` (same value, different key);
#   - `completedAt` is written at task creation, but the PATCH rejects it
#     (`additionalProperties: false`): to complete, use `op="mark_done"`.
_UPDATE_FORBIDDEN_FIELDS = {
    "interaction": {
        "type": "an interaction PATCH names this field `activityType` "
                "(it is `type` only at creation).",
    },
    "task": {
        "completedAt": "completing a task is not a PATCH — "
                       "use op='mark_done' (or op='mark_todo' to "
                       "reopen it).",
    },
}

# Filters accepted by `op="search"` on note/reminder: Folk only exposes one
# filter per parent entity (`list_notes(entity_id=…)`). Unlike
# `list_people(**filters)`, these methods have a CLOSED signature — an unknown
# filter would raise a `TypeError` rendered as an "internal error", where the caller
# needs to read which filter exists.
_SUBRECORD_FILTERS = {"entity_id"}


def _reject_forbidden_update_fields(entity: str, fields: dict) -> None:
    """Rejects, NAMING it, a field that exists elsewhere on the same entity
    but not in its PATCH. Without this the caller gets an opaque Folk 422
    (`unrecognized_keys`) on a word they read in this very docstring — in the
    creation aisle."""
    for name, why in _UPDATE_FORBIDDEN_FIELDS.get(entity, {}).items():
        if name in fields:
            raise _bad(f"op='update' entity='{entity}': field `{name}` "
                       f"rejected — {why}")


def _get_one(c, entity: str, id: str, group_id: Optional[str] = None,
             object_type: str = "deals", entity_id: Optional[str] = None):
    """Fetches a record's current state, for `dry_run` diff/preview.

    Returns `None` for `note`: Folk has NO get-by-id endpoint for
    notes (`client.py` only exposes list/create/update/delete) — a permanent
    API gap, not an implementation shortcut. Note update/delete previews
    degrade accordingly (no diff possible).

    The "interaction without `entity_id`" case no longer occurs: the three ops
    that call `_get_one` on an interaction (get, update, delete) all require it
    upstream. The guard stays for safety."""
    if entity == "person":
        return c.get_person(id)
    if entity == "company":
        return c.get_company(id)
    if entity == "deal":
        if not group_id:
            raise _bad("group_id required for entity='deal'.")
        return c.get_deal(group_id, id, object_type=object_type)
    if entity == "interaction":
        return c.get_interaction(id, entity_id) if entity_id else None
    if entity == "task":
        return c.get_task(id)
    if entity == "reminder":
        return c.get_reminder(id)
    return None


def _valider_creation(entity: str, fields: Optional[dict],
                      group_id: Optional[str]) -> dict:
    """The caller's item, checked against `_CREATE_FIELDS` — shared by the solo
    path and both bulk paths, so a batch refuses exactly what a single call does."""
    fields = dict(fields or {})
    if entity == "deal" and not group_id:
        raise _bad("group_id required for entity='deal'.")
    unknown = set(fields) - _CREATE_FIELDS.get(entity, set())
    if unknown:
        raise _bad(
            f"unknown field(s) for entity='{entity}': {sorted(unknown)}. "
            f"Accepted fields: {sorted(_CREATE_FIELDS.get(entity, set()))}. "
            f"Reminder: op='create' uses Python snake_case keys "
            f"(first_name, company_id...) — NOT the camelCase Folk API field "
            f"names (jobTitle, customFieldValues...) used by op='update'.")
    if entity == "interaction" and fields.get("date_time"):
        fields["date_time"] = _folk_datetime(fields["date_time"], "date_time")
    return fields


def _vers_client(entity: str, fields: dict) -> dict:
    """The validated item, in the keyword arguments of the client's `create_*`
    / `*_body` — which pass the keys they do not name RAW to Folk.

    `custom_fields` becomes `customFieldValues` (person, company), and each of
    its groups joins `group_ids`: Folk refuses a custom value whose group is not
    in `groups`, with a 422 that names neither. A company's `group_ids` becomes
    `groups` (`company_body` has no `group_ids` of its own; `person_body`
    does). A deal keeps `custom_fields`, which `deal_body` names."""
    if entity not in ("person", "company"):
        return fields
    out = dict(fields)
    valeurs = out.pop("custom_fields", None)
    if valeurs is not None:
        if not isinstance(valeurs, dict) or not all(
                isinstance(v, dict) for v in valeurs.values()):
            raise _bad("`custom_fields` is {group_id: {field name: value}}.")
        groupes = list(out.get("group_ids") or [])
        out["group_ids"] = groupes + [g for g in valeurs if g not in groupes]
        out["customFieldValues"] = valeurs
    if entity == "company" and "group_ids" in out:
        out["groups"] = [{"id": g} for g in out.pop("group_ids") or []]
    return out


def _create_one(c, entity: str, fields: Optional[dict] = None,
                 group_id: Optional[str] = None,
                 object_type: str = "deals", dry_run: bool = False):
    """Creates ONE record. `fields` = the caller's item, passed as a DICT.

    Never `**fields`: the item's keys come from the agent, and one
    of them may carry the name of a parameter of this function — `folk_record
    (op='create', entity='person', item={... 'group_id': 'grp_…'})` then raised
    a `TypeError: got multiple values for keyword argument 'group_id'`, rendered to
    the caller as an "internal server error" where they expected the actionable
    refusal "unknown field for entity='person'" that the validation just
    below can produce (signal #353). Same family as the collision of
    context tokens: a business argument eaten by a same-named parameter.
    Passing the dict closes the collision by construction, for any future key.
    """
    fields = _valider_creation(entity, fields, group_id)
    if dry_run:
        preview = {"would_create": fields}
        if entity == "deal":
            preview.update(group_id=group_id, object_type=object_type)
        return preview
    fields = _vers_client(entity, fields)
    if entity == "person":
        return c.create_person(**fields)
    if entity == "company":
        return c.create_company(**fields)
    if entity == "deal":
        return c.create_deal(group_id, object_type=object_type, **fields)
    if entity == "note":
        return c.create_note(**fields)
    if entity == "interaction":
        return c.create_interaction(**fields)
    if entity == "task":
        return c.create_task(**fields)
    if entity == "reminder":
        return c.create_reminder(**fields)
    raise _bad(f"entity must be one of {_CREATE_ENTITIES}.")


def _update_one(c, entity: str, id: str, fields: Optional[dict] = None,
                 group_id: Optional[str] = None, object_type: str = "deals",
                 add_to_groups: Optional[list[str]] = None,
                 remove_from_groups: Optional[list[str]] = None,
                 dry_run: bool = False, entity_id: Optional[str] = None):
    fields, current = _preparer_maj(
        c, entity, id, fields, group_id=group_id, object_type=object_type,
        add_to_groups=add_to_groups, remove_from_groups=remove_from_groups,
        dry_run=dry_run, entity_id=entity_id)
    if dry_run:
        if current is not None:
            return {"id": id, "changes": {k: {"from": current.get(k), "to": v}
                                          for k, v in fields.items()}}
        return {"id": id, "fields": fields, "current_available": False}
    if entity == "person":
        return c.update_person(id, **fields)
    if entity == "company":
        return c.update_company(id, **fields)
    if entity == "deal":
        if not group_id:
            raise _bad("group_id required for entity='deal'.")
        return c.update_deal(group_id, id, object_type=object_type, **fields)
    if entity == "note":
        return c.update_note(id, **fields)
    if entity == "interaction":
        return c.update_interaction(
            id, _need(entity_id, "entity_id", "update (entity='interaction')"),
            **fields)
    if entity == "task":
        return c.update_task(id, **fields)
    if entity == "reminder":
        return c.update_reminder(id, **fields)
    raise _bad(f"entity must be one of {_UPDATE_ENTITIES}.")


def _preparer_maj(c, entity: str, id: str, fields: Optional[dict] = None,
                  group_id: Optional[str] = None, object_type: str = "deals",
                  add_to_groups: Optional[list[str]] = None,
                  remove_from_groups: Optional[list[str]] = None,
                  dry_run: bool = False,
                  entity_id: Optional[str] = None) -> tuple[dict, Optional[dict]]:
    """The fields an update WRITES, and the current record when it had to be
    read — shared by the solo PATCH and the batch update, so that both merge
    `customFieldValues` and `groups` the same way (Folk REPLACES both)."""
    fields = dict(fields or {})
    _reject_forbidden_update_fields(entity, fields)
    if entity == "interaction" and fields.get("dateTime"):
        fields["dateTime"] = _folk_datetime(fields["dateTime"], "dateTime")
    current = None
    # `customFieldValues` joins the list of fields that REQUIRE the current state: without
    # it, a partial patch is destructive (cf. `_merge_custom_fields`).
    besoin_courant = bool(add_to_groups or remove_from_groups or dry_run
                          or isinstance(fields.get("customFieldValues"), dict))
    if besoin_courant:
        current = _get_one(c, entity, id, group_id=group_id,
                           object_type=object_type, entity_id=entity_id)
    if isinstance(fields.get("customFieldValues"), dict):
        # ⚠️ REFUSE rather than write blindly. Without the current state we cannot
        # merge, and sending the patch as is would overwrite the fields we could not
        # read — exactly the damage this batch fixes. A named refusal lets
        # the caller choose; a silent success leaves nothing.
        if current is None:
            raise _bad(
                "Cannot re-read the record to merge `customFieldValues`: "
                "the Folk API REPLACES this object, so writing without the current state "
                "would erase the custom fields not provided. Retry, or pass "
                "the complete object after an op='get'.")
        fields["customFieldValues"] = _merge_custom_fields(
            current.get("customFieldValues"), fields["customFieldValues"])
    if add_to_groups or remove_from_groups:
        if entity not in _GROUP_ENTITIES:
            raise _bad("add_to_groups/remove_from_groups only apply to "
                       "entity='person' or 'company'.")
        if "groups" in fields:
            raise _bad("Do not pass 'groups' in fields at the same time as "
                       "add_to_groups/remove_from_groups.")
        fields["groups"] = _merge_group_ids(
            (current or {}).get("groups"), add_to_groups, remove_from_groups)
    if not fields:
        raise _bad("Nothing to update: provide `fields` and/or "
                   "add_to_groups/remove_from_groups.")
    return fields, current


def _delete_one(c, entity: str, id: str, group_id: Optional[str] = None,
                 object_type: str = "deals", dry_run: bool = False,
                 entity_id: Optional[str] = None):
    if dry_run:
        current = _get_one(c, entity, id, group_id=group_id,
                           object_type=object_type, entity_id=entity_id)
        if current is not None:
            return {"id": id, "would_delete": current}
        return {"id": id, "would_delete": None, "current_available": False}
    if entity == "person":
        return c.delete_person(id)
    if entity == "company":
        return c.delete_company(id)
    if entity == "deal":
        if not group_id:
            raise _bad("group_id required for entity='deal'.")
        return c.delete_deal(group_id, id, object_type=object_type)
    if entity == "note":
        return c.delete_note(id)
    if entity == "interaction":
        return c.delete_interaction(id, _need(entity_id, "entity_id", "delete"))
    if entity == "task":
        return c.delete_task(id)
    if entity == "reminder":
        return c.delete_reminder(id)
    raise _bad(f"entity must be one of {_DELETE_ENTITIES}.")


def _mark_one(c, id: str, done: bool, completed_at: Optional[str] = None,
              dry_run: bool = False):
    """`op="mark_done"` / `op="mark_todo"` on ONE task.

    The preview re-reads the task: it shows what is about to be closed (title,
    due date) and its current `completedAt` — closing an already closed task, or
    reopening one never completed, is a silent no-op on Folk's side."""
    if dry_run:
        current = c.get_task(id)
        return {"id": id,
                "would_mark": "done" if done else "todo",
                "current": current}
    if done:
        return c.mark_task_done(id, completed_at=completed_at)
    return c.mark_task_todo(id)


# 50 remains a call-ergonomics limit (no precise finding behind it),
# independent of the rate below.
_BULK_MAX_ITEMS = 50

# A batch's bottleneck is the per-call network latency, not Folk's rate: it is
# not overlapped as long as calls are sequential. `_BULK_CONCURRENCY` parallel
# in-flight calls overlap it. The RATE is the client's (`FolkClient`, oto-core
# `oto/tools/folk/pacing.py`): every request, retries included, is spaced per key
# across every client and thread of the process, ~8 req/s by default, under
# Folk's burst policy (20 requests / 2 s); its 429 retry stays the backstop.
_BULK_CONCURRENCY = 6


def _bulk_fatal(exc: Exception) -> bool:
    """Auth/connection errors: we abandon the whole batch (repeating the same
    error N times is pointless). Everything else (a rejected record,
    Folk 422…) remains a PER-ITEM error that does not interrupt the batch."""
    from oto.tools.common.errors import UpstreamHTTPError
    import requests
    if isinstance(exc, UpstreamHTTPError):
        return exc.status_code in (401, 403)
    return isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout))


def _bulk_run(items: list, fn) -> list[tuple[int, bool, object]]:
    """Runs `fn(item)` for each item IN PARALLEL (up to
    `_BULK_CONCURRENCY` in-flight HTTP calls; the client paces the requests)
    rather than sequentially with a fixed pause after each call — it is the
    per-call network latency that dominated total time, not Folk's rate, and a
    sequential loop could never overlap it.

    Returns a list of `(index, ok, value_or_error_message)` — NOT necessarily in
    submission order: each caller relies only on the `index` carried by the
    tuple, never on the position in the list. A FATAL error (auth/connection)
    cancels the calls not yet started and re-raises the exception (the whole
    batch is lost, no partial receipt)."""
    if len(items) > _BULK_MAX_ITEMS:
        raise _bad(f"too many items ({len(items)}) — max {_BULK_MAX_ITEMS} per call, "
                   f"split into several calls.")
    if not items:
        return []
    results: list[Optional[tuple[int, bool, object]]] = [None] * len(items)
    pool = ThreadPoolExecutor(max_workers=min(_BULK_CONCURRENCY, len(items)))
    futures = {pool.submit(fn, item): i for i, item in enumerate(items)}
    try:
        for future in as_completed(futures):
            i = futures[future]
            try:
                results[i] = (i, True, future.result())
            except Exception as e:
                if _bulk_fatal(e):
                    pool.shutdown(wait=False, cancel_futures=True)
                    raise
                results[i] = (i, False, str(e))
    finally:
        pool.shutdown(wait=True)
    return [r for r in results if r is not None]


# --- Folk's native batch endpoints (person, company, deal) ----------------------
#
# ONE request for the whole batch (≤ 100 records at Folk, ≤ `_BULK_MAX_ITEMS`
# here), answered 200 with one result per SENT record, in order. Each item is
# validated here first, exactly as the solo path does: an item refused here is
# a per-item `failed` and is not sent, so the results are mapped back to the
# ORIGINAL indices. Used for every entity of `_LOT` (the other entities have no
# batch endpoint at Folk and keep the loop), never for `dry_run` (a preview sends
# nothing, the loop already does that).

_LOT = {
    # entity: (record builder, batch create, batch update)
    "person": ("person_body", "create_people_batch", "update_people_batch"),
    "company": ("company_body", "create_companies_batch", "update_companies_batch"),
    "deal": ("deal_body", "create_deals_batch", "update_deals_batch"),
}


def _envoyer_lot(appel, prets: list[tuple[int, dict]], failed: list,
                 erreur_extra) -> list[tuple[int, dict]]:
    """Sends the prepared records in ONE request; returns `(index, result)` per
    SUCCEEDED record and appends the others to `failed`. A whole-request refusal
    that is not fatal (a 422 on the batch itself) fails every sent record with
    its message; an auth/connection error re-raises, as `_bulk_run` does."""
    if not prets:
        return []
    try:
        resultats = appel([r for _, r in prets])
    except Exception as e:
        if _bulk_fatal(e):
            raise
        failed.extend({"index": i, **erreur_extra(i), "error": str(e)} for i, _ in prets)
        return []
    reussis = []
    for k, (i, _) in enumerate(prets):
        res = resultats[k] if k < len(resultats) else None
        if isinstance(res, dict) and res.get("status") == "succeeded":
            reussis.append((i, res))
        else:
            err = (res or {}).get("error") if isinstance(res, dict) else None
            message = (err or {}).get("message") if isinstance(err, dict) else None
            failed.append({"index": i, **erreur_extra(i),
                           "error": message or str(err or "no result for this record")})
    return reussis


def _creer_par_lot(c, entity: str, items: list, group_id: Optional[str],
                   object_type: str) -> dict:
    """`op="create"` in bulk through `…/batch/create`. A company named exactly
    like an existing one is UPDATED by Folk (as its single create does): the
    receipt says so with `operation: "updated"` on that entry."""
    if len(items) > _BULK_MAX_ITEMS:
        raise _bad(f"too many items ({len(items)}) — max {_BULK_MAX_ITEMS} per call, "
                   f"split into several calls.")
    corps, creer, _ = _LOT[entity]
    failed: list = []
    prets = []
    for i, it in enumerate(items):
        try:
            kw = _vers_client(entity, _valider_creation(entity, it, group_id))
            prets.append((i, getattr(c, corps)(**kw)))
        except McpError as e:
            failed.append({"index": i, "error": str(e)})
    if entity == "deal":
        appel = lambda recs: getattr(c, creer)(group_id, recs, object_type=object_type)
    else:
        appel = getattr(c, creer)
    created = []
    for i, res in _envoyer_lot(appel, prets, failed, lambda i: {}):
        entree = {"index": i, "id": (res.get("data") or {}).get("id")}
        if res.get("operation") and res["operation"] != "created":
            entree["operation"] = res["operation"]
        if res.get("warnings"):
            entree["warnings"] = res["warnings"]
        created.append(entree)
    failed.sort(key=lambda f: f["index"])
    return {"total": len(items), "succeeded": len(created),
            "created": created, "failed": failed}


def _maj_par_lot(c, entity: str, items: list, group_id: Optional[str],
                 object_type: str, entity_id: Optional[str]) -> dict:
    """`op="update"` in bulk through `…/batch/update`. The records that touch
    `customFieldValues` or groups are first RE-READ (in parallel, paced) and
    merged, exactly as the solo PATCH does — Folk's batch update replaces
    `groups` like the PATCH, and nothing in its documentation says it keeps the
    custom fields a record leaves out. Then ONE request writes them all."""
    _, _, maj = _LOT[entity]

    def _prep(it):
        if "id" not in it:
            raise _bad("each item must contain 'id'.")
        champs, _ = _preparer_maj(
            c, entity, it["id"], it.get("fields"), group_id=group_id,
            object_type=object_type, add_to_groups=it.get("add_to_groups"),
            remove_from_groups=it.get("remove_from_groups"),
            entity_id=it.get("entity_id", entity_id))
        return {"id": it["id"], **champs}

    failed: list = []
    prets = []
    for i, ok, val in sorted(_bulk_run(items, _prep)):
        if ok:
            prets.append((i, val))
        else:
            failed.append({"index": i, "id": items[i].get("id"), "error": val})
    if entity == "deal":
        appel = lambda recs: getattr(c, maj)(group_id, recs, object_type=object_type)
    else:
        appel = getattr(c, maj)
    reussis = _envoyer_lot(appel, prets, failed, lambda i: {"id": items[i].get("id")})
    failed.sort(key=lambda f: f["index"])
    return {"total": len(items), "succeeded": len(reussis), "failed": failed}


def _verify(fields: dict, config: dict | None = None) -> None:
    """"Test the connection" probe — otomata-tech/oto#69. Covers `auth` ONLY.

    `GET /v1/users/me`. What Folk's docs establish, quoted:

    - **authenticated** — "Authentication Required: `bearerApiKeyAuth` (HTTP Bearer
      scheme with API key)";
    - **no side effects** — a GET that returns "The current user associated with
      the API key" (`id`, `fullName`, `email`). It reads the identity the
      key carries, it touches nothing;
    - **the cost**: the docs mention NO credit or billing, neither on this
      endpoint nor elsewhere — the regime is a rate limit, "600 requests per
      minute". ⚠️ That is not the same as a line saying "free":
      the absence of a credit counter in the whole doc is a strong argument, not
      proof. If Folk ever introduced per-call billing, this is where
      to come back.

    Does NOT read the quota. The `X-RateLimit-*` headers are served on this
    response though: surfacing them would make it an `auth+quota` probe. That is not done here because
    the per-minute rate is not a balance — it says nothing about what remains to
    be spent, only about the pace. Returning it would suggest a gauge.

    **Authenticated ≠ usable** (class named on oto#69, cf. attio/pennylane):
    Folk exposes no granular per-key scope — a present `id` IS the proof
    of usability, there is nothing finer to distinguish here. That is why
    the guard below checks the identity, not a separate `active`/`scope`.
    """
    from oto.tools.folk.client import FolkClient

    utilisateur = FolkClient(api_key=fields["key"]).get_current_user()
    if not (utilisateur or {}).get("id"):
        # A 200 response without identity: the key passes authentication but
        # designates no one. Staying silent would yield a "connected" verdict on an
        # account we cannot name.
        raise RuntimeError(
            "Folk answered without identifying this key's user — "
            f"unexpected response: {str(utilisateur)[:200]}")


def register(mcp: FastMCP) -> None:
    from oto.tools.folk.client import FolkClient, WEBHOOK_EVENT_TYPES

    connector_verify.register("folk", _verify)

    def _client() -> FolkClient:
        key, _ = access.resolve_api_key("folk")
        # Redaction of sensitive fields: no longer at client level — applied at the
        # tool boundary by `FieldRedactionMiddleware` (policy of the active org).
        return FolkClient(api_key=key)

    def _validate_subscribed_events(events: list) -> None:
        if not events:
            raise _bad("subscribed_events: at least one event required.")
        for e in events:
            event_type = (e or {}).get("eventType")
            if event_type not in WEBHOOK_EVENT_TYPES:
                raise _bad(
                    f"invalid eventType: {event_type!r}. Valid values: "
                    + ", ".join(sorted(WEBHOOK_EVENT_TYPES))
                )

    # --- the CRM record: one tool, the verb as `op` --------------------------
    #
    # Default `op` = "search", a READ: no write op is
    # reachable without naming it. The four mutating ops
    # (create/update/delete/add_to_group) take a pair of mutually
    # exclusive params: the singular (a single record, result/preview
    # returned directly) OR the plural (up to 50, bulk receipt). For person,
    # company and deal, bulk create/update/add_to_group go through Folk's
    # native batch endpoints (one request, October 2026); everything else
    # loops over the single-record methods, in parallel at a capped rate
    # (`_bulk_run`). Either way: a lightweight receipt, never N full bodies.

    @mcp.tool()
    def folk_record(
        entity: _Entity,
        op: _RecordOp = "search",
        id: Optional[str] = None,
        ids: Optional[list[str]] = None,
        item: Optional[dict] = None,
        items: Optional[list[dict]] = None,
        fields: Optional[dict] = None,
        filters: Optional[dict] = None,
        max_results: int = 100,
        add_to_groups: Optional[list[str]] = None,
        remove_from_groups: Optional[list[str]] = None,
        group_id: Optional[str] = None,
        object_type: Optional[str] = None,
        entity_id: Optional[str] = None,
        when: Optional[Literal["past", "upcoming", "all"]] = None,
        dry_run: bool = False,
    ) -> dict:
        """Folk CRM records — people (contacts), companies, deals (or any other
        custom object), notes, interactions, tasks, reminders: search, read,
        create, update, delete, add to a group, mark a task done. This is the
        tool for "find/add/update a contact", "look up a company", "list deals",
        "what did we say to X", "what's still open on X" etc. — Folk has no
        separate `folk_company`/`folk_contact`/`folk_deal` tool; `entity` picks
        the noun.

        `entity` scopes every op (person | company | deal | note | interaction |
        task | reminder); `op` picks the verb:

        - **"search"** (default): search records of that entity. Fetches ALL
          matching pages — always pass `filters` on a large workspace. Works on
          every entity, but three of them are addressed by their PARENT record
          rather than by a query: note and reminder take
          `filters={"entity_id": …}`, interaction takes `entity_id` (required)
          + `when`.
        - **"get"**: fetch one record by ID (full record). Every entity except
          note — Folk has NO get-by-id endpoint for notes. `interaction`
          additionally needs `entity_id` (see below).
        - **"create"**: create one (`item`) or several (`items`, ≤50) records.
        - **"update"**: PATCH one (`id`) or several (`items`, ≤50) records —
          only the given fields change.
        - **"delete"**: delete one (`id`) or several (`ids`, ≤50) records.
          Irreversible.
        - **"mark_done"** / **"mark_todo"**: close / reopen one (`id`) or
          several (`ids`, ≤50) **tasks**. `entity="task"` only, and a separate
          op on purpose: Folk refuses `completedAt` in a task PATCH. Nothing in
          Folk ever completes a task on its own — it only moves when something
          calls this.
        - **"add_to_group"**: add one (`id`) or several (`ids`, ≤50) existing
          people/companies to ONE group (`group_id` = the target group). The
          inverse of `op="update"`'s `add_to_groups` (which batches *groups* for
          *one* record) — this batches *records* into *one* group. Reads each
          record's current groups and writes back the union (Folk's `groups`
          field is replace-all on PATCH), so existing group membership is
          preserved. A record already in the group is a no-op success, not an
          error.

        📖 **Reading what actually happened.** `entity="interaction"` is
        readable, not just writable: `op="search"` returns the emails, calendar
        events and manually-logged interactions Folk holds for a person or
        company. (These endpoints are in Folk's **open beta** — the shape may
        still move.) Three things verified live on 2026-08-27, each of which
        changes how you use it:

        - **`op="search"` gives you `content: {subject, snippet}` — NOT the
          body.** The full `body` only comes back from `op="get"` on one
          interaction. So a sweep tells you what was discussed; reading what was
          actually *said* costs one `get` per interaction.
        - **`privacyLevel` withholds the body, not the subject.** On
          `subjectOnly`/`sensitive`/`internal`, `get` still returns subject and
          snippet but `body` is simply absent. The key is BYO, so what comes
          back is what ITS owner may see. A missing `body` is a permission
          outcome, not an empty interaction.
        - **Imported interactions are read-only.** Folk refuses `op="update"`
          and `op="delete"` on anything it pulled in from email, calendar or
          WhatsApp — those belong to their source. Only `interactionType:
          "logged"` records are writable.

        An org-level field-redaction policy, where one is set, can strip fields
        on top of all that.

        ⏳ **Tasks, not reminders — and they are the SAME records.** Folk
        **deprecated** `/reminders` on 2026-08-13 (removal announced for
        February 2027) in favour of `/tasks`. Verified live on 2026-08-27:
        the two are **one store with two views**, not two collections. A
        workspace with 30 reminders has exactly those 30 as tasks, sharing the
        same UUID under a different prefix — `rmd_<uuid>` and `tsk_<uuid>` are
        the same record, and swapping the prefix resolves in both directions.

        So nothing is stranded: a reminder created before the switch is
        readable, filterable and closable as a task today, and `op="mark_done"`
        works on it. Use `entity="task"` for everything new — it does strictly
        more (a markdown `description`, real filters on due date / assignee /
        completion, and completion tracking, which the reminder view has no
        verb for). Field mapping when porting: name→title,
        recurrence_rule→due_at/due_time + recurrence_frequency,
        visibility→is_public.

        The four write ops are **solo OR bulk depending on which param you
        pass**: exactly one of `item`/`items` (create), `id`/`items` (update),
        `id`/`ids` (delete, add_to_group) is required. Solo returns the record
        (or its dry_run preview) directly; bulk returns a receipt (count +
        per-item errors), never N full response bodies.

        ⚠️ **Two different field vocabularies coexist.** `op="create"` field
        names are Python **snake_case** parameter names (`first_name`,
        `company_id`...), forwarded directly to the client — NOT Folk's raw
        camelCase API field vocabulary (`jobTitle`, `customFieldValues`...) that
        `op="update"`'s `fields` uses. An unrecognized create field name raises
        immediately (listing the accepted ones), it is never silently dropped or
        sent under the wrong name. Don't mix the two conventions.

        Per-entity field shape for op="create" (same for `item` and each entry
        of `items`, `*` = required, snake_case — see the warning above):
            person: {first_name*, last_name, emails, phones, job_title,
                company_name, company_id, group_ids, urls, description,
                custom_fields}
                — ⚠️ Folk ALSO links, on its own, a company matched on the
                person's primary email domain, even when `company_id` is
                passed, and may create one for it — a duplicate of the company
                you named. Read back the person's `companies` after the create
                to see what Folk added.
            company: {name*, emails, industry, description, phones, urls,
                group_ids, custom_fields}
                — `custom_fields` = {group_id: {field name: value}}, set in
                the SAME request (each of its groups is joined too). In
                bulk, a company named EXACTLY like an existing one is updated
                by Folk instead of created: its `created` entry then carries
                `operation: "updated"`.
            deal: {name*, people_ids, company_ids, custom_fields}
            note: {entity_id*, content*, visibility}
            interaction: {entity_id*, type*, title*, content, date_time}
                — `date_time` is UTC with a trailing `Z` (🕘 below), and
                REQUIRED by Folk even though it reads as
                optional here; omitted, it defaults to now rather than failing
                with an opaque 422 (which is what the connector did until
                2026-08-27).
            task: {entity_id*, title*, due_at*, due_time, description,
                recurrence_frequency, assigned_users, is_public}
            reminder: {entity_id*, name*, recurrence_rule*, visibility}
                — DEPRECATED by Folk, use task.

        🕘 **Date-times are UTC with a trailing `Z`** — e.g.
        `2026-11-13T09:00:00Z` (or `…09:00:00.000Z`). This covers
        `fields.completedAt` (op="mark_done"), interaction `date_time`
        (op="create") and interaction `dateTime` (op="update"). Folk refuses
        any other ISO 8601 form with a 422 `Invalid datetime` that does not
        name the one it wants. A value with an offset (`2026-11-13T10:00:00+01:00`)
        is converted here to the same instant in UTC (`…09:00:00.000Z`); a value
        with NO timezone (`2026-11-13T10:00:00`, or a bare date) is refused
        before the call — convert local time to UTC, or give its offset.
        Search `filters` are not covered by this: see their own date format.

        `task` field notes: `due_at` is a date "YYYY-MM-DD", `due_time` an
        optional "HH:mm"; `description` is markdown; `recurrence_frequency` ∈
        weekday | weekly | biweekly | monthly | quarterly | yearly;
        `assigned_users` takes user IDs **or** emails (a list of either, or of
        {"id"}/{"email"} dicts) — never both in the same call, Folk rejects a
        mixed list; `is_public` false = visible only to the assignees. Omit
        `assigned_users` and the task lands on the API key's owner.

        Returns, per op —
            search: {"entity", "count", "results"} — plus "when" and
                "truncated" for interactions (and "past_count"/
                "upcoming_count" when when="all").
            get: the record.
            create solo: the created record, or {"dry_run": true, "would_create": {...}}.
            create bulk: {"total", "succeeded", "created": [{"index","id"}],
                "failed": [...]}, or dry_run: {"dry_run": true, "total",
                "would_create": [...], "failed": [...]}.
            update/add_to_group solo: the updated record, or {"dry_run": true,
                "id", "changes"|"fields", ...}.
            update/add_to_group bulk: {"total", "succeeded", "failed":
                [{"index","id","error"}]}, or dry_run: {"dry_run": true, "total",
                "would_update"|"would_add": [...], "failed": [...]}.
            delete solo: {} (or {"dry_run": true, "id", "would_delete", ...}).
            delete bulk: {"total", "succeeded", "failed": [{"index","id","error"}]},
                or dry_run: {"dry_run": true, "total", "would_delete": [...],
                "failed": [...]}.
            mark_done/mark_todo solo: the task, or {"dry_run": true, "id",
                "would_mark", "current"}.
            mark_done/mark_todo bulk: {"total", "succeeded", "failed":
                [{"index","id","error"}]}, or dry_run: {"dry_run": true,
                "total", "would_mark": [...], "failed": [...]}.

        Args:
            entity: "person" (a contact), "company", "deal" (or any other
                custom object collection — see `object_type`), "note",
                "interaction", "task" or "reminder" (deprecated — see above) —
                see each op for the ones it accepts (notes have no get-by-id,
                add_to_group is person/company only, mark_done/mark_todo are
                task only).
            op: search (default) | get | create | update | delete |
                add_to_group | mark_done | mark_todo.
            id: the record ID (the deal_id for a deal, tsk_… for a task,
                rmd_… for a reminder) — op="get", and solo mode of
                update/delete/add_to_group/mark_done/mark_todo.
            ids: record IDs — bulk mode of delete/add_to_group/mark_done/
                mark_todo (deal IDs for entity="deal").
            item: op="create" solo — fields for ONE record, see the per-entity
                shape below.
            items: op="create" bulk — fields for MULTIPLE records, same shape as
                `item`, one dict per record. op="update" bulk — one
                `{"id", "fields", "add_to_groups", "remove_from_groups"}` per
                record, same field vocabulary as below.
            fields: op="mark_done" — optionally {"completedAt":
                "2026-11-13T09:00:00Z"} (see 🕘 below); omitted, the task is
                stamped as completed now. Not accepted by op="mark_todo".
                op="update" solo — Folk API field names, camelCase (e.g.
                {"jobTitle": "CTO"}, {"industry": "SaaS"}, or custom fields of a
                deal). Optional if only `add_to_groups`/`remove_from_groups`
                are provided.
                **CUSTOM fields of a person/company** (e.g. a group's Status):
                pass them UNDER `customFieldValues`, keyed by group_id —
                `{"customFieldValues": {"<group_id>": {"Status": "Follow-up"}}}`.
                A custom field passed flat (`{"Status": …}`) is rejected (422
                "Unrecognized key"). The structure can be discovered via op="search"
                (customFieldValues grouped by group_id).
                ✅ **PARTIAL patch: the fields you don't cite are
                KEPT.** The Folk API replaces this object entirely; the tool
                re-reads the record and merges field by field before writing, so
                sending a single field no longer erases the others. To CLEAR a
                field, send it explicitly as `null` or `""` — a provided
                value is written as is. If the record cannot be
                re-read, the call is REFUSED rather than written blindly.
                ⚠️ A custom field may carry a value you NEVER
                sent: folk fills its "AI fields" on its own, a setting
                invisible from the API. ⚠️ This does NOT only happen on entering a
                group — measured on 2026-09-04 on an ordinary `op="update"` that
                sent only two fields: a third came back populated on
                re-read, `null` at the previous read-back. A value read back is
                therefore not proof of what YOU wrote. Re-read the record if the
                value commits you, and never conclude from a read-back that your
                write took effect.
            filters: op="search" — Field → value, matched with `like` (e.g.
                {"fullName": "Dupont", "emails": "@otomata.tech"} for people,
                {"name": "Otomata"} for companies).
                Folk's `like` is a case-insensitive **"contains"**,
                identical on people and companies (measured on 2026-09-07: a fragment
                inside a word, or straddling a space, returns the record).
                ⚠️ **The real trap of a `count=0` is the SCOPE**: the search
                only sees the Folk workspace of the credential resolved for the ACTIVE org —
                a record living in another org's workspace yields `count=0`. Yet `count=0` reads "this
                record doesn't exist" and the next move is a CREATION, hence a
                duplicate: **check the active org (`_org=`) before concluding it is
                absent and creating.** For another operator, pass
                {field: {op: value}} — op ∈ eq, not_eq, like, not_like, empty,
                not_empty, gt (dates), in / not_in (relations).
                `empty` / `not_empty` take no operand — write
                `{"emails": {"empty": True}}`, the value is ignored. A date is a
                CALENDAR DAY, `YYYY-MM-DD` (`{"createdAt": {"gt":
                "2026-10-05"}}`): Folk refuses a date-time (`…T00:00:00Z`) with
                a 422 `Invalid date` that does not name the format.
                **Relations (`groups`, `companies`): the value is the BARE id** —
                `{"groups": "grp_…"}` (= `group_id`), `{"groups": {"not_in":
                "grp_…"}}`, `{"groups": {"in": ["grp_…", "grp_…"]}}`. Do NOT nest
                it as `{"in": {"id": […]}}`: the tool adds the `[id]` of the Folk
                parameter itself, and the nested form is REFUSED. For `note` and
                `reminder`, Folk only has ONE filter: {"entity_id": "<id>"} (the
                person/company/deal the note or reminder hangs off) — or pass
                `entity_id` directly, same thing.
                For `interaction`, Folk exposes NO filter at all: use
                `entity_id` + `when`.
                For `task`, filters are `{field: {operator: value}}` over a
                CLOSED set — dueAt (eq/not_eq/gt/lt), createdAt (gt/lt),
                completedAt (empty/not_empty/gt/lt), assigneeUserId (in/not_in),
                entity (in/not_in). A bare value means `eq` on dueAt and `in` on
                entity/assigneeUserId; the two others demand an explicit
                operator. There is NO `like` here, unlike people/companies — an
                unknown field or operator is refused, naming what exists.
                Open tasks on someone: `entity_id="per_…"` +
                filters={"completedAt": {"empty": True}}. Overdue and still
                open: filters={"dueAt": {"lt": "<today>"},
                "completedAt": {"empty": True}}.
            max_results: op="search" — truncate the response (default 100).
                `count` reports the REAL total, so a `count` above the number of
                `results` means the list was cut. **`entity="interaction"` is
                the exception**: Folk offers no filter there and serves 30 per
                page, and one active contact can hold hundreds (>360 measured
                on a single record), so the search STOPS at `max_results`
                instead of draining the collection — `count` is what came back
                and `truncated: true` says more exists. Ask for a bigger
                `max_results` to go deeper; there is no way to ask Folk for
                "the newest 10" more cheaply than reading one page of 30.
            add_to_groups: op="update" — attach a **person** or **company**
                TO groups (`folk_group` for the IDs), without touching its
                other groups — solo mode only.
            remove_from_groups: op="update" — detach a **person** or
                **company** FROM groups, without touching its other groups —
                solo mode only.
            group_id: the group concerned by the call, meaning set by op/entity —
                op="search" on `person`/`company`: LIST THE MEMBERS of that group
                (get its id from `folk_group`) — e.g. audit the "Leads" pipeline;
                op="add_to_group": the TARGET group the record(s) join;
                REQUIRED for `entity="deal"` on every other op (the group where
                the deal lives — on create, all record(s) land in this one group,
                Folk deals aren't creatable across groups in a single call). Do
                NOT pass it for person/company outside the two cases above.
            entity_id: the PARENT record (person `per_…`, company `com_…`, or
                object `obj_…`) a sub-record hangs off. **Required** for
                entity="interaction" on search/get/update/delete — Folk has no
                "read interaction lit_…" on its own, an interaction is only
                addressable through the record it belongs to (in the query for
                get/delete, in the body for the PATCH). In bulk update each
                item may carry its own. Optional, and
                purely a convenience, on search for note/reminder/task (same as
                filters={"entity_id": …} / filters={"entity": …}). NOT used by
                op="create": there the parent record goes inside `item`.
            when: op="search" on entity="interaction" only — "past" (default:
                what already happened — the one you want for "what did we say
                to X"), "upcoming" (scheduled ahead), or "all" (both, with
                `past_count`/`upcoming_count` in the receipt). ⚠️ The default
                HIDES upcoming interactions: a `count` under "past" is not the
                total Folk holds for that record.
            object_type: custom-object collection name — `deal` only. Omit it:
                the tool auto-discovers this group's real deal object name
                (tries "deals", and on a 404 reads the correct one out of
                Folk's own error, which enumerates this group's valid entity
                types — same discovery `folk_group(op="custom_fields")` uses).
                Pass it explicitly only if the group has MULTIPLE non-person/
                company custom objects (e.g. Deals AND Events AND Projects) —
                auto-discovery can't guess which one is "deal" and will raise
                asking you to disambiguate.
            dry_run: write ops only — writes NOTHING. create: `would_create`
                preview, zero network calls. update / add_to_group: re-reads
                the current state and returns a diff `{"changes": {field: {"from",
                "to"}}}` (solo) or `would_update`/`would_add` (bulk). delete:
                re-reads each record and returns `would_delete` (the current record),
                to check what would be destroyed before doing it. For
                `entity="note"` (no get-by-id on Folk's side), degrades to
                `{"fields": ..., "current_available": False}` (update) or a
                `None` record + `"current_available": False` (delete) — preview
                without the "from". An interaction, on the other hand, always has its
                `entity_id` (all three ops require it), so its diff is
                always real.
        """
        def _require_deal_group() -> None:
            """Common to the 5 ops that accept entity="deal" (not add_to_group,
            which rejects it upfront): group_id first (raise if absent, BEFORE
            any network call), then resolves `object_type` once if
            the caller didn't give it. Do NOT resolve higher up (before the
            per-op dispatch): `add_to_group` rejects entity="deal" without ever
            needing group_id or the network — resolving there anyway
            would make a useless network call before a refusal that doesn't need one
            (experienced: it broke `test_add_to_group_deal_entity_rejected`, which
            expects no client call)."""
            nonlocal object_type
            if not group_id:
                raise _bad("group_id required for entity='deal'.")
            if object_type is None:
                object_type = _resolve_deal_object_type(_client(), group_id)

        # A parameter that does not apply to THIS (op, entity) pair must be
        # rejected, never ignored: silently swallowed, it makes the caller believe a
        # filter was applied or a parent was taken into account. It is the same family
        # of error as the claim corrected at the top of the module — a read that
        # returns less than what the caller thinks they asked for.
        if when is not None and not (op == "search" and entity == "interaction"):
            raise _bad("`when` only applies to op='search' on "
                       "entity='interaction' (past | upcoming | all).")
        if entity_id is not None and not (
                (op == "search" and entity in ("note", "reminder", "interaction",
                                               "task"))
                or (op in ("get", "delete", "update")
                    and entity == "interaction")):
            raise _bad(
                f"`entity_id` does not apply to op='{op}' entity='{entity}'. "
                + ("At creation, the owning entity is a FIELD of the record — "
                   "it can differ from one item to another in a batch — so "
                   "`item={'entity_id': 'per_…', …}`, not a parameter of "
                   "the call." if op == "create" else
                   "It is the OWNING entity of a note/interaction/task/"
                   "reminder (search), or the one that makes an interaction "
                   "addressable (get/update/delete). To list the members "
                   "of a group: `group_id`."))

        if op == "search":
            if entity not in _SEARCH_ENTITIES:
                raise _bad(f"op='search': entity must be one of {_SEARCH_ENTITIES}.")
            f = dict(filters or {})
            if entity == "interaction":
                if f:
                    raise _bad(
                        "op='search' entity='interaction': Folk exposes no "
                        "filter here — pass `entity_id` (the owning person/company) "
                        "and, if needed, `when`.")
                _need(entity_id, "entity_id", "search (entity='interaction')")
                c = _client()
                bucket = when or "past"
                # We fetch `max_results + 1` per bucket, not the whole
                # collection: Folk doesn't filter interactions and serves them
                # in pages of 30, so an active contact has hundreds
                # (measured: >360 on a single record). The +1 is used to KNOW
                # that more remain without paying one more page to say so.
                cap = max_results + 1
                past = (c.list_past_interactions(entity_id, max_items=cap)
                        if bucket in ("past", "all") else [])
                upcoming = (c.list_upcoming_interactions(entity_id, max_items=cap)
                            if bucket in ("upcoming", "all") else [])
                found = past + upcoming
                results = found[:max_results]
                out = {"entity": entity, "when": bucket, "count": len(results),
                       # `count` here is what is RETURNED, not the workspace
                       # total: on other entities we know the
                       # total because we fetched everything, here we deliberately
                       # stopped. Say so, rather than letting a
                       # `count` be read as an inventory.
                       "truncated": len(found) > max_results,
                       "results": results}
                if bucket == "all":
                    # The two lists come from distinct endpoints and the
                    # record doesn't say which one it came from: without this detail,
                    # an aggregated `count` can't be interpreted. `past` comes first,
                    # so the split is deduced from the position.
                    n_past = min(len(past), len(results))
                    out.update(past_count=n_past,
                               upcoming_count=len(results) - n_past)
                return out
            if entity == "task":
                if entity_id:
                    if "entity" in f:
                        raise _bad("pass `entity_id` OR filters={'entity': …}, "
                                   "not both.")
                    f["entity"] = entity_id
                c = _client()
                try:
                    found = c.list_tasks(f)
                except ValueError as e:
                    # The client validates fields AND operators against the Folk docs:
                    # its ValueError already names what exists, we return it as
                    # is rather than as an "internal error".
                    raise _bad(str(e))
                return {"entity": entity, "count": len(found),
                        "results": found[:max_results]}
            if entity in ("note", "reminder") and entity_id:
                f.setdefault("entity_id", entity_id)
            if entity in ("note", "reminder"):
                unknown = set(f) - _SUBRECORD_FILTERS
                if unknown:
                    raise _bad(
                        f"op='search' entity='{entity}': unknown filter(s) "
                        f"{sorted(unknown)} — Folk only exposes "
                        f"{sorted(_SUBRECORD_FILTERS)} on notes/reminders.")
                if group_id:
                    raise _bad(
                        f"op='search' entity='{entity}': Folk does not filter "
                        "notes/reminders by group — pass "
                        "filters={'entity_id': '<person/company/deal id>'}.")
            if entity == "deal":
                _require_deal_group()
            if entity in _GROUP_ENTITIES and group_id:
                # Group membership: the client translates it to filter[groups][in][id].
                f["groups"] = group_id
            if entity in ("person", "company", "deal"):
                from oto.tools.folk.client import filter_params
                try:
                    # Validation BEFORE any network call: the client rejects a
                    # filter shape it would turn into nonsense (oto#146)
                    # and its ValueError spells out the expected shape — returned as
                    # is rather than as an "internal error".
                    filter_params(f)
                except ValueError as e:
                    raise _bad(str(e))
            c = _client()
            if entity == "person":
                found = c.list_people(**f)
            elif entity == "company":
                found = c.list_companies(**f)
            elif entity == "deal":
                found = c.list_deals(group_id, object_type=object_type, **f)
            elif entity == "note":
                found = c.list_notes(**f)
            else:
                found = c.list_reminders(**f)
            return {"entity": entity, "count": len(found),
                    "results": found[:max_results]}

        if op == "get":
            if entity not in _GET_ENTITIES:
                raise _bad(
                    f"op='get': entity must be one of {_GET_ENTITIES} — Folk "
                    "has no get-by-id endpoint for notes (list them: "
                    "op='search', entity='note').")
            _need(id, "id", op)
            if entity == "deal":
                _require_deal_group()
            if entity in _ENTITY_ID_REQUIRED:
                _need(entity_id, "entity_id", f"{op} (entity='{entity}')")
            return _get_one(_client(), entity, id, group_id=group_id,
                            object_type=object_type, entity_id=entity_id)

        if op == "create":
            if (item is None) == (items is None):
                raise _bad("op='create': provide either `item` (a single record) or "
                           "`items` (several) — not both, not neither.")
            if entity not in _CREATE_ENTITIES:
                raise _bad(f"op='create': entity must be one of {_CREATE_ENTITIES}.")
            if entity == "deal":
                _require_deal_group()
            c = _client()
            if item is not None:
                result = _create_one(c, entity, item, group_id=group_id,
                                     object_type=object_type, dry_run=dry_run)
                return {"dry_run": True, **result} if dry_run else result
            if not dry_run and entity in _LOT:
                return _creer_par_lot(c, entity, items, group_id, object_type)
            results = _bulk_run(
                items, lambda it: _create_one(c, entity, it, group_id=group_id,
                                              object_type=object_type,
                                              dry_run=dry_run))
            failed = [{"index": i, "error": val} for i, ok, val in results if not ok]
            if dry_run:
                would_create = [{"index": i, **val} for i, ok, val in results if ok]
                return {"dry_run": True, "total": len(items),
                        "would_create": would_create, "failed": failed}
            created = [{"index": i, "id": val.get("id")} for i, ok, val in results if ok]
            return {"total": len(items), "succeeded": len(created),
                    "created": created, "failed": failed}

        if op == "update":
            if (id is None) == (items is None):
                raise _bad("op='update': provide either `id` (+ fields/add_to_groups/"
                           "remove_from_groups) for ONE record, or `items` for "
                           "several — not both, not neither.")
            if entity not in _UPDATE_ENTITIES:
                raise _bad(f"op='update': entity must be one of {_UPDATE_ENTITIES}.")
            if entity in _ENTITY_ID_REQUIRED and entity_id is None and not (
                    items and all("entity_id" in it for it in items)):
                # In a batch, each item may carry its own (several
                # interactions on different records); otherwise the
                # call's one is needed.
                _need(entity_id, "entity_id", f"{op} (entity='{entity}')")
            if entity == "deal":
                _require_deal_group()
            c = _client()
            if id is not None:
                result = _update_one(
                    c, entity, id, fields=fields, group_id=group_id,
                    object_type=object_type, add_to_groups=add_to_groups,
                    remove_from_groups=remove_from_groups, dry_run=dry_run,
                    entity_id=entity_id)
                return {"dry_run": True, **result} if dry_run else result
            if not dry_run and entity in _LOT:
                return _maj_par_lot(c, entity, items, group_id, object_type,
                                    entity_id)

            def _one(it):
                if "id" not in it:
                    raise _bad("each item must contain 'id'.")
                return _update_one(
                    c, entity, it["id"], fields=it.get("fields"),
                    group_id=group_id, object_type=object_type,
                    add_to_groups=it.get("add_to_groups"),
                    remove_from_groups=it.get("remove_from_groups"),
                    dry_run=dry_run, entity_id=it.get("entity_id", entity_id))

            results = _bulk_run(items, _one)
            failed = [{"index": i, "id": items[i].get("id"), "error": val}
                      for i, ok, val in results if not ok]
            if dry_run:
                would_update = [{"index": i, **val} for i, ok, val in results if ok]
                return {"dry_run": True, "total": len(items),
                        "would_update": would_update, "failed": failed}
            return {"total": len(items), "succeeded": len(items) - len(failed),
                    "failed": failed}

        if op == "delete":
            if (id is None) == (ids is None):
                raise _bad("op='delete': provide either `id` (a single record) or "
                           "`ids` (several) — not both, not neither.")
            if entity not in _DELETE_ENTITIES:
                raise _bad(f"op='delete': entity must be one of {_DELETE_ENTITIES}.")
            if entity == "deal":
                _require_deal_group()
            if entity in _ENTITY_ID_REQUIRED:
                # Required HERE rather than in `_delete_one`: otherwise a dry_run
                # without `entity_id` would return an empty preview instead of saying what
                # is missing, and the real call would fail right after.
                _need(entity_id, "entity_id", f"{op} (entity='{entity}')")
            c = _client()
            if id is not None:
                result = _delete_one(c, entity, id, group_id=group_id,
                                     object_type=object_type, dry_run=dry_run,
                                     entity_id=entity_id)
                return {"dry_run": True, **result} if dry_run else result
            results = _bulk_run(
                ids, lambda rid: _delete_one(c, entity, rid, group_id=group_id,
                                             object_type=object_type,
                                             dry_run=dry_run,
                                             entity_id=entity_id))
            failed = [{"index": i, "id": ids[i], "error": val}
                      for i, ok, val in results if not ok]
            if dry_run:
                would_delete = [{"index": i, **val} for i, ok, val in results if ok]
                return {"dry_run": True, "total": len(ids),
                        "would_delete": would_delete, "failed": failed}
            return {"total": len(ids), "succeeded": len(ids) - len(failed),
                    "failed": failed}

        if op in ("mark_done", "mark_todo"):
            if entity not in _MARK_ENTITIES:
                raise _bad(f"op='{op}': entity must be one of "
                           f"{_MARK_ENTITIES} — only the task has a notion of "
                           "completion (a reminder triggers on its own, it "
                           "does not complete).")
            if (id is None) == (ids is None):
                raise _bad(f"op='{op}': provide either `id` (a single task) "
                           "or `ids` (several) — not both, not "
                           "neither.")
            done = op == "mark_done"
            completed_at = (fields or {}).get("completedAt") if done else None
            unknown = set(fields or {}) - ({"completedAt"} if done else set())
            if unknown:
                raise _bad(
                    f"op='{op}': field(s) {sorted(unknown)} rejected here — "
                    + ("only `fields={'completedAt': …}` is accepted (default: "
                       "now)." if done
                       else "this op takes no fields."))
            if completed_at:
                # Checked ONCE, before `_bulk_run` and before the dry_run
                # preview: a bad value is one named refusal, not N identical
                # per-item 422s, and a dry run never looks fine where the
                # real call would fail.
                completed_at = _folk_datetime(completed_at, "completedAt")
            c = _client()
            if id is not None:
                result = _mark_one(c, id, done, completed_at=completed_at,
                                   dry_run=dry_run)
                return {"dry_run": True, **result} if dry_run else result
            results = _bulk_run(
                ids, lambda tid: _mark_one(c, tid, done,
                                           completed_at=completed_at,
                                           dry_run=dry_run))
            failed = [{"index": i, "id": ids[i], "error": val}
                      for i, ok, val in results if not ok]
            if dry_run:
                would_mark = [{"index": i, **val} for i, ok, val in results if ok]
                return {"dry_run": True, "total": len(ids),
                        "would_mark": would_mark, "failed": failed}
            return {"total": len(ids), "succeeded": len(ids) - len(failed),
                    "failed": failed}

        if op == "add_to_group":
            # Written by `_update_one(add_to_groups=[group_id])`: it is the one that
            # re-reads the current groups and rewrites the union (`groups` is
            # replace-all on a Folk PATCH). The contract returned to the caller is
            # in the docstring — here we only validate and route.
            _need(group_id, "group_id", op)
            if (id is None) == (ids is None):
                raise _bad("op='add_to_group': provide either `id` (a single record) "
                           "or `ids` (several) — not both, not "
                           "neither.")
            if entity not in _GROUP_ENTITIES:
                raise _bad(f"op='add_to_group': entity must be one of "
                           f"{_GROUP_ENTITIES}.")
            c = _client()
            if id is not None:
                result = _update_one(c, entity, id, add_to_groups=[group_id],
                                     dry_run=dry_run)
                return {"dry_run": True, **result} if dry_run else result
            if not dry_run and entity in _LOT:
                # Re-read each record (paced), union its groups, ONE batch update.
                return _maj_par_lot(c, entity,
                                    [{"id": rid, "add_to_groups": [group_id]}
                                     for rid in ids], None, object_type, None)
            results = _bulk_run(
                ids, lambda rid: _update_one(c, entity, rid, add_to_groups=[group_id],
                                             dry_run=dry_run))
            failed = [{"index": i, "id": ids[i], "error": val}
                      for i, ok, val in results if not ok]
            if dry_run:
                would_add = [{"index": i, **val} for i, ok, val in results if ok]
                return {"dry_run": True, "total": len(ids), "would_add": would_add,
                        "failed": failed}
            return {"total": len(ids), "succeeded": len(ids) - len(failed),
                    "failed": failed}

        raise _bad("op must be 'search', 'get', 'create', 'update', 'delete', "
                   "'add_to_group', 'mark_done' or 'mark_todo'")

    # --- groups + group custom fields + group members -------------------------
    #
    # No "get a group" on the Folk API side (only list/create/update exist):
    # the dry_run of op="update"/"remove_member"/"update_member" re-reads list_groups()/
    # list_group_members() and filters on the id, same limitation already met
    # on notes/reminders (no server filter, filtered client-side).

    @mcp.tool()
    def folk_group(
        op: Literal[
            "list", "create", "update",
            "custom_fields", "get_custom_field",
            "create_custom_field", "update_custom_field",
            "members", "add_member", "remove_member", "update_member",
        ] = "list",
        group_id: Optional[str] = None,
        entity_type: str = "person",
        custom_field_name: Optional[str] = None,
        name: Optional[str] = None,
        visibility: Optional[Literal["public", "private"]] = None,
        custom_field: Optional[dict] = None,
        fields: Optional[dict] = None,
        user_id: Optional[str] = None,
        role: Optional[Literal["admin", "contributor", "reader"]] = None,
        dry_run: bool = False,
    ) -> dict:
        """A Folk group (a folder of people/companies/deals), the custom fields
        defined on it, and its members — list/create/update any of the three.

        `op`:
        - **"list"** (default): list all groups in the Folk workspace.
        - **"create"**: create a group (`name` + `visibility`). A default "All
          people" table view is created automatically by Folk.
        - **"update"**: PATCH a group (`group_id` + `name` and/or `visibility`).
        - **"custom_fields"**: list the custom fields defined on a group for an
          entity type (`group_id` + `entity_type`).
        - **"get_custom_field"**: read one custom field by name (`group_id` +
          `entity_type` + `custom_field_name`).
        - **"create_custom_field"**: create a custom field on a group for an
          entity type (`group_id` + `entity_type` + `custom_field`).
        - **"update_custom_field"**: PATCH a custom field (`group_id` +
          `entity_type` + `custom_field_name` + `fields`).
        - **"members"**: list a group's members (`group_id`) — id, full name,
          email, role. On a **public** group this lists EVERY workspace user
          (Folk auto-membership, role always "admin"), not just people
          explicitly added — see note below.
        - **"add_member"**: add a workspace user to a group (`group_id` +
          `user_id` + `role`). Get `user_id` from `folk_user(op="list")`. A
          no-op on a public group (see note below) — the target is already an
          implicit member.
        - **"remove_member"**: remove a member from a group (`group_id` +
          `user_id`).
        - **"update_member"**: change a member's role (`group_id` + `user_id` +
          `role`).

        Note: Folk has no delete endpoint for a group or a custom field — remove
        either from the Folk app, not from here. A group MEMBER, unlike the
        group itself, can be removed via the API (`op="remove_member"`).

        Note: **`visibility="public"` makes membership implicit and workspace-
        wide** (confirmed live, 2026-08-17) — EVERY workspace user shows up in
        `op="members"` with role "admin", whether or not anyone explicitly
        added them. `op="add_member"`/`"remove_member"`/`"update_member"` only
        do something meaningful on a **private** group (explicit membership).
        To manage membership deliberately, create/update the group with
        `visibility="private"` first.

        Args:
            op: list (default) | create | update | custom_fields |
                get_custom_field | create_custom_field | update_custom_field |
                members | add_member | remove_member | update_member.
            group_id: required for every op except "list"/"create".
            entity_type: "person", "company", or a custom object's DISPLAY
                name — required for every custom-field op (default "person").
                Not used by member ops. ⚠️ Only "person"/"company" are fixed.
                Everything else is a **custom object each Folk customer names
                themselves** — "Deals" is just what THIS workspace happens to
                call theirs; another workspace's equivalent could be
                "Opportunities", "Transactions", singular, translated,
                anything. Never assume a name. Discover it: call
                `op="custom_fields"` with any placeholder entity_type — Folk's
                404 names the group's REAL entity types (`"Available entity
                types are: ..."`), then reissue with the right one. This is
                also the exact, case-sensitive display name — NOT the
                lowercase URL slug `folk_record`/`folk_webhook` call
                `object_type` (confirmed live: on one real workspace,
                `object_type="deals"` 404s while `object_type="Deals"`,
                matching this workspace's custom object name, returns
                records) — the two params can differ even for the same
                collection.
            custom_field_name: op="get_custom_field"/"update_custom_field" — the
                field's `name` (custom fields have no separate id; `name` IS the
                identifier Folk matches on, from op="custom_fields").
            name: op="create"/"update" — group name (1-255 chars).
            visibility: op="create"/"update" — "public" (every workspace user
                is an implicit member, role "admin" — see note above) or
                "private" (only explicitly added members). Required on create.
            custom_field: op="create_custom_field" — the field body, discriminated
                by `type`:
                  textField / dateField / userField / contactField / objectField:
                    `{"type": ..., "name": ...}`
                  singleSelect / multipleSelect:
                    `{"type": ..., "name": ..., "options": [{"label", "color"}]}`
                    (color is one of #5738ff #20cea9 #f54e50 #f2b934 #879aab
                    #de4a96 #4a90e2 #f5a623)
                  numericField:
                    `{"type": "numericField", "name": ...,
                      "config": {"format": "default"|"percent"|"currency"|"none"|"number",
                                 "decimals"?: 0-5, "currency"?: "EUR"}}`
                    (currency required only when format="currency")
            fields: op="update_custom_field" — any of `name`, `config` (same
                shape as `custom_field.config` above), `addOptions`
                (`[{"label", "color"}]`, 1-100), `removeOptions` (option ids,
                1-100), `updateOptions` (`[{"id", "label"?, "color"?}]`, 1-100) —
                only the given ones change.
            user_id: op="add_member"/"remove_member"/"update_member" — the
                workspace user id (from `folk_user(op="list")`, NOT `folk_group
                (op="members")` — a group member IS a workspace user).
            role: op="add_member"/"update_member" — required, one of "admin",
                "contributor", "reader". A LIST can also show "owner" (Folk's
                workspace owner) but it can't be SET through this API.
            dry_run: op="create"/"create_custom_field"/"add_member" — preview
                (`would_create`/`would_add`), zero network calls. op="update"/
                "update_custom_field"/"update_member" — diff `{"changes":
                {field: {"from", "to"}}}` against the current record.
                op="remove_member" — preview (`would_remove`) of the member
                that would be removed, zero network calls.
        """
        if op == "list":
            return {"groups": _client().list_groups()}

        if op == "create":
            _need(name, "name", op)
            _need(visibility, "visibility", op)
            if dry_run:
                return {"dry_run": True,
                         "would_create": {"name": name, "visibility": visibility}}
            return _client().create_group(name, visibility)

        if op == "update":
            _need(group_id, "group_id", op)
            group_fields: dict = {}
            if name is not None:
                group_fields["name"] = name
            if visibility is not None:
                group_fields["visibility"] = visibility
            if not group_fields:
                raise _bad("op='update' requires name and/or visibility "
                           "(not `fields` — reserved for op='update_custom_field').")
            c = _client()
            if dry_run:
                current = next((g for g in c.list_groups() if g.get("id") == group_id), None)
                if current is None:
                    raise _bad(f"group_id {group_id!r} introuvable (folk_group op='list').")
                return {"dry_run": True, "group_id": group_id,
                         "changes": {k: {"from": current.get(k), "to": v}
                                     for k, v in group_fields.items()}}
            return c.update_group(group_id, **group_fields)

        if op == "custom_fields":
            _need(group_id, "group_id", op)
            return {"custom_fields": _client().get_group_custom_fields(
                group_id, entity_type)}

        if op == "get_custom_field":
            _need(group_id, "group_id", op)
            _need(custom_field_name, "custom_field_name", op)
            return _client().get_group_custom_field(group_id, entity_type, custom_field_name)

        if op == "create_custom_field":
            _need(group_id, "group_id", op)
            _need(custom_field, "custom_field", op)
            _reject_reserved_keys(custom_field, "custom_field", op)
            if dry_run:
                return {"dry_run": True, "would_create": custom_field}
            return _client().create_group_custom_field(group_id, entity_type, **custom_field)

        if op == "update_custom_field":
            _need(group_id, "group_id", op)
            _need(custom_field_name, "custom_field_name", op)
            if not fields:
                raise _bad("op='update_custom_field' requiert fields : au moins un "
                           "champ (name, config, addOptions, removeOptions, "
                           "updateOptions).")
            _reject_reserved_keys(fields, "fields", op)
            c = _client()
            if dry_run:
                current = c.get_group_custom_field(group_id, entity_type, custom_field_name)
                return {"dry_run": True, "custom_field_name": custom_field_name,
                         "changes": {k: {"from": current.get(k), "to": v}
                                     for k, v in fields.items()}}
            return c.update_group_custom_field(group_id, entity_type, custom_field_name, **fields)

        if op == "members":
            _need(group_id, "group_id", op)
            return {"members": _client().list_group_members(group_id)}

        if op == "add_member":
            _need(group_id, "group_id", op)
            _need(user_id, "user_id", op)
            _need(role, "role", op)
            if dry_run:
                return {"dry_run": True, "would_add": {"id": user_id, "role": role}}
            return _client().add_group_member(group_id, user_id, role)

        if op == "remove_member":
            _need(group_id, "group_id", op)
            _need(user_id, "user_id", op)
            c = _client()
            if dry_run:
                current = next(
                    (m for m in c.list_group_members(group_id) if m.get("id") == user_id), None)
                if current is None:
                    raise _bad(f"user_id {user_id!r} not found in this group "
                               "(folk_group op='members').")
                return {"dry_run": True, "would_remove": current}
            return c.remove_group_member(group_id, user_id)

        if op == "update_member":
            _need(group_id, "group_id", op)
            _need(user_id, "user_id", op)
            _need(role, "role", op)
            c = _client()
            if dry_run:
                current = next(
                    (m for m in c.list_group_members(group_id) if m.get("id") == user_id), None)
                if current is None:
                    raise _bad(f"user_id {user_id!r} not found in this group "
                               "(folk_group op='members').")
                return {"dry_run": True, "user_id": user_id,
                         "changes": {"role": {"from": current.get("role"), "to": role}}}
            return c.update_group_member(group_id, user_id, role)

        raise _bad("op must be one of 'list', 'create', 'update', 'custom_fields', "
                   "'get_custom_field', 'create_custom_field', 'update_custom_field', "
                   "'members', 'add_member', 'remove_member', 'update_member'")

    # --- users (workspace members, read-only) -------------------------------

    @mcp.tool()
    def folk_user(op: Literal["list", "get"] = "list", user_id: str = "me") -> dict:
        """A Folk workspace user (member) — list them, or fetch one.

        `op`:
        - **"list"** (default): list the workspace users (members) — useful to
          resolve owners/assignees.
        - **"get"**: fetch a workspace user by ID. `user_id="me"` (default)
          returns the authenticated user — call it to attribute an action to the
          current user.

        Args:
            op: list (default) | get.
            user_id: op="get" — the user ID, or "me" (default).
        """
        if op == "list":
            return {"users": _client().list_users()}
        if op == "get":
            return _client().get_user(user_id)
        raise _bad("op must be 'list' or 'get'")

    # --- webhooks -------------------------------------------------------------
    #
    # Global resource (no `entity`, no group_id/object_type, no bulk
    # mode — a workspace has few). `dry_run` follows the same convention as
    # `folk_record` (`would_create` preview on create, `changes` diff on
    # update, no mutating network call).

    @mcp.tool()
    def folk_webhook(
        op: Literal["list", "create", "update"] = "list",
        webhook_id: Optional[str] = None,
        name: Optional[str] = None,
        target_url: Optional[str] = None,
        subscribed_events: Optional[list[dict]] = None,
        fields: Optional[dict] = None,
        dry_run: bool = False,
    ) -> dict:
        """A Folk webhook — list, create, update. Folk POSTs an event payload to
        `target_url` each time one of the subscribed events fires.

        `op`:
        - **"list"** (default): list all webhooks configured on this Folk
          workspace: target URL, status, and which events/filters each one
          subscribes to.
        - **"create"**: create a webhook (`name` + `target_url` +
          `subscribed_events`).
        - **"update"**: PATCH a webhook (`webhook_id` + `fields`) — only the
          given fields change.

        Before creating with a filter, call `folk_group` (op="list" for
        `groupId`, op="custom_fields" for the custom field name used in `path`)
        to get real workspace values — don't guess them.

        Note: on create, the response's `signingSecret` is returned in FULL only
        there — Folk only ever shows a redacted version afterwards, so save it
        now if you need to verify payload signatures.

        Note: filters only exist through this API — editing a webhook's events
        from Folk's own settings UI afterwards silently drops them.

        Args:
            op: list (default) | create | update.
            webhook_id: op="update" — the webhook ID (wbk_…, from op="list").
            name: op="create" — friendly name (max 255 chars).
            target_url: op="create" — public HTTPS URL that will receive the
                event (max 2048 chars).
            subscribed_events: op="create" — 1-20 items, each
                `{"eventType": ..., "filter": {...}}`.
                eventType — one per entity, by lifecycle:
                  person: created, updated, deleted, groups_updated,
                    workspace_interaction_metadata_updated
                  company: created, updated, deleted, groups_updated
                  object (deals AND any custom object_type): created, updated, deleted
                  note: created, updated, deleted
                  reminder: created, updated, deleted, triggered
                (full values are "person.created", "object.updated", etc.)
                filter (optional, all keys optional):
                  groupId — only for entities in this group (`folk_group`).
                    For object.* this is a sibling of `path`, never repeated
                    inside it.
                  objectType — for object.* events, scope to one collection
                    (e.g. "Deals" vs a custom object_type). Confirmed against a
                    live workspace: this is the exact display name, same as
                    `folk_group`'s `entity_type` — NOT the lowercase slug
                    `folk_record(entity="deal", object_type=...)` historically
                    defaulted to (that tool now auto-discovers it; here, pass
                    the real name yourself).
                  path + value — for *.updated events, fire only when the
                    attribute at `path` changes to `value`. `path` covers both
                    plain attributes and custom fields, and its shape differs
                    by entity:
                      plain attribute (any entity): `["firstName"]`, `["name"]`
                      person/company custom field:
                        `["customFieldValues", groupId, fieldName]` (3 segments
                        — the group id is repeated here, inside the path)
                      object/deal custom field:
                        `["customFieldValues", fieldName]` (2 segments — no
                        group id in path; use the sibling `filter.groupId`
                        instead)
                    fieldName is the field's `name` from
                    `folk_group(op="custom_fields")` (custom fields have no
                    separate id — `name` IS the identifier Folk matches on).
            fields: op="update" — Folk's raw API field names, camelCase — same
                vocabulary caveat as `folk_record(op="update")`: name, targetUrl,
                subscribedEvents (REPLACES the full list, not a merge/add — call
                op="list" first and resend the existing entries you want to
                keep), status ("active"|"inactive" — pause without deleting).
                Same eventType/filter shape as op="create".
            dry_run: if true, writes nothing — op="create" returns a preview
                (`would_create`), zero network calls; op="update" returns a diff
                `{"changes": {field: {"from", "to"}}}` against the current
                webhook.
        """
        if op == "list":
            return {"webhooks": _client().list_webhooks()}

        if op == "create":
            _need(name, "name", op)
            _need(target_url, "target_url", op)
            _need(subscribed_events, "subscribed_events", op)
            _validate_subscribed_events(subscribed_events)
            if dry_run:
                return {"dry_run": True, "would_create": {
                    "name": name, "targetUrl": target_url,
                    "subscribedEvents": subscribed_events,
                }}
            return _client().create_webhook(name, target_url, subscribed_events)

        if op == "update":
            _need(webhook_id, "webhook_id", op)
            if not fields:
                raise _bad("op='update' requires fields: at least one field to "
                           "update (name, targetUrl, subscribedEvents, status).")
            if "subscribedEvents" in fields:
                _validate_subscribed_events(fields["subscribedEvents"])
            c = _client()
            if dry_run:
                current = c.get_webhook(webhook_id)
                return {"dry_run": True, "id": webhook_id,
                        "changes": {k: {"from": current.get(k), "to": v}
                                    for k, v in fields.items()}}
            return c.update_webhook(webhook_id, **fields)

        raise _bad("op must be 'list', 'create' or 'update'")
