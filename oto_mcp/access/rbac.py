"""Who has the RIGHT — access governance, outside resolution (ADR 0031/0038/0044).

Three families, all GUARDS or ENUMERATIONS, never a resolution:

- **tool visibility** hidden by the org_admin or the team lead (0031);
- **instance sharing**: the guard of a pinned instance (0038 B6), another person's
  account only where its rules open it (#1499);
- **instances within reach**: what a call token could legitimately reach, so
  that the "nothing resolves" error surfaces the choices instead of a bare refusal.

Added to these is `resolve_field_filter`: the REDACTION policy of the active org —
same nature (what the org governs applies to the actor), another surface (the
response fields rather than access to the connector).

There is no more connector RBAC (ADR 0025/0012 B2, removed 2026-09-24):
reserving a connector for part of the members no longer exists. Restricting means
PLACING the key at the right level (ADR 0053 D1) — a personal key is only resolved for
its holder, a team key only for the team's members.

Depends on `scope` (role, context, membership) and `cascade` (the list of
org-shareable connectors). Does NOT depend on resolution: it is resolution that
calls these guards.
"""
from __future__ import annotations

import logging
from typing import Optional

from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import credentials_store, db, group_store, org_store, providers
from ..auth.hooks import current_user_sub_from_token
from . import cascade, scope

logger = logging.getLogger(__name__)


def org_admin_hidden_tools(org: Optional[int]) -> set:
    """Tools hidden BY DEFAULT for `org` (denylist set by the org_admin) —
    visibility governance, NOT a security barrier (ADR 0031, same spirit
    as `tool_visibility.DEFAULT_HIDDEN_TOOLS`): a positive personal override
    (`user_enabled_tools`) always lifts it. No escalation to exempt — even an
    org_admin who hid the tool sees it hidden, and re-enables it for themselves
    like anyone else (consistent with DEFAULT_HIDDEN_TOOLS today). RAISES on a
    DB hiccup: each surface (session_visibility, oto_list_my_tools) keeps its
    own fail-open rule, independent of the team tier."""
    if org is None:
        return set()
    return set(db.list_org_disabled_tools(org))


def group_admin_hidden_tools(group: Optional[int]) -> set:
    """Mirror at TEAM grain — a team lead hides a tool for THEIR team.
    Purely additive (the caller UNIONs this result with `org_admin_hidden_tools`): this
    seam never expresses a lift, so a team can never reveal a tool
    the org hid. RAISES on a DB hiccup (fail-open per tier, up to the
    caller)."""
    if group is None:
        return set()
    return set(db.list_group_disabled_tools(group))


def _ouvert_par_une_regle(sub: str, ref, entity_type: str, entity_id: str, *,
                          refus: str, nominative: bool = False) -> Optional[int]:
    """An account the caller does not own nor reach by membership, NAMED by `_instance=`:
    it serves if its rules give her a level (#1499) — another person's account of her
    org, or an account of another org a rule naming her opens (D1), or her own key in
    another org (#172). The caller keeps her org. A refusal says nothing of the account
    beyond what the caller already named."""
    from . import comptes, niveaux
    org_appel = scope.current_org(sub)
    porteur = providers.credential_provider(ref.connector)
    trouves = comptes.comptes_des_lignes(porteur, [(entity_type, entity_id, ref.account or "")])
    compte = trouves.get((entity_type, entity_id, ref.account or ""))
    appelant = niveaux.Appelant(sub, org_appel, lambda: scope.current_group(sub))
    if nominative and compte is not None and not any(
            r.user == sub and r.level != "none" for r in compte.regles):
        compte = None   # only a rule NAMING her opens it, never a default
    if compte is None or niveaux.RANG[niveaux.niveau(appelant, compte, besoin="read")] == 0:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(f"Instance refused: {refus} "
                     "(oto_instance op='rules' on an account you manage; "
                     "oto_identity op='list' for those you can name)."),
            data={"code": niveaux.CODE_NIVEAU_INSUFFISANT, "retryable": False}))
    return org_appel


def guard_instance_access(sub: str, ref) -> Optional[int]:
    """Access guard for a connector instance by LEVEL (ADR 0038 B6) — same
    semantics as the B4 projection: member = MY row in an org I am a member of;
    group = group I am a reader of; org = org I am a member of;
    platform = refused (the grant already resolves at the last tier). Returns the
    instance's org (to co-set). Actionable McpError otherwise. Sync DB path — hot
    inbound callers: threadpool. Shared by the `_instance=` axis (pin) and the
    resolution of a project binding (re-guard for the CALLER, who is not
    necessarily the one who bound)."""
    from .. import group_store, roles
    # Batch L6: `parse_ref` now accepts the stable identifier `inst:{id}` — but
    # NOTHING resolves it yet (resolution by identifier is L7). Without this
    # branch, an `inst:` would fall into the final refusal and be told that
    # "`platform:` refs cannot be pinned": a wrong message is worse than a
    # refusal, it sends people looking in the wrong place.
    if ref.level == "inst":
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=("The `inst:` instance identifier cannot be pinned yet: "
                     "pass back the `ref` returned by oto_instance(op='list').")))
    if ref.level == "member":
        if ref.sub == sub and roles.is_org_member(sub, ref.org_id):
            return ref.org_id                    # owner: my own instance, in its org
        # ANOTHER person's account, or the caller's own key in an org she cannot act in
        # (a delegated job locked to its org, #172), or an account of another org a rule
        # naming her opens (D1, #1499): pinned, it serves to someone its rules open
        # it to — the exact level the call requires is judged at resolution
        # (`resolve._resolve_pinned_instance`). The caller KEEPS her org.
        return _ouvert_par_une_regle(
            sub, ref, credentials_store.MEMBER,
            credentials_store.member_id(ref.org_id, ref.sub),
            refus=("you are no longer a member of its org" if ref.sub == sub
                   else "it belongs to another member and no rule opens it to you"))
    if ref.level == "group":
        # Group reader = member OR org admin (escalation `can_read_group`,
        # roles.py) — this is the path by which an org_admin uses the instance
        # of a team in their org (pin `_instance=` / project binding).
        if not roles.can_read_group(sub, ref.group_id):
            # Not a reader: a team account of ANOTHER org a rule naming her opens (D1).
            return _ouvert_par_une_regle(sub, ref, "group", str(ref.group_id),
                                         refus=f"you are not a member of group "
                                               f"#{ref.group_id}")
        g = group_store.get_group(ref.group_id)
        return g.get("org_id") if g else None
    if ref.level == "org":
        if not roles.is_org_member(sub, ref.org_id):
            # Not a member: only an org account a rule NAMING her opens (D1, or a shared
            # project's inheritance taken over, #480) — also under `_project=` of that org
            # or a project binding, where the call's org is that org: the members' default
            # never reaches her (`nominative`).
            return _ouvert_par_une_regle(sub, ref, "org", str(ref.org_id),
                                         refus=f"you are not a member of org #{ref.org_id}",
                                         nominative=True)
        return ref.org_id
    if ref.level == "tenant":
        # L-keys PR 1: a tenant's key is pinned by its accounts and them alone — the
        # tenant is read from the qualified sub (`rung_tenant`), never from the org. The
        # context stays the CALLER's (the key carries no org).
        from .. import tenant_vault
        if tenant_vault.rung_tenant(sub) != ref.tenant:
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=(f"Instance refused: this key belongs to tenant "
                         f"`{ref.tenant}`, and your account is not part of it.")))
        return scope.current_org(sub)
    raise McpError(ErrorData(
        code=INVALID_PARAMS,
        message="`platform:` refs cannot be pinned (the platform grant "
                "already resolves by itself at the last tier)."))


def reachable_instances(sub: str, org: Optional[int], provider: str) -> list[dict]:
    """`provider` instances usable in a context OTHER than the ambient one:
    teams of `org` that `sub` is a MEMBER of (secret present, team not necessarily
    active) + their OTHER orgs (shared org key, or their member key there). The
    cascade does not read them — but a call token (`_group=`/`_org=`/`_instance=`)
    legitimately reaches them (same membership guards). Feeds the
    "nothing resolves" error: we SURFACE the choices so the agent pins explicitly,
    never a silent choice between identities. Best-effort: never raises,
    returns whatever could be enumerated (seen with Zoho/a client 2026-07-16: key on
    the sales team, 3 members, 0 active → bare "no key" and a lost session).

    `provider` is normalized to the credential's CARRIER (delegation): the
    instances of a unipile channel ARE those of the account, there are no others.
    Without that, a channel's card lost the "a team has the key" signal — and for
    a per-person connector, that is the signal that avoids reconnecting an account
    already linked elsewhere (the `account_id` duplicate of #172)."""
    provider = providers.credential_provider(provider)
    out: list[dict] = []
    shareable = provider in cascade.ORG_SHAREABLE_PROVIDERS
    try:
        if org is not None and shareable:
            seen_gids: set = set()
            # One batched read for all the caller's teams, never one per team (#1317).
            mes_equipes = group_store.list_groups_for_user(sub, org)
            detiennent = group_store.groups_holding(
                [g["group_id"] for g in mes_equipes], org, provider)
            for g in mes_equipes:
                if int(g["group_id"]) in detiennent:
                    out.append({"kind": "group", "id": g["group_id"],
                                "name": g["name"]})
                    seen_gids.add(g["group_id"])
            # #218: the org_admin GOVERNS their teams without being a MEMBER — their group
            # key is reachable by escalation (group=/instance= re-guarded by
            # can_read_group). The hint was blind there (list_groups_for_user = STRICT
            # member) → bare "no key" while the key exists on a team they
            # govern. We complete with the org's teams visible by escalation.
            from .. import roles
            try:  # ISOLATED best-effort escalation: must not damage the enumeration
                if roles.is_org_admin(sub, org):  # below (orgs) if it hiccups.
                    gouvernees = [g for g in group_store.list_groups(org)
                                  if g["id"] not in seen_gids]
                    detiennent = group_store.groups_holding(
                        [g["id"] for g in gouvernees], org, provider)
                    for g in gouvernees:
                        if int(g["id"]) in detiennent:
                            out.append({"kind": "group", "id": g["id"], "name": g["name"]})
                            seen_gids.add(g["id"])
            # noqa: SILENT — visibility fail-open, hard backstop at call time
            except Exception:
                pass
        for o in org_store.list_orgs_for_user(sub):
            oid = o["org_id"]
            if oid == org:
                continue
            if ((shareable and org_store.has_org_secret(oid, provider))
                    or db.has_member_api_key(sub, oid, provider)):
                out.append({"kind": "org", "id": oid,
                            "name": o.get("name") or f"org {oid}"})
    # noqa: SILENT — visibility fail-open, hard backstop at call time
    except Exception:
        return out
    return out


def reachable_team_key(sub: str, org: Optional[int], provider: str,
                       groups: "Optional[list[dict]]" = None,
                       secrets_by_group: "Optional[dict]" = None) -> Optional[dict]:
    """First team of `org` that `sub` is a member of and that holds a `provider`
    secret — the `team_key_group` hint of `status_for` (drawer). `groups` =
    preloaded list from `list_groups_for_user` (hoisted by the batch caller,
    /api/me loops over ~50 providers). Best-effort, never raises.

    `secrets_by_group` = the map from `cascade.group_secret_map` when the caller
    has already built it. Without it the behavior is UNCHANGED (one read per
    team and per connector): that is what the other callers and the tests do.
    With it, `/api/me` stops paying a round trip per `forbidden` connector —
    the majority of a real account, 67 reads measured on a single team, and as
    many more per additional team.

    The ORDER is the contract: the first team in `groups` that holds the secret
    wins, map or not. Answering from the map only changes where the answer is
    read, never which one."""
    if org is None or provider not in cascade.ORG_SHAREABLE_PROVIDERS:
        return None
    try:
        if groups is None:
            groups = group_store.list_groups_for_user(sub, org)
        for g in groups:
            detient = (provider in secrets_by_group.get(int(g["group_id"]), ())
                       if secrets_by_group is not None
                       else group_store.has_group_secret(g["group_id"], provider))
            if detient:
                return {"id": g["group_id"], "name": g["name"]}
    # noqa: SILENT — visibility fail-open, hard backstop at call time
    except Exception:
        return None
    return None


def reachable_instances_map(sub: str, org: Optional[int]) -> dict[str, list[dict]]:
    """`{provider: [instances within reach]}` in ONE pass — BATCHED version of
    `reachable_instances`, to annotate a whole catalog (≈40 connectors).

    `reachable_instances` queries the DB **per provider** (has_group_secret /
    has_org_secret): calling it in a loop over the catalog would make N×M
    round trips on a single-loop server. Here we list each entity's secrets
    ONCE (`list_group_secrets` / `list_org_secrets`) and invert in
    memory → cost bounded by the number of teams + orgs, not providers.

    Accepted limit: does not cover "my MEMBER key in another org" (no grouped
    listing on the `db.has_member_api_key` side, which is per-provider). The error
    hint does cover it — the catalog is a discovery surface, not the
    authority. Best-effort: never raises."""
    out: dict[str, list[dict]] = {}

    def _add(provider: str, item: dict) -> None:
        out.setdefault(provider, []).append(item)

    try:
        if org is not None:
            seen_gids: set = set()
            groups = list(group_store.list_groups_for_user(sub, org))
            # #218: the org_admin governs their teams without being a member — their team
            # keys are reachable by escalation (re-guarded at call time).
            from .. import roles
            try:
                if roles.is_org_admin(sub, org):
                    known = {g["group_id"] for g in groups}
                    groups += [{"group_id": g["id"], "name": g["name"]}
                               for g in group_store.list_groups(org)
                               if g["id"] not in known]
            # noqa: SILENT — visibility fail-open, hard backstop at call time
            except Exception:
                pass
            for g in groups:
                gid = g["group_id"]
                if gid in seen_gids:
                    continue
                seen_gids.add(gid)
                for s in group_store.list_group_secrets(gid):
                    p = s.get("provider")
                    if p and p in cascade.ORG_SHAREABLE_PROVIDERS:
                        _add(p, {"kind": "group", "id": gid, "name": g["name"]})
        for o in org_store.list_orgs_for_user(sub):
            oid = o["org_id"]
            if oid == org:
                continue
            for s in org_store.list_org_secrets(oid):
                p = s.get("provider")
                if p and p in cascade.ORG_SHAREABLE_PROVIDERS:
                    _add(p, {"kind": "org", "id": oid,
                             "name": o.get("name") or f"org {oid}"})
    # noqa: SILENT — visibility fail-open, hard backstop at call time
    except Exception:
        return out
    # Delegation: a channel's card must show the instances of ITS account —
    # they are the only ones that exist, and it is the same list, not an approximation.
    # The catalog annotates by connector NAME (`connectors_selection`), so without
    # this alias the `whatsapp` row stays mute while `unipile` shows the team
    # key that would make it work.
    for c in providers._REGISTRY_LIST:
        if c.credential_of and c.credential_of in out:
            out[c.name] = list(out[c.credential_of])
    return out


def resolve_org_field_policy(service: str) -> Optional[dict]:
    """La politique de rédaction que l'**org active** de l'appelant a posée pour
    `service`, telle que stockée (`rules`, `salt`, `unmask`, `documents`), ou None si
    elle n'en a pas posé (ou sans org active). Ce n'est PAS ce qui s'applique : la
    cascade est `field_filter_defaults.bloc_effectif`. Une erreur DB LÈVE.

    Sans `sub`, l'org est celle que `scope.current_org(None)` résout : l'org
    PROPRIÉTAIRE d'un projet publié sans login (`subdomain_project`), qui sert ses
    outils et ses tableaux à un tiers sans compte. C'est elle qui a posé la
    politique, et c'est là qu'elle compte le plus. Hors d'un tel endpoint, None."""
    sub = current_user_sub_from_token()
    if sub:
        active_org = scope.current_org(sub)
    else:
        # Sans appelant, SEUL un endpoint publié porte une org : son propriétaire.
        # Hors d'un tel endpoint, aucune — jamais une org persistée ou devinée.
        from .. import subdomain_project
        active_org = subdomain_project.current_anon_org()
    if active_org is None:
        return None
    return org_store.get_org_field_filters(active_org).get(service)


def resolve_field_filter(service: str):
    """Builds the `FieldFilter` to apply to a connector's responses for
    the current sub, according to the redaction policy of its **active org**.

    Cascade (`field_filter_defaults.bloc_effectif`, the single source):
      1. no org policy → the **server default** (explicit PII floor, e.g. PayFit
         NIR/IBAN), otherwise an empty filter (no-op);
      2. service without a floor → the org policy is **authoritative**;
      3. service with a floor → the policy is **added to** the floor, which can only be
         lifted by naming its fields (`unmask`) — decision of 2026-10-06, oto signal
         #1269; a stored policy that lifted without naming (`rules: []` alone) no longer
         lifts anything, and reading it logs that.

    Without an active org, we fall back to the server default. A DB error, however, RAISES:
    the caller (`redaction.redact_payload`) then withholds the output (#1045)."""
    from .. import field_filter_defaults

    block = resolve_org_field_policy(service)
    if field_filter_defaults.leve_sans_nommer(service, block):
        logger.warning("field filters: the org policy for %s lifts the floor without "
                       "naming a field (`rules: []`) — it has lifted nothing since "
                       "2026-10-06, the floor applies", service)
    return field_filter_defaults.filtre(field_filter_defaults.bloc_effectif(service, block))
