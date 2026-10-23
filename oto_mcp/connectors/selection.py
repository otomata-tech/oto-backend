"""Connector selection by a member — “marketplace” model (ADR 0019).

**Three distinct facts, not to be confused** (cf. ADR 0019):
- *Exposure* = `connector_activation` (who CAN see, platform governance,
  deny-by-default, admin-only) — the **ceiling**.
- *Proposal* = `orgs.default_connectors` (what the org RECOMMENDS, advisory).
- *Selection* = **this table** (what the MEMBER installs in their space), per
  `(sub, org_id)`. This is the new state the marketplace introduces.

Three member states, per connector:
- **not selected**: no row → stays in the library/catalog.
- **selected-active** (`state='active'`): tools visible (normal visibility).
- **selected-paused** (`state='paused'`): installed but tools hidden.

The table is the **source of truth for selection**; *visibility* stays
computed (`tool_visibility.is_tool_visible` unchanged) — the middleware derives an
additional masking from it (pause/non-selection), never on PROTECTED_TOOLS nor
grant-only. `org_id=0` = personal space (ADR 0015 sentinel), like `user_disabled_tools`.

NB rung **B1**: table + helpers only, NO caller reads it yet — deployment canary
(no-behavior-change). The wiring (reading `/api/me/connectors`, mutation,
pause masking in the middleware) follows in B3/B4/B5.

Convention: self-managing (they open their own connection, like `connector_activation`).
Only `init_schema` receives the `conn` of the `db.init_db` transaction. No oto_mcp
import at module level (leaf) — `db` imported lazily.
"""
from __future__ import annotations

# Closed values of the selection state.
ACTIVE = "active"
PAUSED = "paused"
STATES = (ACTIVE, PAUSED)

# PROVENANCE of an installation (ADR 0050 §E7, oto#166) — who placed the row.
# Without it, a seeded row, one placed by the kit and one chosen by the member were
# indistinguishable, and no removal rule was tenable: removing from the kit what the kit
# placed requires knowing WHO placed it. `inconnue` = any row written before the trace,
# or by code that doesn't know about it (production during the preprod→tag window:
# shared database) — no org gesture ever removes it.
SOCLE = "socle"        # the platform `default_active` base, at seeding
KIT = "kit"            # the org's kit, at seeding or on the admin's gesture
ADMIN = "admin"        # named push from an admin to ONE member
MEMBRE = "membre"      # the member themselves (installation, or resuming after a pause)
INCONNUE = "inconnue"  # predates the trace — never removed by an org gesture
CLE = "cle"            # the credential: a usable key made it active (`connectors/usable.py`)
ORIGINS = (SOCLE, KIT, ADMIN, MEMBRE, INCONNUE, CLE)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_selected_connectors (
    sub         TEXT   NOT NULL,
    -- The org where the selection applies. Never `0`: the old “personal, no org”
    -- sentinel (ADR 0015) hasn't been read by any surface since ADR 0030 §8 — a row
    -- placed there was invisible (#959). The database refuses it, and with no default: a
    -- write that forgets the org fails instead of filing itself under `0`.
    org_id      BIGINT NOT NULL CONSTRAINT user_selected_connectors_org_reelle
                                CHECK (org_id > 0),
    connector   TEXT   NOT NULL,             -- connector name (providers/ registry)
    state       TEXT   NOT NULL DEFAULT 'active',  -- 'active' | 'paused'
    selected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Provenance (ADR 0050 §E7): socle | kit | admin | membre | inconnue. Also set
    -- by `ALTER … ADD COLUMN` in `db/_init.py` for the SHARED prod/preprod database,
    -- where this `CREATE TABLE` is skipped — both definitions are identical (the
    -- `test_boot_order_replay` ratchet compares default and nullability).
    origin      TEXT   NOT NULL DEFAULT 'inconnue',
    PRIMARY KEY (sub, org_id, connector)
);
-- The member's REMOVAL, kept with its date (ADR 0050 §E6/§E7). A separate table
-- and not one more state in `user_selected_connectors`: the code served BEFORE this
-- batch (shared database) reads each row of that table as “installed or paused”,
-- and would serve a state it doesn't know. Here, it sees nothing. A removal remains
-- a DELETE from the selection, paired with this trace; a member's `select`/`pause`
-- erases it (their last gesture is no longer a removal).
CREATE TABLE IF NOT EXISTS connector_selection_removed (
    sub        TEXT   NOT NULL,
    org_id     BIGINT NOT NULL CONSTRAINT connector_selection_removed_org_reelle
                               CHECK (org_id > 0),   -- never `0` (#959), cf. above
    connector  TEXT   NOT NULL,
    removed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (sub, org_id, connector)
);
-- Transition marker (B6): a “seeded” (sub, org) received its initial selection
-- (= the current exposure as active) on the switch to the strict regime “not selected =
-- hidden”. Avoids re-seeding a member who legitimately deselected everything.
CREATE TABLE IF NOT EXISTS connector_selection_seeded (
    sub       TEXT   NOT NULL,
    org_id    BIGINT NOT NULL DEFAULT 0,
    seeded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (sub, org_id)
);
"""


# --- schema (receives the conn of the init_db transaction) ------------------

def init_schema(conn) -> None:
    """Creates the table. Idempotent. Called by `db.init_db` in the same transaction."""
    conn.execute(_SCHEMA)


# --- reads (self-managing) --------------------------------------------------

def list_selection(sub: str, org_id: int = 0) -> dict[str, str]:
    """The member's selections in an org: `{connector: state}`. Connectors
    absent from the map are *not selected*."""
    from .. import db

    with db._connect() as conn:
        rows = conn.execute(
            "SELECT connector, state FROM user_selected_connectors WHERE sub = %s AND org_id = %s",
            (sub, org_id),
        ).fetchall()
    _signaler_orphelins(rows)
    return {r["connector"]: r["state"] for r in rows}


def list_selection_detail(sub: str, org_id: int = 0) -> dict[str, dict]:
    """Same read as `list_selection`, with the PROVENANCE of each row:
    `{connector: {"state": …, "origin": …}}` (ADR 0050 §E7)."""
    from .. import db

    with db._connect() as conn:
        rows = conn.execute(
            "SELECT connector, state, origin FROM user_selected_connectors "
            "WHERE sub = %s AND org_id = %s",
            (sub, org_id),
        ).fetchall()
    _signaler_orphelins(rows)
    return {r["connector"]: {"state": r["state"], "origin": r["origin"]} for r in rows}


def _signaler_orphelins(rows) -> None:
    """A selection naming a connector absent from the served catalog mounts no tool:
    logged as an error, never dropped in silence (#905). The rows are returned as read."""
    from . import orphelins

    orphelins.signaler("user_selected_connectors", (r["connector"] for r in rows))


def list_removed(sub: str, org_id: int = 0) -> dict:
    """The member's removals in an org: `{connector: removed_at}` (ADR 0050 §E6).
    A connector in this map is no longer installed AND the member removed it themselves:
    no org gesture puts it back for them."""
    from .. import db

    with db._connect() as conn:
        rows = conn.execute(
            "SELECT connector, removed_at FROM connector_selection_removed "
            "WHERE sub = %s AND org_id = %s",
            (sub, org_id),
        ).fetchall()
    return {r["connector"]: r["removed_at"] for r in rows}


def state_of(sub: str, connector: str, org_id: int = 0) -> str | None:
    """State of a connector for the member: 'active' | 'paused' | None (not selected)."""
    from .. import db

    with db._connect() as conn:
        row = conn.execute(
            "SELECT state FROM user_selected_connectors "
            "WHERE sub = %s AND org_id = %s AND connector = %s",
            (sub, org_id, connector),
        ).fetchone()
    return row["state"] if row is not None else None


# --- MEMBER writes (self-managing) --------------------------------------------

def set_state(sub: str, connector: str, state: str, org_id: int = 0) -> None:
    """MEMBER gesture: installs (or toggles active↔pause) a connector. Upsert.

    Provenance (ADR 0050 §E7): an installation by the member, or their resuming after
    a pause, sets the row to `membre` — this is what shields it from a kit removal
    (decision Q1: “a member who had installed it themselves keeps it”). A
    PAUSE does not change the provenance of an existing row: pausing what the
    kit placed does not appropriate it. The gesture erases a prior removal by the member
    — their last gesture is no longer a removal. ⚠️ Reserved for member gestures: an
    org gesture writes through `install_for_member`, never through here (the provenance
    would lie)."""
    if state not in STATES:
        raise ValueError(f"invalid selection state: {state!r} (∈ {STATES})")
    from .. import db

    on_conflict = ("DO UPDATE SET state = EXCLUDED.state, selected_at = NOW(), "
                   "origin = EXCLUDED.origin" if state == ACTIVE
                   else "DO UPDATE SET state = EXCLUDED.state, selected_at = NOW()")
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO user_selected_connectors (sub, org_id, connector, state, origin) "
            "VALUES (%s, %s, %s, %s, %s) "
            f"ON CONFLICT (sub, org_id, connector) {on_conflict}",
            (sub, org_id, connector, state, MEMBRE),
        )
        conn.execute(
            "DELETE FROM connector_selection_removed "
            "WHERE sub = %s AND org_id = %s AND connector = %s",
            (sub, org_id, connector),
        )


def unselect(sub: str, connector: str, org_id: int = 0) -> bool:
    """MEMBER gesture: removes a connector from their selection (→ back to the library).
    Returns True if a row existed.

    The removal is KEPT with its date (ADR 0050 §E6): without it, an addition to the kit
    reinstalled what the member had just removed — the erased row left no trace of
    their “no”. A removal that finds nothing writes nothing: there was nothing to
    remove, and the caller refuses it."""
    from .. import db

    with db._connect() as conn:
        cur = conn.execute(
            "DELETE FROM user_selected_connectors WHERE sub = %s AND org_id = %s AND connector = %s",
            (sub, org_id, connector),
        )
        if not (cur.rowcount or 0):
            return False
        conn.execute(
            "INSERT INTO connector_selection_removed (sub, org_id, connector) "
            "VALUES (%s, %s, %s) ON CONFLICT (sub, org_id, connector) "
            "DO UPDATE SET removed_at = NOW()",
            (sub, org_id, connector),
        )
        return True


# --- write for an ORG gesture (receives the caller's conn) --------------------

def install_for_member(conn, sub: str, connector: str, org_id: int, origin: str) -> str:
    """Installs `connector` for ONE member on behalf of an org gesture (kit,
    push) — never over the member (ADR 0050 §E4/§E6). Receives the caller's `conn`:
    an org gesture is ONE transaction. Returns what happened:

    - `installed`       — no row, no removal: row placed, `active`, `origin`;
    - `already_active`  — an active row exists (whatever its provenance):
                          untouched;
    - `paused`          — the member has it paused: untouched (a pause holds);
    - `removed_by_member` — the member removed it themselves: nothing is placed.

    The read and the write are a single statement guarded by the PK and by the
    removal, not a `SELECT` then an `INSERT`: a member who removes the connector
    during the gesture doesn't see it come back."""
    if origin not in (SOCLE, KIT, ADMIN):
        raise ValueError(f"invalid org-gesture provenance: {origin!r}")
    cur = conn.execute(
        "INSERT INTO user_selected_connectors (sub, org_id, connector, state, origin) "
        "SELECT %s, %s, %s, 'active', %s "
        " WHERE NOT EXISTS (SELECT 1 FROM connector_selection_removed "
        "                    WHERE sub = %s AND org_id = %s AND connector = %s) "
        "ON CONFLICT (sub, org_id, connector) DO NOTHING",
        (sub, org_id, connector, origin, sub, org_id, connector),
    )
    if cur.rowcount:
        return "installed"
    row = conn.execute(
        "SELECT state FROM user_selected_connectors "
        "WHERE sub = %s AND org_id = %s AND connector = %s",
        (sub, org_id, connector),
    ).fetchone()
    if row is None:
        return "removed_by_member"
    return "already_active" if row["state"] == ACTIVE else "paused"


# --- initial seed of a (sub, org) — curated base (ADR 0050) -------------------

def is_seeded(sub: str, org_id: int = 0) -> bool:
    """True if this (sub, org) has already received its initial selection (cf. `seed_active`)."""
    from .. import db

    with db._connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM connector_selection_seeded WHERE sub = %s AND org_id = %s",
            (sub, org_id),
        ).fetchone()
    return row is not None


def seed_active(sub: str, origins: dict[str, str], org_id: int = 0) -> None:
    """Initial selection of a (sub, org) (one-shot, sets the `seeded` marker): installs
    each connector of `origins` as `active`, under its provenance (`socle` or
    `kit`, ADR 0050 §E7). The caller decides the content (`session_visibility`).
    The rest of the exposure starts not selected (→ library). Idempotent, never
    rewrites an existing selection — and never puts back what the member removed
    (a kit gesture may have placed the row BEFORE their first pass, and they may have
    removed it since from the screen, which doesn't seed)."""
    if not isinstance(origins, dict):
        raise TypeError("seed_active: `origins` = {connector: provenance}")
    from .. import db

    with db._connect() as conn:
        for name, origin in origins.items():
            install_for_member(conn, sub, name, org_id, origin)
        conn.execute(
            "INSERT INTO connector_selection_seeded (sub, org_id) VALUES (%s, %s) "
            "ON CONFLICT (sub, org_id) DO NOTHING",
            (sub, org_id),
        )


# --- renaming of a retired connector (carrying selections over) ---------------

def rename_selection(conn, old: str, new: str) -> int:
    """Carries over to `new` the selections left on a RETIRED connector `old`
    (receives the `conn` of the `db.init_db` transaction). Returns the number of rows
    renamed (excluding duplicates absorbed). Idempotent: on replay, no row
    carries `old` any more — everything becomes a no-op.

    **Why a boot migration and not a manual one-shot**: retiring a connector
    (#279: `linkedin` → `aiark`) renames the registry, but a member's toolbox is the list
    of their INSTALLED connectors (ADR 0019/0050) — a name that no longer resolves to
    anything mounts no tool, and under the strict regime “not selected = hidden” the
    member loses the entire surface, with nothing telling them. The gesture was noted in
    prose (“to be done at the prod tag”): a note blocks nothing and reminds of nothing,
    seven tags went by (#295). A data migration that must follow a tag is a BOOT migration.

    The ORDER of the three gestures is the fix, not a detail: the PK is
    `(sub, org_id, connector)`, so a raw `UPDATE … SET connector = new` fails
    on any pair that ALREADY carried both (they coexisted — one in platform mode, the
    other in BYO). Hence: promote, deduplicate, rename."""
    if old == new:
        raise ValueError("rename_selection: old and new are identical")
    # 1. Pairs already carrying `new`: the most PERMISSIVE wins — if `old` was active,
    #    the surviving row must be too (the member did have the tool).
    conn.execute(
        "UPDATE user_selected_connectors a SET state = %s "
        " WHERE a.connector = %s AND a.state <> %s "
        "   AND EXISTS (SELECT 1 FROM user_selected_connectors b "
        "                WHERE b.sub = a.sub AND b.org_id = a.org_id "
        "                  AND b.connector = %s AND b.state = %s)",
        (ACTIVE, new, ACTIVE, old, ACTIVE),
    )
    # 2. … and the old row is then redundant there (otherwise the rename violates the PK).
    conn.execute(
        "DELETE FROM user_selected_connectors a "
        " WHERE a.connector = %s "
        "   AND EXISTS (SELECT 1 FROM user_selected_connectors b "
        "                WHERE b.sub = a.sub AND b.org_id = a.org_id "
        "                  AND b.connector = %s)",
        (old, new),
    )
    # 3. The rest is renamed without conflict — and keeps its `state` (a paused
    #    selection stays paused: the rename is not an opportunity to install).
    cur = conn.execute(
        "UPDATE user_selected_connectors SET connector = %s WHERE connector = %s",
        (new, old),
    )
    # 4. The member's REMOVALS follow too (ADR 0050 §E6): a removal left on the
    #    old name would protect nothing any more, and the kit would reinstall under the
    #    new name what the member had removed. Same order: a removal already placed
    #    under `new` wins (it is the more recent of the two facts), the old one goes.
    conn.execute(
        "DELETE FROM connector_selection_removed a WHERE a.connector = %s "
        "   AND EXISTS (SELECT 1 FROM connector_selection_removed b "
        "                WHERE b.sub = a.sub AND b.org_id = a.org_id AND b.connector = %s)",
        (old, new),
    )
    conn.execute(
        "UPDATE connector_selection_removed SET connector = %s WHERE connector = %s",
        (new, old),
    )
    return cur.rowcount or 0


# --- one-shot of the unipile SPLIT (2026-08-28) ------------------------------

# Sentinel of the split fan-out, same ledger and same shape as `_BACKFILL_MARK`
# (never a real sub — Logto subs are alphanumeric).
_SPLIT_MARK = "#unipile-split-fanout"
# The google split (2026-09-26): the account + its six services. Same ledger, its own
# sentinel — two moves, two markers.
GOOGLE_SPLIT_MARK = "#google-split-fanout"
GOOGLE_SERVICES = ("gmail", "drive", "sheets", "calendar", "tasks", "chat")


def split_fanout_pending(conn, targets: tuple[str, ...],
                         mark: str = _SPLIT_MARK) -> bool:
    """Must the split fan-out still run on THIS database? (one-shot)

    **Why this safeguard exists.** The fan-out was written “idempotent, therefore
    replayable at every boot” on the strength of an `ON CONFLICT DO NOTHING` — which only
    protects rows that are PRESENT. Yet deselecting a connector DELETES its row
    (`unselect`, a DELETE): the protection therefore didn't cover the one case where it
    mattered. Lived result: whoever removed WhatsApp found it installed again at the next
    restart, along with the five other channels — “just because one connector is active
    doesn't mean the others should be”. Same mechanics on the two other rungs: a
    platform availability switched off by hand came back on, an org ACL that had been
    erased came back set.

    A split fan-out is not a convergence to maintain — it is a MOVE, true once. What must
    be replayable is the BOOT, not the write: hence a sentinel, exactly like the ADR 0050
    backfill.

    **The prod database has already received it** (it has booted with this code since
    2026-08-28), and setting the sentinel without looking at anything else would make it
    run one last time — reinstalling one last time what people removed. Hence the probe:
    a database that ALREADY carries a selection on one of the channels has received the
    move, we mark without rewriting. A new database, or one restored from before the
    split, carries none and receives it normally."""
    # `mark`: ONE sentinel per split (unipile 2026-08-28, google 2026-09-26) —
    # the second must neither read nor set the first.
    done = conn.execute(
        "SELECT 1 FROM connector_selection_seeded WHERE sub = %s AND org_id = 0",
        (mark,),
    ).fetchone()
    if done:
        return False
    deja = conn.execute(
        "SELECT 1 FROM user_selected_connectors WHERE connector = ANY(%s) LIMIT 1",
        (list(targets),),
    ).fetchone()
    if deja:
        mark_split_fanout(conn, mark)
        return False
    return True


def mark_split_fanout(conn, mark: str = _SPLIT_MARK) -> None:
    """Sets the split fan-out sentinel — to be called AFTER the pass."""
    conn.execute(
        "INSERT INTO connector_selection_seeded (sub, org_id) VALUES (%s, 0) "
        "ON CONFLICT DO NOTHING",
        (mark,),
    )


def fanout_selection(conn, source: str, targets: tuple[str, ...]) -> int:
    """Extends `source`'s selection to `targets` — a connector that SPLITS.

    Counterpart of `rename_selection` for the 1→N case. The unipile split of 2026-08-28
    is its first carrier: the “hosted messaging” card became seven cards
    (the account + its six channels), and a member who had installed `unipile` must
    find their WhatsApp and LinkedIn tools where they are NOW. Without this
    gesture, under the strict regime “not selected = hidden” (ADR 0050), the messaging
    surface disappears from the toolbox of everyone who had it — silently,
    exactly the failure mode of #295.

    `source` is KEPT: it doesn't disappear from the registry (it becomes the provider
    account, which carries the key). This is what distinguishes a split from a rename.

    `ON CONFLICT DO NOTHING` on the PK `(sub, org_id, connector)`: a pair that
    already carries one of the `targets` keeps ITS state — a member who had already
    paused a channel doesn't see it reinstalled by the migration. Idempotent, therefore
    replayable at every boot (shared preprod/prod database, docs/live-migrations.md).

    The `state` is INHERITED from the source: a paused selection stays paused. A
    split is not an opportunity to install anything."""
    n = 0
    for cible in targets:
        cur = conn.execute(
            "INSERT INTO user_selected_connectors (sub, org_id, connector, state) "
            "SELECT sub, org_id, %s, state FROM user_selected_connectors "
            " WHERE connector = %s "
            "ON CONFLICT DO NOTHING",
            (cible, source),
        )
        n += cur.rowcount or 0
    return n


# --- ADR 0050 migration: one-shot backfill of pre-existing pairs --------------

# `default_hidden` connectors AT THE TIME the flag was removed (ADR 0050 B3) — a
# historical fact frozen in the migration: the backfill reconstitutes what each member
# SAW (their org's exposure minus these hidden ones), not the raw exposure.
_BACKFILL_HIDDEN = frozenset(
    {"attio", "brevoauto", "pennylaneged", "resend", "scaleway", "http", "bridge"})
# Sentinel of the one-shot (never a real sub — Logto subs are alphanumeric).
# Set in `connector_selection_seeded` after the pass: the backfill NEVER replays,
# because a pair created AFTER it must receive the BASE at lazy seed, not the
# historical exposure.
_BACKFILL_MARK = "#adr0050-backfill"


def backfill_preexisting(conn) -> None:
    """ADR 0050 one-shot (receives the `conn` of the `db.init_db` transaction): on the
    switch to the nominal regime “not selected = hidden”, each ALREADY existing and
    never-seeded (sub, org) receives as `active` selection what it SAW (org
    exposure − ex-`default_hidden`) — zero toolbox change for the existing ones.
    Pairs already seeded (strict regime tested as a canary) keep their choices."""
    done = conn.execute(
        "SELECT 1 FROM connector_selection_seeded WHERE sub = %s AND org_id = 0",
        (_BACKFILL_MARK,),
    ).fetchone()
    if done:
        return
    from .activation import _resolve

    # UNIFIED table `connector_availability` (ACL workstream, framing 10/07) — populated
    # BEFORE this backfill by `connector_activation.init_schema` (legacy copy): cf.
    # the call order in db._init. Don't re-read the legacy table (dropped in B2).
    rows = conn.execute(
        "SELECT scope_type, scope_id, connector, enabled FROM connector_availability "
        "WHERE scope_type IN ('platform', 'org')").fetchall()
    global_map: dict[str, bool] = {}
    overrides: dict[int, dict[str, bool]] = {}
    for r in rows:
        if r["scope_type"] == "platform":
            global_map[r["connector"]] = bool(r["enabled"])
        else:
            overrides.setdefault(int(r["scope_id"]), {})[r["connector"]] = bool(r["enabled"])
    # All (sub, org) couples liable to have a visibility profile: the
    # memberships — minus the already-seeded pairs. Plus the `0` sentinel: it seeded,
    # on 10/07, 2,090 rows that no surface read (#959), and the database now refuses it
    # (`…_org_reelle`).
    pairs = conn.execute(
        "SELECT sub, org_id FROM org_members "
        "EXCEPT SELECT sub, org_id FROM connector_selection_seeded").fetchall()
    for p in pairs:
        exposed = _resolve(global_map, overrides.get(p["org_id"], {}))
        for name in sorted(exposed - _BACKFILL_HIDDEN):
            conn.execute(
                "INSERT INTO user_selected_connectors (sub, org_id, connector, state) "
                "VALUES (%s, %s, %s, 'active') "
                "ON CONFLICT (sub, org_id, connector) DO NOTHING",
                (p["sub"], p["org_id"], name),
            )
        conn.execute(
            "INSERT INTO connector_selection_seeded (sub, org_id) VALUES (%s, %s) "
            "ON CONFLICT (sub, org_id) DO NOTHING",
            (p["sub"], p["org_id"]),
        )
    conn.execute(
        "INSERT INTO connector_selection_seeded (sub, org_id) VALUES (%s, 0) "
        "ON CONFLICT (sub, org_id) DO NOTHING",
        (_BACKFILL_MARK,),
    )
