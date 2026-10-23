"""Active = usable — a member's selection follows the credentials they can use.

**The rule** (22/10/2026). A connector is ACTIVE for a member, in an org, exactly when
a credential is usable for them there: their own key, their team's, the org's, the
tenant's or the platform's — or none is needed (`secret_kind` `none` / `settings`).
Nothing usable → not active. There is no on/off switch any more: the dashboard dropped
it, and this module is what makes the rule hold for EVERY member and every hosted run,
not only for the people who happen to open the connectors page.

**How.** The selection table (`connectors/selection.py`) stays the mounting truth —
cheap, read at every handshake by `session_visibility`. This module RECONCILES it from
the credential snapshot (`access.status_for`, the one `/api/me` serves), throttled per
`(sub, org)`: computing credentials at every `initialize` would cost a second per
session, and a runner opens one per call.

- `verdict` — the rule, one connector at a time: `usable` | `not_usable` | `unknown`.
- `plan` — PURE: what to activate, what to pause, given the snapshot and the selection.
- `reconcile` — reads, plans, writes in ONE transaction, sets the marker.
- `claim` / `run_if_due` / `invalidate` — the throttle: a marker per `(sub, org)`,
  claimed before computing (one session per burst), dropped by every write
  to the vault (`credentials_store._upsert/_delete`) so a new key shows at the next
  session, and aged out after `TTL` for what the vault doesn't see (a platform grant).

⚠️ **Fail inert, never fail closed.** No snapshot, an exception, an entry that is
missing or still pending (`pending_action`: a hosted channel whose account isn't
linked yet, an authorisation left half-way) ⟹ the row is left as it is. A pass whose
team read failed pauses nothing. One narrow window remains (the snapshot degrading
after that read): a team-keyed connector can then be paused for at most `TTL`, and the
next pass reactivates it.

⚠️ A refused key (`health_ko`) still counts as usable: it is there to be fixed, and the
agent should see the failure rather than not see the tool.

Convention: self-managing like `selection` (opens its own connection); `invalidate`
receives the caller's `conn` (it runs inside the vault write's transaction).
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Mapping, Optional

logger = logging.getLogger(__name__)

USABLE = "usable"
NOT_USABLE = "not_usable"
UNKNOWN = "unknown"

# The cascade levels that ANSWER (`access.status_for`): a key resolves for the member.
# `over_quota` is the platform level whose day is spent — the key exists, the quota is
# readiness, not selection (same reading as `credential_presence._PALIER`).
_RESOLVED_MODES = frozenset({"user", "group", "org", "tenant", "platform", "over_quota"})
# Nothing to bring: open data, and the cards that only hold optional settings.
_KEYLESS = frozenset({"none", "settings"})

# How long a reconciliation holds when nothing in the vault moved. The vault invalidates
# on every write; this ages out what it can't see (a platform key grant, a quota reset).
TTL = timedelta(minutes=30)

# Provenance of a row written by the rule (selection.ORIGINS): neither the member nor an
# org gesture placed it — the credential did. A kit removal never touches it (it removes
# `kit` rows only).
ORIGIN = "cle"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS connector_selection_reconciled (
    sub           TEXT        NOT NULL,
    org_id        BIGINT      NOT NULL CONSTRAINT connector_selection_reconciled_org_reelle
                                       CHECK (org_id > 0),
    reconciled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (sub, org_id)
);
"""


def init_schema(conn) -> None:
    """Creates the marker table. Idempotent, additive (boot DDL)."""
    conn.execute(_SCHEMA)


# --- the rule (pure) ------------------------------------------------------------

def verdict(connector, entry: Optional[Mapping]) -> str:
    """The rule for ONE connector. `connector` = its registry declaration (or None),
    `entry` = its row of `status_for(...)["providers"]` (or None).

    - nothing mounts for it (no namespace: a model key used by agents, never by a tool)
      → `unknown`: its selection means nothing, leave it;
    - `secret_kind` none / settings → `usable`, whatever the snapshot says;
    - no snapshot row → `unknown` (not computed is not "nothing resolves");
    - a step still pending → `unknown` (the dashboard says "one step left", it doesn't
      switch anything);
    - a level answers → `usable` (a refused key included);
    - `forbidden` → `not_usable`."""
    if connector is None or not getattr(connector, "namespaces", None):
        return UNKNOWN
    if connector.secret_kind in _KEYLESS:
        return USABLE
    if entry is None:
        return UNKNOWN
    if entry.get("pending_action"):
        return UNKNOWN
    mode = entry.get("mode")
    if mode in _RESOLVED_MODES:
        return USABLE
    if mode == "forbidden":
        return NOT_USABLE
    return UNKNOWN


def plan(snapshot: Mapping[str, Mapping], selection: Mapping[str, str],
         exposed: set[str], registry: Mapping) -> tuple[list[str], list[str]]:
    """`(to_activate, to_pause)` — PURE, the whole rule over a member's catalogue.

    Only connectors EXPOSED to the member in this org are touched: an unexposed one
    mounts nothing whatever its row says, and installing it would put back what the
    org cut. Usable and not active → activate (from nothing, from a pause, from a
    removal). Active and not usable → pause. Everything else stays."""
    from . import selection as sel

    to_activate: list[str] = []
    to_pause: list[str] = []
    for name in sorted(exposed):
        v = verdict(registry.get(name), snapshot.get(name))
        state = selection.get(name)
        if v == USABLE and state != sel.ACTIVE:
            to_activate.append(name)
        elif v == NOT_USABLE and state == sel.ACTIVE:
            to_pause.append(name)
    return to_activate, to_pause


# --- the throttle -------------------------------------------------------------------

def due(sub: str, org_id: int) -> bool:
    """Must this `(sub, org)` be reconciled now? No marker, or one older than `TTL`."""
    from .. import db

    # The age is judged by the database (rows come back with timestamps as text here).
    with db._connect() as conn:
        row = conn.execute(
            "SELECT reconciled_at > NOW() - %s AS fresh FROM connector_selection_reconciled "
            "WHERE sub = %s AND org_id = %s",
            (TTL, sub, org_id),
        ).fetchone()
    return row is None or not row["fresh"]


def claim(sub: str, org_id: int) -> bool:
    """Takes this `(sub, org)`'s turn, in ONE statement: True when the caller must
    reconcile now, False when the marker is fresh — or another session just took it.

    Claimed BEFORE computing, not stamped after: on a first deploy (no marker anywhere)
    or after a key write that clears every marker, a fleet of hosted runs for the same
    person opens its sessions together, and each would otherwise pay its own snapshot
    (~seconds of database work) for the same answer. One wins; the others mount what
    the table already holds. The winner gives the turn back if it fails
    (`release`), so the next session retries."""
    from .. import db

    with db._connect() as conn:
        row = conn.execute(
            "INSERT INTO connector_selection_reconciled (sub, org_id) VALUES (%s, %s) "
            "ON CONFLICT (sub, org_id) DO UPDATE SET reconciled_at = NOW() "
            "WHERE connector_selection_reconciled.reconciled_at <= NOW() - %s "
            "RETURNING 1 AS won",
            (sub, org_id, TTL),
        ).fetchone()
    return row is not None


def release(sub: str, org_id: int) -> None:
    """Gives a claimed turn back (the reconciliation failed): the next session retries."""
    from .. import db

    with db._connect() as conn:
        conn.execute("DELETE FROM connector_selection_reconciled WHERE sub = %s AND org_id = %s",
                     (sub, org_id))


def run_if_due(sub: str, org_id: int, group_id: Optional[int]) -> Optional[tuple[list[str], list[str]]]:
    """The handshake's entry point: claim, reconcile, give the turn back on failure.
    Returns `None` when it wasn't this session's turn. Raises what `reconcile` raised
    (the caller logs and mounts what the table holds)."""
    if not claim(sub, org_id):
        return None
    try:
        return reconcile(sub, org_id, group_id)
    except Exception:
        release(sub, org_id)
        raise


def invalidate(conn, entity_type: str, entity_id: str) -> None:
    """A vault row was written or deleted: the members it can reach are reconciled at
    their next session. Receives the WRITE's `conn` — same transaction, a write that
    rolls back invalidates nothing.

    `member` (`<org>:<sub>`) → that member in that org; `user` (an OAuth account, cross
    org) → that person everywhere; `org` → everyone in the org; anything else (a team,
    a tenant, the platform) → everyone: those keys move rarely, and the next session of
    each member re-reads its snapshot once."""
    if entity_type == "member" and ":" in entity_id:
        org, sub = entity_id.split(":", 1)
        conn.execute("DELETE FROM connector_selection_reconciled WHERE sub = %s AND org_id = %s",
                     (sub, int(org)))
    elif entity_type == "user":
        conn.execute("DELETE FROM connector_selection_reconciled WHERE sub = %s", (entity_id,))
    elif entity_type == "org":
        conn.execute("DELETE FROM connector_selection_reconciled WHERE org_id = %s",
                     (int(entity_id),))
    else:
        conn.execute("DELETE FROM connector_selection_reconciled")


# --- the write ------------------------------------------------------------------------

def reconcile(sub: str, org_id: int, group_id: Optional[int]) -> tuple[list[str], list[str]]:
    """Brings `(sub, org)`'s selection in line with the rule, sets the marker, and
    returns `(activated, paused)`.

    Raises on a snapshot or exposure read error — the CALLER decides (the handshake
    logs and mounts what is already there): nothing is written in that case."""
    from .. import access, db, group_store, providers
    from . import activation, selection as sel

    # `status_for` is fail-open INSIDE: a hiccup reading the member's teams leaves them
    # out, and a key held only by a team then reads `forbidden`. Pausing on that would be
    # the "pause on a transient" this module refuses. The teams are read here first: if
    # that read fails, this pass ACTIVATES what it can but PAUSES nothing. (A failure
    # landing between this read and the snapshot's own can still slip through — the next
    # pass, at most `TTL` later, puts it back.)
    try:
        group_store.list_groups_for_user(sub, org_id)
        trusted = True
    except Exception as e:
        logger.warning("teams unreadable for %s in org %s: no pause this pass: %s", sub, org_id, e)
        trusted = False
    snapshot = access.status_for(sub, org=org_id, group=group_id)["providers"]
    exposed = activation.exposed_for(org_id, group_id)
    current = sel.list_selection(sub, org_id)
    to_activate, to_pause = plan(snapshot, current, exposed, providers.REGISTRY)
    if not trusted:
        to_pause = []
    with db._connect() as conn:
        for name in to_activate:
            conn.execute(
                "INSERT INTO user_selected_connectors (sub, org_id, connector, state, origin) "
                "VALUES (%s, %s, %s, 'active', %s) "
                "ON CONFLICT (sub, org_id, connector) DO UPDATE SET state = 'active', "
                "selected_at = NOW(), origin = EXCLUDED.origin",
                (sub, org_id, name, ORIGIN),
            )
            # A removal the member made no longer holds against a usable connector.
            conn.execute(
                "DELETE FROM connector_selection_removed "
                "WHERE sub = %s AND org_id = %s AND connector = %s",
                (sub, org_id, name),
            )
        for name in to_pause:
            conn.execute(
                "UPDATE user_selected_connectors SET state = 'paused', selected_at = NOW() "
                "WHERE sub = %s AND org_id = %s AND connector = %s AND state = 'active'",
                (sub, org_id, name),
            )
        conn.execute(
            "INSERT INTO connector_selection_reconciled (sub, org_id) VALUES (%s, %s) "
            "ON CONFLICT (sub, org_id) DO UPDATE SET reconciled_at = NOW()",
            (sub, org_id),
        )
    if to_activate or to_pause:
        logger.info("selection reconciled for %s in org %s: +%d active, %d paused",
                    sub, org_id, len(to_activate), len(to_pause))
    return to_activate, to_pause


def is_usable(sub: str, org_id: int, group_id: Optional[int], name: str) -> bool:
    """The rule for ONE connector, read live — for the member gestures that would
    contradict it (`connectors.pause` / `unselect` refuse on a usable connector)."""
    from .. import access, providers

    snapshot = access.status_for(sub, org=org_id, group=group_id)["providers"]
    return verdict(providers.REGISTRY.get(name), snapshot.get(name)) == USABLE
