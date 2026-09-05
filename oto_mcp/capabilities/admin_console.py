"""Console admin MCP consolidée par concept (ADR 0009, fusion `*_op`).

Réunit les verbes d'un même objet métier en UN outil `oto_admin_<objet>(op=…)`, au
service du provisioning d'org de bout en bout depuis Claude. L'autz reste DÉCLARÉE
(combinateur op-aware `ADMIN_BY_OP` quand les paliers divergent) — jamais redescendue
dans le handler. Les handlers de domaine sont **réutilisés tels quels** (on construit
leur Input spécifique depuis l'Input consolidé) ; aucune logique n'est dupliquée. Les
faces REST historiques ne bougent pas : on retire seulement le binding `mcp=` des
capacités d'origine.

Concepts : `oto_admin_org`, `oto_admin_org_member`, `oto_admin_user`,
`oto_admin_key_grant`. Hors périmètre (décision 2026-06-25) :
pose de secret brut (`set_org_secret`/`set_platform_key`) = dashboard-only.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel

from .. import credentials_store, db
from . import platform_invites, unipile_seats, users_admin
from .orgs import (admin as orgs_admin, members as orgs_members,
                   reads as orgs_reads)
from ._authz import ADMIN_BY_OP, ORG_ADMIN_OF, ORG_MEMBER_OF, PLATFORM_ADMIN, SUPER_ADMIN
from ._types import AuthzDenied, Capability, ResolvedCtx
from ._execution import execute
from .registry import CAPABILITIES, by_key


def _need(val, code: str, msg: str):
    if val is None or (isinstance(val, str) and not val.strip()):
        raise AuthzDenied(400, code, msg)
    return val


# ── oto_admin_org : create / archive / list / get ────────────────────────────
class OrgAdminInput(BaseModel):
    op: Literal["create", "archive", "list", "get"]
    name: Optional[str] = None        # create
    admin: Optional[str] = None       # create : responsable (email|sub|"me"), défaut = toi
    org_id: Optional[int] = None      # archive / get


def _org(ctx: ResolvedCtx, inp: OrgAdminInput) -> dict:
    if inp.op == "create":
        return orgs_admin._create_org(ctx, orgs_admin.CreateOrgInput(
            name=_need(inp.name, "missing_name", "`name` requis pour create."),
            admin=inp.admin))
    if inp.op == "list":
        return orgs_reads._list_all_orgs(ctx, orgs_reads.NoInput())
    oid = _need(inp.org_id, "missing_org", f"`org_id` requis pour {inp.op}.")
    if inp.op == "archive":
        return orgs_admin._archive_org(ctx, orgs_admin.OrgIdInput(org_id=oid))
    return orgs_reads._org_detail(ctx, orgs_reads.OrgIdInput(org_id=oid))  # get


# ── oto_admin_org_member : add / remove / set_role / list ────────────────────
class OrgMemberAdminInput(BaseModel):
    op: Literal["add", "remove", "set_role", "list", "connectors"]
    org_id: int
    target: Optional[str] = None      # add/remove/set_role/connectors : email ou sub
    role: Optional[str] = None        # add/set_role


def _org_member(ctx: ResolvedCtx, inp: OrgMemberAdminInput) -> dict:
    if inp.op == "list":
        return {"org_id": inp.org_id, "members": orgs_reads._members(inp.org_id)}
    target = _need(inp.target, "missing_target", "`target` (email ou sub) requis.")
    if inp.op == "connectors":
        # Projection admin des credentials d'un MEMBRE (scope member, ADR 0033) :
        # connector/account/secret_kind/set_at/meta public — JAMAIS de secret
        # (list_credentials filtre déjà par `_public_meta`). Diagnostic d'une clé
        # byo_user qui échoue (ex. data_center Zoho absent) sans lire la DB (#186).
        sub = orgs_members._resolve_target(target)
        eid = credentials_store.member_id(inp.org_id, sub)
        return {"org_id": inp.org_id, "sub": sub,
                "connectors": credentials_store.list_credentials(credentials_store.MEMBER, eid)}
    if inp.op == "add":
        return orgs_members._add_member(ctx, orgs_members.AddMemberInput(
            org_id=inp.org_id, target=target, role=inp.role or "org_member"))
    if inp.op == "remove":
        return orgs_members._remove_member(ctx, orgs_members.RemoveMemberInput(
            org_id=inp.org_id, target=target))
    # set_role — résout target→sub (le handler de domaine prend un sub direct).
    role = _need(inp.role, "missing_role", "`role` requis pour set_role.")
    sub = orgs_members._resolve_target(target)
    return orgs_members._set_member_role(ctx, orgs_members.SetMemberRoleInput(
        org_id=inp.org_id, sub=sub, role=role))


# ── oto_admin_user : list / get / set_role ───────────────────────────────────
class UserAdminInput(BaseModel):
    op: Literal["list", "get", "set_role"]
    target: Optional[str] = None      # get/set_role : email ou sub
    query: Optional[str] = None       # list : filtre substring
    role: Optional[str] = None        # set_role : member|admin|super_admin


def _user(ctx: ResolvedCtx, inp: UserAdminInput) -> dict:
    if inp.op == "list":
        return users_admin._list_users(ctx, users_admin.UserListInput(query=inp.query))
    target = _need(inp.target, "missing_target", "`target` (email ou sub) requis.")
    if inp.op == "get":
        return users_admin._user_detail(ctx, users_admin.UserGetInput(target=target))
    role = _need(inp.role, "missing_role", "`role` requis pour set_role.")
    return users_admin._set_role(ctx, users_admin.SetRoleInput(target=target, role=role))


# ── oto_admin_key_grant : list / grant / revoke · scope user|org (DROITS, pas de secret) ─
class KeyGrantInput(BaseModel):
    op: Literal["list", "grant", "revoke"]
    scope: Optional[Literal["user", "org"]] = None  # grant/revoke seulement
    target: Optional[str] = None      # scope=user : email ou sub
    org_id: Optional[int] = None      # scope=org
    provider: Optional[str] = None    # grant/revoke : connecteur ciblé ; op=list : filtre
    daily_quota: Optional[int] = None  # grant (optionnel)


def _key_grant(ctx: ResolvedCtx, inp: KeyGrantInput) -> dict:
    if inp.op == "list":
        # Inventaire des clés plateforme posées (quels vendors oto contracte). Le
        # SECRET n'est JAMAIS renvoyé — on ne montre que l'identité (provider, label).
        # ADR 0044 §F : instances scope PLATFORM du coffre unifié (plus platform_keys).
        keys = credentials_store.list_platform_credentials(inp.provider)
        return {"keys": keys, "count": len(keys)}
    scope = _need(inp.scope, "missing_scope", "`scope` (user|org) requis pour grant/revoke.")
    provider = _need(inp.provider, "missing_provider", "`provider` (connecteur) requis.")
    if inp.scope == "user":
        target = _need(inp.target, "missing_target", "scope=user : `target` requis.")
        if inp.op == "grant":
            return users_admin._grant_key(ctx, users_admin.GrantKeyInput(
                target=target, provider=provider, daily_quota=inp.daily_quota))
        return users_admin._revoke_key(ctx, users_admin.RevokeKeyInput(target=target, provider=provider))
    org_id = _need(inp.org_id, "missing_org", "scope=org : `org_id` requis.")
    if inp.op == "grant":
        return users_admin._grant_org_key(ctx, users_admin.OrgGrantKeyInput(
            org_id=org_id, provider=provider, daily_quota=inp.daily_quota))
    return users_admin._revoke_org_key(ctx, users_admin.OrgRevokeKeyInput(org_id=org_id, provider=provider))


# ── oto_admin_guide : get / list / set / delete (org ciblée, ADR 0047 B2) ────
class GuideAdminInput(BaseModel):
    op: Literal["get", "list", "set", "delete"]
    org_id: int
    slug: Optional[str] = None        # get (None = base+index) / set (None = base) / delete
    scope: Optional[str] = None       # get/list : org (défaut) | group
    version: Optional[int] = None     # get
    with_history: bool = False        # get
    query: Optional[str] = None       # list
    body_md: Optional[str] = None     # set
    title: Optional[str] = None       # set
    description: Optional[str] = None  # set
    from_version: Optional[int] = None  # set (revert)
    slots: Optional[list] = None      # set (ADR 0035)


_GUIDE_OPERATIONS = {
    op: by_key(key) for op, key in {
        "get": "org.guide.admin_get", "list": "org.guide.admin_list",
        "set": "org.instruction.admin_set", "delete": "org.instruction.admin_delete",
    }.items()
}


async def _guide(ctx: ResolvedCtx, inp: GuideAdminInput) -> dict:
    operation = _GUIDE_OPERATIONS[inp.op]

    def prepare():
        # Le schéma public de console reste plat ; le modèle du verbe REST est
        # l'unique validation métier. Ses défauts s'appliquent aux champs absents.
        data = inp.model_dump(include=set(operation.Input.model_fields))
        if inp.op == "get":
            data["scope"] = inp.scope or "org"
        elif inp.op == "delete":
            data["slug"] = _need(inp.slug, "missing_slug", "`slug` requis pour delete.")
        return ctx, operation.Input(**data)

    # L'autz du verbe a déjà été exécutée par l'adaptateur via ADMIN_BY_OP.
    # Rejouer la capacité entière ici vérifierait deux fois la même règle.
    _, result = await execute(operation.handler, prepare)
    return result


# ── oto_admin_invite : create / list / revoke (invitation plateforme, cascade) ─
class InviteAdminInput(BaseModel):
    op: Literal["create", "list", "revoke"]
    email: Optional[str] = None       # create : destinataire (None = lien à partager)
    org_id: Optional[int] = None      # create : org cible optionnelle (None = onboarding pur)
    role: Optional[str] = None        # create : org_member (défaut) | org_admin (si org_id)
    send_email: bool = True           # create
    invite_id: Optional[int] = None   # revoke


def _invite(ctx: ResolvedCtx, inp: InviteAdminInput) -> dict:
    if inp.op == "list":
        return platform_invites._invite_list(ctx, platform_invites._NoInput())
    if inp.op == "create":
        return platform_invites._invite_create(ctx, platform_invites.PlatformInviteCreateInput(
            email=inp.email, org_id=inp.org_id, role=inp.role or "org_member",
            send_email=inp.send_email))
    return platform_invites._invite_revoke(ctx, platform_invites.PlatformInviteRevokeInput(
        invite_id=_need(inp.invite_id, "missing_invite_id", "`invite_id` requis pour revoke.")))


# ── oto_admin_unipile_seat : list / release (ce que la plateforme PAIE) ──────
class UnipileSeatAdminInput(BaseModel):
    op: Literal["list", "release"]
    account_id: Optional[str] = None      # release


class UnipileSeatConsoleOut(BaseModel):
    """Les deux formes d'un même concept : l'inventaire (op=list) et l'accusé de
    libération (op=release). Champs optionnels plutôt que deux schémas, parce que la
    console est UN tool — celui qui lit le schéma doit voir les deux sorties."""
    # op=list
    configured: Optional[bool] = None
    instance_dsn: Optional[str] = None
    seats: Optional[list[unipile_seats.Seat]] = None
    orphan_count: Optional[int] = None
    reclaimable_count: Optional[int] = None
    # op=release
    ok: Optional[bool] = None
    account_id: Optional[str] = None
    was: Optional[str] = None


async def _unipile_seat(ctx: ResolvedCtx, inp: UnipileSeatAdminInput) -> dict:
    if inp.op == "list":
        return await unipile_seats._list_seats(ctx, unipile_seats.SeatsListInput())
    return await unipile_seats._release_seat(ctx, unipile_seats.SeatReleaseInput(
        account_id=_need(inp.account_id, "missing_account_id",
                         "`account_id` requis pour release.")))


# ── oto_admin_signal : list / set_status (boucle d'usage, ADR 0017) ──────────
#
# `op=resolve` a été REMPLACÉ par `op=set_status` (#450) : le verbe « résoudre »
# ne savait dire ni « je l'ai lu, je ne sais pas encore » ni « je ne le ferai pas »,
# qui sont précisément les deux gestes de triage qui manquaient. Un signal qu'on
# refuse n'est pas résolu, et le forcer dans ce mot rendait le refus invisible.
class SignalAdminInput(BaseModel):
    op: Literal["list", "set_status", "reroute", "notify_preview", "notify_send"]
    signal: Optional[str] = None      # list : tool_feedback | gap
    target: Optional[str] = None      # list
    # list : open | acknowledged | declined | resolved | pending | None (tous)
    # set_status : l'état à poser (pending n'en est pas un — c'est un filtre)
    status: Optional[str] = None
    limit: int = 200                  # list
    signal_id: Optional[int] = None   # set_status / reroute
    note: Optional[str] = None        # set_status : ce qui a été décidé, et pourquoi
    # reroute : la destination, `<id>` d'un espace ou `platform` en toutes lettres.
    #
    # Une CHAÎNE, et pas l'`org_id: Optional[int]` de la route REST, parce que la
    # contrainte n'est pas la même. Là-bas le champ est REQUIS et `None` y est une
    # destination — la plateforme — jamais « ne rien changer » : l'écriture est
    # toujours un choix. Ici tous les champs d'une console sont optionnels par
    # construction, et fastmcp remplit les défauts avant d'appeler le handler
    # (vérifié) — un `org_id` omis et un `org_id: null` arrivent donc RIGOUREUSEMENT
    # identiques, et `model_fields_set` ne tranche rien. Le mot rétablit la
    # distinction : absent = non dit (refusé), `platform` = voulu.
    to_org: Optional[str] = None
    # notify_* : restreint le retour à ces destinataires (emails ou subs). Vide = tous.
    only: Optional[list[str]] = None


def _destination(brut: Optional[str]) -> Optional[int]:
    """`<id>` → l'espace, `platform` → la plateforme, rien → un refus qui le dit.

    Aucun repli : un mot de travers ne doit pas se faire lire comme « la plateforme »,
    sans quoi une faute de frappe sortirait le signal de son espace en silence — le
    défaut même qu'on répare, en pire (plus personne ne le voit et rien ne le dit)."""
    val = (brut or "").strip()
    if not val:
        raise AuthzDenied(
            400, "missing_to_org",
            "`to_org` requis pour reroute : l'id de l'espace de destination, ou "
            "`platform` pour l'en sortir sans le rattacher ailleurs. Une destination "
            "non dite serait un déplacement que personne n'a demandé.")
    if val.lower() == "platform":
        return None
    if not val.lstrip("+").isdigit():
        raise AuthzDenied(
            400, "invalid_to_org",
            f"`to_org={val}` n'est ni un id d'espace ni `platform`.")
    return int(val)


def _signal(ctx: ResolvedCtx, inp: SignalAdminInput) -> dict:
    from . import usage
    if inp.op == "list":
        return usage._signals(ctx, usage.SignalsInput(
            signal=inp.signal, target=inp.target, status=inp.status, limit=inp.limit))
    if inp.op.startswith("notify_"):
        # Deux `op` plutôt qu'un booléen `send=`, et le préfixe le rappelle à chaque
        # appel : ces mails partent chez des tiers sous notre marque, l'aperçu ne
        # touche à rien et l'envoi est un ACTE. `preview` reste ce que fait un appel
        # étourdi.
        return usage._notify_reporters(ctx, usage.NotifyReportersInput(
            op="send" if inp.op == "notify_send" else "preview", only=inp.only))
    if inp.op == "reroute":
        return usage._reroute_signal(ctx, usage.RerouteSignalInput(
            signal_id=_need(inp.signal_id, "missing_signal_id",
                            "`signal_id` requis pour reroute."),
            org_id=_destination(inp.to_org)))
    return usage._set_signal_status(ctx, usage.SetSignalStatusInput(
        signal_id=_need(inp.signal_id, "missing_signal_id",
                        "`signal_id` requis pour set_status."),
        status=_need(inp.status, "missing_status",
                     "`status` requis pour set_status : open | acknowledged | "
                     "declined | resolved."),
        note=inp.note))


CAPABILITIES += [
    Capability(
        key="admin.org", handler=_org, Input=OrgAdminInput,
        authz=ADMIN_BY_OP({"create": SUPER_ADMIN, "archive": SUPER_ADMIN,
                           "list": PLATFORM_ADMIN, "get": PLATFORM_ADMIN}),
        description=("Manage organizations. op=create (`name` + `admin` = email|sub of "
                     "the person who will run it, or \"me\"/omitted for yourself; super "
                     "admin) / archive (`org_id`, super admin) / list (all orgs, platform admin) / get "
                     "(`org_id` → full fiche: members, secrets, entitlements, grants; platform admin)."),
        mcp="oto_admin_org",
    ),
    Capability(
        key="admin.org_member", handler=_org_member, Input=OrgMemberAdminInput,
        authz=ADMIN_BY_OP({"add": ORG_ADMIN_OF("org_id"),
                           "remove": ORG_ADMIN_OF("org_id"),
                           "set_role": ORG_ADMIN_OF("org_id"),
                           "connectors": ORG_ADMIN_OF("org_id"),
                           "list": PLATFORM_ADMIN}),
        description=("Manage an org's members (org_admin of `org_id`; list = platform admin). "
                     "op=add (`target` email|sub, `role` org_member|org_admin) / remove (`target`) / "
                     "set_role (`target`, `role`) / list / connectors (`target` — the member's "
                     "configured connectors: connector/account/secret_kind/set_at/public meta, "
                     "NEVER any secret — diagnose a failing byo_user key). Anti-lockout on the last org_admin."),
        mcp="oto_admin_org_member",
    ),
    Capability(
        key="admin.user", handler=_user, Input=UserAdminInput,
        authz=ADMIN_BY_OP({"list": PLATFORM_ADMIN, "get": PLATFORM_ADMIN,
                           "set_role": SUPER_ADMIN}),
        description=("Accounts. op=list (optional `query` email/name/sub; platform admin) / get "
                     "(`target` email|sub → full fiche; platform admin) / set_role (`target`, "
                     "`role` member|admin|super_admin; super admin)."),
        mcp="oto_admin_user",
    ),
    Capability(
        key="admin.key_grant", handler=_key_grant, Input=KeyGrantInput,
        authz=ADMIN_BY_OP({"list": PLATFORM_ADMIN, "grant": SUPER_ADMIN, "revoke": SUPER_ADMIN}),
        description=("Platform keys as a RIGHT — never reveals the secret. "
                     "op=list (which vendors oto contracts: provider, label; optional "
                     "`provider` filter; platform admin) / grant|revoke (by `provider`, super admin) · "
                     "scope=user (`target` email|sub) | org (`org_id`); grant takes optional "
                     "`daily_quota`. To POSE a raw key/secret, use the dashboard."),
        mcp="oto_admin_key_grant",
    ),
    Capability(
        key="admin.guide", handler=_guide, Input=GuideAdminInput,
        authz=ADMIN_BY_OP({op: operation.authz
                           for op, operation in _GUIDE_OPERATIONS.items()}),
        description=("[ADMIN] Another org's guide, by `org_id` (cross-org = platform "
                     "admin). op=get (`slug` = one skill, none = base+index; `scope=group`) "
                     "/ list (named guides incl. base) / set (write: omit slug = base; "
                     "`from_version` restores; `slots` = required entities ADR 0035) / "
                     "delete (exact `slug`, drops history)."),
        mcp="oto_admin_guide",
    ),
    Capability(
        key="admin.invite", handler=_invite, Input=InviteAdminInput,
        authz=PLATFORM_ADMIN,
        description=("Invite users to the oto platform (feature cascade, platform tier). "
                     "op=create (optional `email` + `org_id` to attach directly to an org "
                     "with `role` org_member|org_admin — omit org_id for pure onboarding; "
                     "send_email=false returns the link only) / list (pending platform "
                     "invitations) / revoke (`invite_id`). Platform admin."),
        mcp="oto_admin_invite",
    ),
    Capability(
        key="admin.unipile_seat", handler=_unipile_seat, Input=UnipileSeatAdminInput,
        authz=SUPER_ADMIN, Output=UnipileSeatConsoleOut,
        description=("Seats on the shared unipile platform key — what the platform PAYS "
                     "for (super admin). op=list (each seat + `state`: bound in service | "
                     "disconnected, owner unhooked it on oto but it still bills | orphan, "
                     "nobody claims it; `reclaimable_count` = what you can stop paying) / "
                     "release (`account_id` — deletes it on unipile. IRREVERSIBLE; refuses "
                     "a seat still in service, that disconnection belongs to its owner)."),
        mcp="oto_admin_unipile_seat",
    ),
    Capability(
        key="admin.signal", handler=_signal, Input=SignalAdminInput,
        authz=PLATFORM_ADMIN,
        description=("Usage signals reported about oto (feedback/gap; platform admin). "
                     "op=list (most recent first + `counts` per status; filters `signal` "
                     "tool_feedback|gap, `target`, `status` open|acknowledged|declined|"
                     "resolved, or 'pending' = everything left to arbitrate) / set_status "
                     "(`signal_id`, `status`, `note` = what was decided — REQUIRED to "
                     "decline). Four states: open (nobody looked yet), acknowledged (read, "
                     "not decided), declined (won't do), resolved (done). "
                     "op=reroute (`signal_id`, `to_org` = the workspace id it was "
                     "really about, or `platform` to lift it out — a signal filed "
                     "against the wrong workspace is MOVED, never deleted: only its "
                     "address was wrong, the fact is real). "
                     "op=notify_preview (who would be told what was decided about "
                     "their agents' signals — sends NOTHING) / notify_send (actually "
                     "sends ONE grouped email per person, never one per signal; "
                     "`only` = restrict to these emails/subs)."),
        mcp="oto_admin_signal",
    ),
]
