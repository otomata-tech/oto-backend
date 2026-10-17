"""Connector activation tier — DB governance (ADR 0010, decision 4).

**Declaration (`providers/` registry) ≠ activation (this table).** A connector
declared in code is NOT exposed merely by being declared: an activation row is
needed. Resolution, the scope ladder taking precedence from nearest to widest:

    exposed(connector, org)  = org override if set, else platform master, else OFF
    effective(team member)   = exposed(org) − team cuts (restrict-only)

**SINGLE table `connector_availability`** (ACL workstream, scoping 10/07 — merge of
the former pair `connector_activation` + `group_connector_activation`): the grain is a
scope COLUMN, not one table per grain.

- **('platform', '')**      : platform master (global switch).
- **('org', <org_id>)**     : org override — forces ON/OFF over the master.
- **('group', <group_id>)** : team cut — `enabled=FALSE` ONLY
  (MONOTONE invariant ADR 0012: the team cuts, never exposes; the business
  guard lives in the capability).
- **no row**                : OFF at the org level (deny-by-default), inherited at the team level.

**One-time seed** (platform rows): the connectors in the registry AT THE TIME the
tier was introduced are activated; later ones stay OFF until explicit
activation — unless the INSTANCE declares `OTO_CONNECTEURS_ACTIFS_PAR_DEFAUT`
(`seed_default_on`): every boot then opens, at the platform, each registry
connector that has no platform row yet. An explicit OFF is never reopened. **Legacy copy at boot** (guarded by `to_regclass`, newer-wins on `set_at`):
the two historical tables are copied over as long as they exist — they drop
in B2 once this code is promoted (shared canary/prod DB).

Convention: reads/writes are **self-managing** (they open their own
connection, like `db.*` and `org_store.*`). Only `init_schema`/`seed_initial`
receive the `conn` of the `db.init_db` transaction. The module does NO
oto_mcp import at module level (leaf, like `providers`) — `db`/`providers` are
imported lazily to avoid any cycle.
"""
from __future__ import annotations

from typing import Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS connector_availability (
    scope_type TEXT NOT NULL CHECK (scope_type IN ('platform','org','tenant','group')),
    scope_id   TEXT NOT NULL DEFAULT '',   -- '' for platform ; org.id / group.id as text ; slug for tenant
    connector  TEXT NOT NULL,              -- connector name (providers/ registry)
    enabled    BOOLEAN NOT NULL,
    set_by     TEXT,
    set_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (scope_type, scope_id, connector)
);
"""


# --- schema (receives the init_db transaction's conn) -----------------------

def init_schema(conn) -> None:
    """Create the unified table + copy the legacy tables if they still exist.
    Idempotent. Called by `db.init_db` in the same transaction as the rest."""
    conn.execute(_SCHEMA)
    _copy_legacy(conn)


def _copy_legacy(conn) -> None:
    """Copy legacy → unified, at EVERY boot as long as the legacy tables exist
    (canary/prod window: prod still writes the legacy ones until promotion —
    newer-wins on `set_at` catches up its writes at the next boot). Guarded by
    `to_regclass`: after the DROP (B2), no-op — a boot never breaks."""
    if conn.execute("SELECT to_regclass('connector_activation') AS t").fetchone()["t"]:
        conn.execute("""
            INSERT INTO connector_availability (scope_type, scope_id, connector, enabled, set_by, set_at)
            SELECT CASE WHEN org_id IS NULL THEN 'platform' ELSE 'org' END,
                   COALESCE(org_id::text, ''), connector, enabled, set_by, set_at
              FROM connector_activation
            ON CONFLICT (scope_type, scope_id, connector) DO UPDATE
               SET enabled = EXCLUDED.enabled, set_by = EXCLUDED.set_by, set_at = EXCLUDED.set_at
             WHERE EXCLUDED.set_at > connector_availability.set_at
        """)
    if conn.execute("SELECT to_regclass('group_connector_activation') AS t").fetchone()["t"]:
        conn.execute("""
            INSERT INTO connector_availability (scope_type, scope_id, connector, enabled, set_by, set_at)
            SELECT 'group', group_id::text, connector, enabled, set_by, set_at
              FROM group_connector_activation
            ON CONFLICT (scope_type, scope_id, connector) DO UPDATE
               SET enabled = EXCLUDED.enabled, set_by = EXCLUDED.set_by, set_at = EXCLUDED.set_at
             WHERE EXCLUDED.set_at > connector_availability.set_at
        """)


def seed_initial(conn) -> None:
    """One-time seed: if no PLATFORM row exists (neither copied from legacy nor already
    seeded), activate (master ON) all connectors of the current registry — snapshot
    of the state at the tier's introduction. `ON CONFLICT DO NOTHING` covers a
    concurrent boot. ⚠️ The guard looks at platform rows ONLY: emptying the table
    by mistake would re-seed everything to ON (documented at scoping — do not empty it)."""
    n = conn.execute("SELECT COUNT(*) AS n FROM connector_availability "
                     "WHERE scope_type = 'platform'").fetchone()["n"]
    if n:
        return
    from .. import providers  # single-source registry (pure, no oto_mcp import)

    for name in providers.REGISTRY:
        conn.execute(
            "INSERT INTO connector_availability (scope_type, scope_id, connector, enabled, set_by) "
            "VALUES ('platform', '', %s, TRUE, %s) ON CONFLICT DO NOTHING",
            (name, "seed"),
        )


ENV_DEFAUT_ACTIF = "OTO_CONNECTEURS_ACTIFS_PAR_DEFAUT"


def seed_default_on(conn) -> int:
    """Open at the platform every registry connector that has NO platform row yet —
    only on an instance that declares `ENV_DEFAUT_ACTIF` (decision of 17/10/2026).

    The deny-by-default of `seed_initial` is the right rule for oto: a new connector
    is a product decision. An instance that would rather offer each release's new
    connectors as they ship declares it, and stops discovering them as « missing »
    one by one. Only the ABSENCE of a row is filled: an explicit OFF (`enabled =
    FALSE`) stays OFF, an org override or a team cut is untouched, and the tenant
    ceiling still applies above. Platform rows are never deleted (`clear_activation`
    is org-scoped), so replaying it at every boot reopens nothing that was closed.

    Run AFTER the split fan-outs (`db.init_db`): a split's targets must first copy
    their source's state, ON or OFF, before anything fills them with ON. Read at
    boot only — a new connector only arrives with a new release, hence a boot.
    Returns the number of connectors opened."""
    import os

    from .. import interrupteurs, providers

    brut = os.environ.get("OTO_CONNECTEURS_ACTIFS_PAR_DEFAUT")
    if not interrupteurs.oui_non(ENV_DEFAUT_ACTIF, brut):
        return 0
    # ONE statement, not one per connector: it replays at every boot (`db/_ddl_garde`
    # spirit — a boot with nothing to do should cost next to nothing).
    cur = conn.execute(
        "INSERT INTO connector_availability (scope_type, scope_id, connector, enabled, set_by) "
        "SELECT 'platform', '', c, TRUE, 'seed:default_on' FROM unnest(%s::text[]) AS c "
        "ON CONFLICT DO NOTHING",
        (list(providers.REGISTRY),),
    )
    return cur.rowcount or 0


def fanout_availability(conn, source: str, targets: tuple[str, ...]) -> int:
    """Extend `source`'s exposure to `targets` — at ALL THREE scopes.

    The initial seed (`seed_initial`) only runs once, on an empty table: a
    connector added after it has NO platform row, so stays OFF
    (deny-by-default). That is the right rule for a brand-new connector — and the
    worst possible case for a SPLIT connector: on the day of the unipile split
    (2026-08-28), the six channels are born OFF and all hosted messaging goes dark
    for everyone, though nothing was disabled.

    So we copy all three scopes, not just the master:
    · `platform` — the global switch follows the original connector;
    · `org`      — an org override (ON or OFF) is a DECISION of that org:
                   an org that had cut unipile must not see six channels
                   light up, and an org that had forced it ON keeps them;
    · `group`    — a team cut is monotone (it only removes):
                   losing it would RELEASE a restriction, never the reverse.

    `ON CONFLICT DO NOTHING`: a setting already placed on a target wins (boot
    replay, or an admin who has since ruled). Idempotent."""
    n = 0
    for cible in targets:
        cur = conn.execute(
            "INSERT INTO connector_availability "
            "       (scope_type, scope_id, connector, enabled, set_by) "
            "SELECT scope_type, scope_id, %s, enabled, %s "
            "  FROM connector_availability WHERE connector = %s "
            "ON CONFLICT DO NOTHING",
            (cible, f"split:{source}", source),
        )
        n += cur.rowcount or 0
    return n


# --- resolution (pure) ------------------------------------------------------

def _resolve(global_map: dict[str, bool], override_map: dict[str, bool],
             tenant_map: "dict[str, bool] | None" = None) -> set[str]:
    """Apply `org override > platform master > OFF`, under the tenant CEILING.
    Returns the exposed connectors. Pure (no DB) → testable offline.

    The TENANT tier (2026-09-26): a partner serving oto under its brand cuts, for
    ALL its orgs at once, a connector its offer does not include — without
    setting an override on each, and without an org admin being able to reopen it.
    It is a ceiling, like the platform: it only REMOVES (`enabled=false`),
    a `true` row exposes nothing the platform does not already expose. Concrete
    reason: a Google service that the tenant's Google Cloud project does not declare —
    consent would fail at Google, the card must not exist for them."""
    names = set(global_map) | set(override_map)
    exposed = {n for n in names if override_map.get(n, global_map.get(n, False))}
    if not tenant_map:
        return exposed
    return {n for n in exposed if tenant_map.get(n, True)}


def effective_for_group(exposed: set[str], group_cut: set[str]) -> set[str]:
    """EFFECTIVE exposure for a team member = what the org exposes MINUS
    the active team's cuts. MONOTONE invariant (ADR 0012): the team can only
    REMOVE — never make visible a connector the org has cut. Pure
    (no DB) → testable offline."""
    return exposed - group_cut


def exposed_for(org_id: Optional[int], group_id: Optional[int] = None) -> set[str]:
    """What a member of `org_id` may see — the org's exposure (platform and tenant
    ceilings included) minus the cuts of their team `group_id`, if any. The single
    source of the ACTIVATION layer of a session's visibility (`session_visibility`)
    and of the member-type preview (`org.context.preview`, #1194)."""
    exposed = exposed_connectors(org_id)
    if group_id is None:
        return exposed
    return effective_for_group(exposed, group_cut_connectors(group_id))


# --- reads (self-managing) --------------------------------------------------

def tenant_of_org(org_id: Optional[int], conn=None) -> Optional[str]:
    """The slug of the tenant that HOSTS this org, or `None` (primary tenant, or no
    org) — the only case where the tenant tier does not exist. Read through
    `db.org_tenant_slug` (the union of the three axes, `docs/tenants.md`), never
    guessed. `conn`: the connection already opened by the caller — activation
    resolution reads the tenant in ITS OWN, not in one more connection on every
    tool call."""
    if org_id is None:
        return None
    from .. import db, tenancy
    slug = (db.org_tenant_slug(int(org_id)) if conn is None
            else db.org_tenant_slug(int(org_id), conn=conn))
    return None if not slug or slug == tenancy.primary_slug() else slug


def is_exposed(connector: str, org_id: Optional[int] = None) -> bool:
    """exposed = org override if set, else platform master, else OFF."""
    return cran_qui_coupe(connector, org_id) is None


def cran_qui_coupe(connector: str, org_id: Optional[int] = None,
                   group_id: Optional[int] = None) -> Optional[str]:
    """The tier that CUTS `connector` for (org, team), or `None` if it is exposed.

    Same resolution as `effective_for_group(exposed_connectors(org), group_cut…)`,
    for ONE connector: `'org'` = org override at OFF; `'platform'` = no
    org override and master OFF or absent (deny-by-default); `'group'` = exposed
    for the org but cut by team `group_id`. Naming the tier means naming WHO
    can reopen — what the call refusal (`activation_gate`) tells the agent."""
    from .. import db

    with db._connect() as conn:
        slug = tenant_of_org(org_id, conn=conn)
        # The TENANT ceiling first: cut there, nobody in the org reopens —
        # neither an ON org override, nor a team. Naming this tier means saying that
        # the gesture is with the host.
        if slug is not None and conn.execute(
                "SELECT 1 FROM connector_availability "
                "WHERE scope_type = 'tenant' AND scope_id = %s AND connector = %s "
                "AND enabled = FALSE",
                (slug, connector),
        ).fetchone() is not None:
            return "tenant"
        org_row = None
        if org_id is not None:
            org_row = conn.execute(
                "SELECT enabled FROM connector_availability "
                "WHERE scope_type = 'org' AND scope_id = %s AND connector = %s",
                (str(org_id), connector),
            ).fetchone()
        if org_row is not None:
            if not org_row["enabled"]:
                return "org"
        else:
            row = conn.execute(
                "SELECT enabled FROM connector_availability "
                "WHERE scope_type = 'platform' AND connector = %s",
                (connector,),
            ).fetchone()
            if row is None or not row["enabled"]:
                return "platform"
        if group_id is not None and conn.execute(
                "SELECT 1 FROM connector_availability "
                "WHERE scope_type = 'group' AND scope_id = %s AND connector = %s "
                "AND enabled = FALSE",
                (str(group_id), connector),
        ).fetchone() is not None:
            return "group"
    return None


def exposed_connectors(org_id: Optional[int] = None) -> set[str]:
    """Set of exposed connectors (resolves org override vs master in a single
    scan). To filter the catalog / loading in one query."""
    from .. import db

    with db._connect() as conn:
        slug = tenant_of_org(org_id, conn=conn)
        rows = conn.execute(
            "SELECT scope_type, connector, enabled FROM connector_availability "
            "WHERE scope_type = 'platform' OR (scope_type = 'org' AND scope_id = %s)"
            "   OR (scope_type = 'tenant' AND scope_id = %s)",
            (str(org_id) if org_id is not None else "", slug or ""),
        ).fetchall()
    # An activation naming a connector absent from the served catalog exposes nothing
    # that exists: logged as an error, never dropped in silence (#905).
    from . import orphelins
    orphelins.signaler("connector_availability", (r["connector"] for r in rows))
    global_map: dict[str, bool] = {}
    override_map: dict[str, bool] = {}
    tenant_map: dict[str, bool] = {}
    for r in rows:
        target = {"org": override_map, "tenant": tenant_map}.get(r["scope_type"], global_map)
        target[r["connector"]] = bool(r["enabled"])
    return _resolve(global_map, override_map, tenant_map)


def list_activations() -> list[dict]:
    """All platform master rows + org overrides, for the admin surface.
    HISTORICAL projection kept: `org_id` (None = master) — the callers
    (REST admin) did not change at unification."""
    from .. import db

    with db._connect() as conn:
        rows = conn.execute(
            "SELECT scope_type, scope_id, connector, enabled, set_by, set_at "
            "FROM connector_availability WHERE scope_type IN ('platform', 'org') "
            "ORDER BY connector, (scope_type <> 'platform'), scope_id"
        ).fetchall()
    return [{"connector": r["connector"],
             "org_id": None if r["scope_type"] == "platform" else int(r["scope_id"]),
             "enabled": r["enabled"], "set_by": r["set_by"], "set_at": r["set_at"]}
            for r in rows]


# --- writes (admin surface, B4) ---------------------------------------------

def set_activation(connector: str, enabled: bool, org_id: Optional[int] = None,
                   set_by: Optional[str] = None) -> None:
    """Set/update the activation: platform master if `org_id` is None, else org override."""
    from .. import db

    scope_type, scope_id = ("platform", "") if org_id is None else ("org", str(org_id))
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO connector_availability (scope_type, scope_id, connector, enabled, set_by) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (scope_type, scope_id, connector) "
            "DO UPDATE SET enabled = EXCLUDED.enabled, set_by = EXCLUDED.set_by, set_at = NOW()",
            (scope_type, scope_id, connector, enabled, set_by),
        )


def clear_activation(connector: str, org_id: int) -> None:
    """Delete an org override → the connector falls back to the platform master."""
    from .. import db

    with db._connect() as conn:
        conn.execute(
            "DELETE FROM connector_availability "
            "WHERE scope_type = 'org' AND scope_id = %s AND connector = %s",
            (str(org_id), connector),
        )


# --- TENANT tier (ceiling, 2026-09-26) ---------------------------------------

def list_tenant_activations(slug: str) -> dict[str, bool]:
    """The cuts (and rows) set by this tenant: `{connector: enabled}`."""
    from .. import db

    with db._connect() as conn:
        rows = conn.execute(
            "SELECT connector, enabled FROM connector_availability "
            "WHERE scope_type = 'tenant' AND scope_id = %s ORDER BY connector",
            (slug,),
        ).fetchall()
    return {r["connector"]: bool(r["enabled"]) for r in rows}


def set_tenant_activation(slug: str, connector: str, enabled: bool,
                          set_by: Optional[str] = None) -> None:
    """Set/update the tenant row. `enabled=false` cuts for all the tenant's orgs;
    `true` only removes the cut (the platform ceiling remains its own)."""
    from .. import db

    with db._connect() as conn:
        conn.execute(
            "INSERT INTO connector_availability (scope_type, scope_id, connector, enabled, set_by) "
            "VALUES ('tenant', %s, %s, %s, %s) "
            "ON CONFLICT (scope_type, scope_id, connector) "
            "DO UPDATE SET enabled = EXCLUDED.enabled, set_by = EXCLUDED.set_by, set_at = NOW()",
            (slug, connector, enabled, set_by),
        )


def clear_tenant_activation(slug: str, connector: str) -> None:
    """Remove the tenant row → the connector follows the platform again."""
    from .. import db

    with db._connect() as conn:
        conn.execute(
            "DELETE FROM connector_availability "
            "WHERE scope_type = 'tenant' AND scope_id = %s AND connector = %s",
            (slug, connector),
        )


# --- TEAM tier (restrict-only, ADR 0012) ------------------------------------

def group_cut_connectors(group_id: int) -> set[str]:
    """Connectors CUT for the team (`enabled=FALSE` rows). A member's effective
    exposure = `exposed_connectors(org) - group_cut_connectors(active
    team)` — monotone invariant: the team can only remove."""
    from .. import db

    with db._connect() as conn:
        rows = conn.execute(
            "SELECT connector FROM connector_availability "
            "WHERE scope_type = 'group' AND scope_id = %s AND enabled = FALSE",
            (str(group_id),),
        ).fetchall()
    return {r["connector"] for r in rows}


def list_group_activations(group_id: int) -> list[dict]:
    """The team's cut rows (team admin surface)."""
    from .. import db

    with db._connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT connector, enabled, set_by, set_at FROM connector_availability "
            "WHERE scope_type = 'group' AND scope_id = %s ORDER BY connector",
            (str(group_id),),
        ).fetchall()]


def set_group_activation(group_id: int, connector: str, enabled: bool,
                         set_by: Optional[str] = None) -> None:
    """Set a team cut. `enabled` MUST be False (restrict-only) — the business
    guard (monotone invariant) is in the capability; here we just store."""
    from .. import db

    with db._connect() as conn:
        conn.execute(
            "INSERT INTO connector_availability (scope_type, scope_id, connector, enabled, set_by) "
            "VALUES ('group', %s, %s, %s, %s) "
            "ON CONFLICT (scope_type, scope_id, connector) "
            "DO UPDATE SET enabled = EXCLUDED.enabled, set_by = EXCLUDED.set_by, set_at = NOW()",
            (str(group_id), connector, enabled, set_by),
        )


def clear_group_activation(group_id: int, connector: str) -> None:
    """Remove the team cut → the connector falls back to the org's exposure."""
    from .. import db

    with db._connect() as conn:
        conn.execute(
            "DELETE FROM connector_availability "
            "WHERE scope_type = 'group' AND scope_id = %s AND connector = %s",
            (str(group_id), connector),
        )
