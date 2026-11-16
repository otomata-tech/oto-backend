"""The per-connector SNAPSHOT — what `/api/me` returns to the dashboard.

`status_for` is a PROJECTION, not a resolution: it walks the walker as a
presence probe (nothing is decrypted) and returns, for each connector, the
winning level, the levels configured beyond it, the day's quota, the missing
step declared by the connector's module and its health state.

It is structurally the mirror of `resolve_api_key` — the walker is what
guarantees it. Its two preloads (presence probe + quota map)
are built on the snapshot's SUBJECT, never on the requester: that is what
keeps a third party's admin sheet correct.

Top of the package: depends on `scope`, `cascade`, `quotas` and `rbac`, and nothing
depends on it.
"""
from __future__ import annotations

import functools
import logging

from .. import providers, credentials_store, db, group_store, status_hints
from ..connectors import link as connector_link
from . import cascade, chain_shadow, quotas, rbac, scope

logger = logging.getLogger(__name__)


def status_for(sub: str, *, org: "int | None | object" = scope._UNSET,
               group: "int | None | object" = scope._UNSET) -> dict:
    """Snapshot for `/api/me` — thin wrapper: opens ONE connection for the whole
    call (oto-backend, `status_for` N+1 batch, 17/09/2026 — cf. `db.reuse_connection`)
    and delegates to `_status_for_projection`, which carries the REAL docstring."""
    with db.reuse_connection():
        return _status_for_projection(sub, org=org, group=group)


def _status_for_projection(sub: str, *, org: "int | None | object" = scope._UNSET,
               group: "int | None | object" = scope._UNSET) -> dict:
    """Snapshot for `/api/me` — role + status per provider:

    - `mode`: `user` (personal key) | `group` | `org` | `tenant` (shared key of the
              caller's tenant, L-keys PR 1) | `platform` (grant + quota OK)
              | `over_quota` (grant but quota exhausted)
              | `forbidden` (neither user key nor grant)

    Explicit `org`/`group` (≠ _UNSET) = snapshot of a THIRD PARTY against THEIR own
    context (admin sheet), without the requester's current_org/current_group (anti-leak).
    """
    role = scope.get_user_role(sub)
    # Effective org resolved once (perf: otherwise 1 lookup/provider). None for
    # any user without an org → the org_secret branch below is inert. Via the
    # `current_org` seam → reflects the session override (MCP) or the view (REST
    # view-as) when applicable, otherwise the home (ADR 0023).
    active_org = scope.current_org(sub) if org is scope._UNSET else org
    active_group = scope.current_group(sub) if group is scope._UNSET else group
    out: dict = {"role": role, "active_org": active_org,
                 "active_group": active_group, "providers": {}}
    # The sub's teams in the org (one query, shared by all the `team_key_group`
    # hints below). Best-effort.
    #
    # ⚠️ **Two preloads, measured before being written (21/08, 67 connectors,
    # 1,707 ms warm).** What weighed was NOT what the previous batch had
    # fixed on `/shell`: `current_org` is already resolved only once here (9 ms), and
    # applying the same fix would have gained nothing. What weighed:
    #   • the `member` (589 ms) and `org` (488 ms) probes — 64%, one walk per
    #     connector ⟹ **preloaded probe** (the inventory read once, the cascade answers
    #     from memory; the walker stays untouched, it is just one more probe);
    #   • **the quota, 410 ms and 24%** — one query per connector on a table of which
    #     a single one returns a person's whole day. Nobody had seen it.
    #
    # ⚠️ Both are built on `active_org` — hence on the snapshot's SUBJECT,
    # never on the requester. That is what keeps a third party's admin sheet
    # correct: explicit `org`/`group` (≠ `_UNSET`) short-circuit `current_org`, and
    # the preloads FOLLOW that value. A preload built on the caller's context
    # would reopen the leak that the actor-scoped seam closed.
    try:
        member_groups = (group_store.list_groups_for_user(sub, active_org)
                         if active_org is not None else [])
    # noqa: SILENT — per-tier fail-open: a team hiccup does not deprive the org of its sheet
    except Exception:
        member_groups = []
    # THIRD preload (28/08): the team-secrets map. It was ALREADY built inside the probe
    # for the `group` rung — but the `team_key_group` hint, fifteen lines below, asked the
    # connector database for it again, connector by connector. And since it only fires on `forbidden`,
    # i.e. the majority of a real account, it cost 67 round trips by itself (a single
    # team; as many more per team) — more than everything the two previous
    # preloads had removed from the org rung. So we build it here,
    # once, and pass it to BOTH.
    #
    # The probe builds ITS OWN on its side (`group_secret_map` is the shared
    # function, not the map): passing it this one would require one more parameter
    # on a function that three test files stub with a lambda. One read per
    # team paid twice — one to three in all — against sixty-seven removed.
    #
    # It follows the same rule as its elders: built on `active_org`/`member_groups`,
    # hence on the snapshot's SUBJECT, never on the requester.
    #
    # ⚠️ **Two separate `try`s, not a single enclosing one (oto#522).** A single block
    # threw away the team map even when ITS construction had succeeded, as soon as
    # the OTHER preload (the probe) failed afterwards — 3 reads paid for
    # nothing, then ~200 unit reads on the `team_key_group` hint (one per
    # `forbidden` connector, cf. `reachable_team_key`) whereas the map
    # already avoided them. Separating the two failures brings this degraded path from ~380 to
    # ~180 queries (measured in the review of #518, 81 connectors, 3 teams) without
    # touching the double failure: both fallbacks remain what they were.
    try:
        secrets_par_equipe = cascade.group_secret_map(member_groups)
    except Exception:      # an acceleration, never a prerequisite
        logger.warning("status_for: team-map preload unavailable",
                       exc_info=True)
        # None (and not {}): an empty map WOULD SILENCE the hint on teams that
        # hold the key. The fallback must re-read, not answer "none".
        secrets_par_equipe = None
    try:
        sonde = cascade.preloaded_presence_probe(sub, org=active_org, groups=member_groups)
    except Exception:      # an acceleration, never a prerequisite
        logger.warning("status_for: presence-probe preload unavailable",
                       exc_info=True)
        sonde = cascade.PRESENCE_PROBE
    try:
        quotas_du_jour = db.usage_today_map(sub)
    except Exception:
        logger.warning("status_for: quota preload unavailable", exc_info=True)
        quotas_du_jour = None

    def _used(provider: str) -> int:
        """The day's counter — from the preloaded map, or the unit read.

        The fallback is not decorative: if the preload failed, returning 0 everywhere
        would display "quota intact" to someone who has exhausted it. Better to pay
        the 48 queries than lie about a quota."""
        # The counter is the KEY's (delegation): a unipile channel reads
        # that of its carrier account, otherwise it would display "quota intact"
        # next to the cap of an already exhausted key.
        porteur = providers.credential_provider(provider)
        if quotas_du_jour is not None:
            return quotas_du_jour.get(porteur, 0)
        return db.get_usage_today(sub, porteur)

    # Connectors that DELEGATE their credential (`Connector.credential_of`, unipile
    # split): their walk would give, by construction, EXACTLY that of their
    # carrier — `walk_cascade` normalizes before the first rung. Doing it six more
    # times adds no information, it adds six walks on THE hot path
    # (`/api/me`, on every dashboard load) — the very one for which this
    # block preloads the vault inventory and the quota map. So we set them
    # aside here and COPY the carrier's entry after the loop.
    delegants = {p: providers.credential_provider(p) for p in db.KEY_PROVIDERS
                 if providers.credential_provider(p) != p}
    # The `platform_unmetered` lift depends only on (subject, org): read at most ONCE
    # per snapshot, and only if a cap is to be lifted (`quotas.plafond_du_jour`).
    leves = functools.cache(lambda: quotas.quotas_leves(sub, active_org))
    for provider in db.KEY_PROVIDERS:
        if provider in delegants:
            continue
        # COMPLETE walker walk as a PRESENCE probe (no decryption on the
        # /api/me path): the winner gives the mode, the following rungs remain
        # displayable (per-level flags). STRUCTURAL mirror of resolve_api_key —
        # any divergence would make /api/me lie about the real mode.
        hits = list(chain_shadow.resolution_rungs(sub, provider, org=active_org, group=active_group,
                                 probe=sonde, want="auto"))
        user_has = any(r.mode == "user" and r.via == "local" for r in hits)
        group_has = any(r.mode == "group" for r in hits)
        org_has = any(r.mode == "org" for r in hits)
        grant = next((r.payload for r in hits if r.mode == "platform"), None)
        # The refusal's count (`quotas.platform_used_today`) when the platform rung
        # ANSWERS: a key counted per org reads the org's counter, a `user` edge its
        # edge counter, anything else the preloaded per-person map. Not shown
        # otherwise, so not read.
        used = (quotas.platform_used_today(sub, provider, usage=lambda: _used(provider),
                                           **quotas.compteurs(grant))
                if grant and hits[0].mode == "platform" else _used(provider))
        # The refusal cap (`quotas.plafond_du_jour`), entitlement lift included: a
        # cap copied here announced `over_quota` to a person nothing limits.
        limit = quotas.plafond_du_jour(grant, provider, leves) if grant else 0

        winner = hits[0] if hits else None
        # ⚠️ THE distinction of this projection, and the sole cause of the defect it
        # carried: `hits` carries the WHOLE cascade, `winner` only the rung that
        # ANSWERS. Both are legitimate and do not say the same thing —
        #   • a flag "is there a key at this level" (`*_configured`,
        #     `platform_key_label`) is read on `hits`: "what you would
        #     fall back on" is a correct piece of information;
        #   • everything that describes the CURRENT EFFECT (cap, counter, exhaustion) is
        #     read on `winner`, otherwise we announce a constraint nothing enforces.
        # `status_for` is the ONLY function in the module to keep all the rungs
        # (the others take the winner and leave: `credential_mode_for`,
        # `platform_quota_hint`, `_win_quota`) — hence the only place where this
        # confusion can arise. Hence this NAMED boolean rather than a condition
        # rewritten for each field: an effect field added later reads it, and
        # `test_quota_affiche_le_barreau_qui_repond` fails if it does not.
        plateforme_repond = winner is not None and winner.mode == "platform"
        if winner is None:
            mode = "forbidden"
        elif plateforme_repond and limit and used >= limit:
            mode = "over_quota"
        else:
            mode = winner.mode

        out["providers"][provider] = {
            "mode": mode,
            "user_key_configured": user_has,
            "group_secret_configured": group_has,
            "org_secret_configured": org_has,
            # LEVEL flag (read on `hits`): "what you would fall back on".
            # Served even outside the platform rung, deliberately — the front displays it
            # since v1.12.0, and it is true.
            "platform_key_label": grant["label"] if grant else None,
            # EFFECT fields (read on `winner`): the platform key's counter
            # and its cap. Outside the platform rung, neither makes
            # sense — `resolve_api_key` returns BEFORE `_win_quota` as soon as
            # `win.mode != "platform"`, and the settlement only debits a call the
            # resolver noted as served by a platform key (`access.reglement`). A cap announced
            # there would thus be a PHANTOM in both directions: neither counted (hence the perpetual
            # "0" in the numerator), nor enforceable. Lived on an org served by
            # a TENANT key: "0/200 today" on a connector without
            # any cap.
            # ⚠️ A tenant key's cap lives on the tenant→org edge
            # (`tenant_budget`, 0 or absent = unlimited) and has NO field here:
            # an org that has one sees nothing of it. Known hole, separate batch.
            "quota_used_today": used if plateforme_repond else None,
            # limit 0 = unlimited (default_quota convention) → None so the UI
            # displays "∞", not "/0" (which reads as an exhausted quota).
            "quota_daily": (limit or None) if (grant and plateforme_repond) else None,
            # "Reachable" team key (member of a team that has the secret, without
            # having it active): nothing resolves but a key exists → the UI must
            # say so instead of a dry "no key".
            "team_key_group": (rbac.reachable_team_key(sub, active_org, provider,
                                                  groups=member_groups,
                                                  secrets_by_group=secrets_par_equipe)
                               if mode == "forbidden" else None),
        }

    # Copy of the delegators (cf. above): same key ⟹ same verdict, word for word.
    # ⚠️ Copy, not shared reference: two cards pointing at the same dict
    # would answer each other on a caller's first `.update()`.
    # Carrier absent (not keyed, or inconsistent registry) ⟹ we invent nothing: the
    # card has no entry, like any connector without a credential.
    for delegant, porteur in delegants.items():
        entree = out["providers"].get(porteur)
        if entree is not None:
            out["providers"][delegant] = dict(entree)

    # byo_user credentials with declared fields, outside KEY_PROVIDERS (generic
    # multi-field model, ADR 0011): in-process clients with a `basic_auth` credential
    # (planity) or multi-secrets (silae, zoho).
    # No quota or grant — the credential IS the
    # grant (cf. resolve_credential_fields). Mirror of the
    # byo cascade user > active group > org (an org-shareable `fields` provider
    # resolves through the team/org secret — the former user-only check displayed
    # `forbidden` with an org key that resolved, the UI lied; fixed
    # 2026-07-16). Lets the dashboard display "configured / remove".
    for c in providers.REGISTRY.values():
        if (c.name in out["providers"] or not c.secret_fields
                or "byo_user" not in c.auth_modes):
            continue
        # Same walker, `want='byo'` (the credential IS the grant — no platform
        # tier or quota, cf. resolve_credential_fields).
        hits = list(chain_shadow.resolution_rungs(sub, c.name, org=active_org, group=active_group,
                                 probe=sonde, want="byo"))
        mode = hits[0].mode if hits else "forbidden"
        out["providers"][c.name] = {
            "mode": mode,
            "user_key_configured": any(r.mode == "user" and r.via == "local"
                                       for r in hits),
            "group_secret_configured": any(r.mode == "group" for r in hits),
            "org_secret_configured": any(r.mode == "org" for r in hits),
            "platform_key_label": None,
            "quota_used_today": 0,
            "quota_daily": None,
            "team_key_group": (rbac.reachable_team_key(sub, active_org, c.name,
                                                  groups=member_groups,
                                                  secrets_by_group=secrets_par_equipe)
                               if mode == "forbidden" else None),
        }

    # Connectors with a browser SESSION (`personal_session`, secret_kind="cookie":
    # brevo/crunchbase): no field to enter → connection via Browserbase Live View
    # (MCP `<ns>_connect_start`), the credential = the Context persisted in the vault. We
    # just expose "configured + since when" so the card renders its session
    # widget (ADR 0026 provided for `providers` without ever feeding it → /api/me said
    # nothing about these sessions anymore; fixed 2026-06-30).
    for c in providers.REGISTRY.values():
        if c.name in out["providers"] or c.secret_kind != "cookie":
            continue
        shareable = c.name in cascade.ORG_SHAREABLE_PROVIDERS
        # One read of each level's inventory for ALL the session connectors — the
        # same rows the presence probe already read (#1148), never one query per
        # connector and per level.
        st = (credentials_store.statuts_mono_compte(
                  credentials_store.MEMBER,
                  credentials_store.member_id(active_org, sub)).get(c.name)
              if active_org is not None else None)
        # Shared sessions (org-shareable connector): active team then org.
        # Mirror of the resolution cascade (member > group > org).
        grp_st = (credentials_store.statuts_mono_compte("group", str(active_group)).get(c.name)
                  if shareable and active_group is not None else None)
        org_st = (credentials_store.statuts_mono_compte("org", str(active_org)).get(c.name)
                  if shareable and active_org is not None else None)
        meta = (st or {}).get("meta") or {}
        # `mode` = winning level of the cascade (member > group > org), so the
        # card says under which session we resolve — like the keyed connectors.
        if st:
            mode = "user"
        elif grp_st:
            mode = "group"
        elif org_st:
            mode = "org"
        else:
            mode = "forbidden"
        if chain_shadow.chain_decides():
            winner = next(chain_shadow.resolution_rungs(
                sub, c.name, org=active_org, group=active_group, probe=sonde, want="byo"), None)
            mode = winner.mode if winner else "forbidden"
        out["providers"][c.name] = {
            "mode": mode,
            "user_key_configured": st is not None,
            "session_set_at": st["set_at"] if st else None,
            # Default identity/target of the ADR 0024 selector (pennylaneged: the
            # client company = ITS GED) — PUBLIC satellites of the meta, the card
            # displays them without listing (listing = a rented Browserbase session).
            "identity_id": meta.get("default_identity_id"),
            "identity_label": meta.get("default_identity_label"),
            # Shared sessions (one per scope): presence + timestamp, so the
            # card displays/disconnects each level. `session_set_at` remains the member's.
            "group_secret_configured": grp_st is not None,
            "group_session_set_at": grp_st["set_at"] if grp_st else None,
            "org_secret_configured": org_st is not None,
            "org_session_set_at": org_st["set_at"] if org_st else None,
            "platform_key_label": None,
            "quota_used_today": 0,
            "quota_daily": None,
        }

    # 4th loop — connectors with an OAuth credential (google; atlassian and folkmcp were here
    # until 2026-09-09, gone with the MCP federation, ADR 0069).
    # They are in NONE of the three loops above: `keyed=False`,
    # `secret_fields=0`, `secret_kind='oauth'`. So they had no entry at all —
    # and without an entry, the `pending_action` decoration just below cannot reach them,
    # nor can `health_ko`, and the sheet's verdict has nothing to read. That hole is
    # what forced the dashboard to query `/api/<name>/oauth/status`, hence to
    # know the connectors by name.
    #
    # The READ is declared by each module (`connector_link`): the three do not store
    # their credential in the same place (one row PER ACCOUNT for google, a legacy
    # ("user", sub) scope for the departing ones). The TRANSLATION to `ProviderStatus` —
    # the shape the dashboard reads — is done here, once.
    for c in providers.REGISTRY.values():
        if c.name in out["providers"] or c.secret_kind != "oauth":
            continue
        link = connector_link.state(c.name, sub) if sub else None
        if link is None:
            continue          # no declared read, or read failed: we stay silent
        entry = {
            # `forbidden` = "no key resolves", the default state of a BYO not yet
            # connected (it is NOT an RBAC refusal — cf. the connector card).
            "mode": "user" if link.linked else "forbidden",
            "user_key_configured": link.linked,
            "session_set_at": link.set_at,
            "group_secret_configured": False,
            "org_secret_configured": False,
            "platform_key_label": None,
            "quota_used_today": 0,
            "quota_daily": None,
        }
        if chain_shadow.chain_decides():
            winner = next(chain_shadow.resolution_rungs(
                sub, c.name, org=active_org, group=active_group, probe=sonde, want="byo"), None)
            entry["mode"] = winner.mode if winner else "forbidden"
        # Health (oto#25 batch a): the generic batch just below only sees the
        # MEMBER tier — invisible for this LEGACY scope (`("user", sub)`). The module
        # read ITS own row (`_link_state`); we relay it without recomputing.
        if link.health_ko:
            entry["health_ko"] = True
            entry["health_reason"] = link.health_reason
        out["providers"][c.name] = entry

    # Missing step per connector (generic `pending_action` seam, batch 2):
    # "the key resolves but a step remains" (unipile: link a channel…). The
    # specificity lives INSIDE the connector module (`status_hints.register` hook),
    # never here. Only connectors with a hook pay the cost; fail-open.
    for name, entry in out["providers"].items():
        if status_hints.has_hook(name):
            entry["pending_action"] = status_hints.pending_action(
                name, sub, active_org, active_group, entry)

    # Connector health (persistent `meta.health_ko` flag, set by the verify probe =
    # each connector's "easy read"): a "broken connector" (expired session,
    # revoked token…) stays flagged until a test/reconnection restores it.
    # Read in ONE batch on the actor's MEMBER keys — generic (any connector), fail-open.
    # ⚠️ Does NOT cover the OAuth ones of the loop above (LEGACY scope `("user",
    # sub)`, outside this batch): those already set `health_ko`/`health_reason` on
    # their entry from their own `LinkState` — `m.get("health_ko")` is absent there,
    # so this pass does not touch them (oto#25 batch a).
    if sub and active_org is not None:
        try:
            health = {r["connector"]: (r.get("meta") or {})
                      for r in credentials_store.list_credentials(
                          credentials_store.MEMBER, credentials_store.member_id(active_org, sub))
                      if r.get("account") == ""}
            for name, entry in out["providers"].items():
                m = health.get(name) or {}
                if m.get("health_ko"):
                    entry["health_ko"] = True
                    entry["health_reason"] = m.get("health_reason")
        # noqa: SILENT — per-tier fail-open on the status sheet
        except Exception:  # noqa: BLE001 — health is a bonus, never blocking
            pass

    return out
