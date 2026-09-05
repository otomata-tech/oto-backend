"""Capacités d'administration centrées USER (ADR 0009).

Migre le bloc user-admin (jadis REST-only, écrit main dans `api/routes.py`) vers
des capacités co-déclarées → faces MCP **et** REST dérivées d'une seule déclaration,
sur les **mêmes chemins REST** (dashboard inchangé). Permet de setup complètement un
compte depuis Claude : retrouver un user, voir son état, poser son rôle, lui grant une
clé plateforme (user/org), offrir une option payante.

Logique reprise verbatim des handlers d'origine ; gates préservés à l'identique
(list/detail = PLATFORM_ADMIN, écritures = SUPER_ADMIN). Confort MCP : `_resolve_target`
accepte un email OU un sub (côté REST le `{sub}` du path mappe vers `target`).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel

from .. import (access, billing_grants, providers, credentials_store, db,
                group_store, org_store)
from . import _identite
from ._authz import PLATFORM_ADMIN, SUPER_ADMIN
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

_SUB = {"sub": "target"}   # route {sub} (dashboard) → champ Input `target`
_ID = {"id": "org_id"}


def _resolve_target(target: str) -> str:
    """Email → sub (404 si inconnu), sinon sub brut, pour le confort MCP.

    ⚠️ L'ambiguïté est refusée par le seam (`_identite.porteurs_de`) : suspendre
    ou changer le rôle du mauvais homonyme laisse un compte marqué qui
    n'apparaîtra dans AUCUN inventaire — c'est le seul geste de cette classe où
    rien ne se compte et où quelque chose demeure.
    """
    if "@" in target:
        porteurs = _identite.porteurs_de(target)
        if not porteurs:
            raise AuthzDenied(404, "unknown_user", f"Aucun compte avec l'email {target!r}.")
        return porteurs[0]["sub"]
    return target


# ── Input models (seule source de validation) ───────────────────────────────
class UserListInput(BaseModel):
    query: Optional[str] = None   # filtre email/name/sub (MCP) ; absent côté dashboard


class UserGetInput(BaseModel):
    target: str                   # email ou sub


class SetRoleInput(BaseModel):
    target: str
    role: str


class ResetMfaInput(BaseModel):
    target: str


class ResetMfaOutput(BaseModel):
    ok: bool
    sub: str
    removed: list[str]   # types de facteurs retirés (Totp, BackupCode, WebAuthn…) — jamais leur valeur


# ADR 0044 §F R4 : le grant vise le CONNECTEUR (provider) au lieu d'un surrogate key_id
# (la table platform_keys disparaît). L'instance ciblée = la clé plateforme du provider.
class GrantKeyInput(BaseModel):
    target: str
    provider: str
    daily_quota: Optional[int] = None


class RevokeKeyInput(BaseModel):
    target: str
    provider: str


class OrgGrantKeyInput(BaseModel):
    org_id: int
    provider: str
    daily_quota: Optional[int] = None


class OrgRevokeKeyInput(BaseModel):
    org_id: int
    provider: str


class OptionInput(BaseModel):
    entity_type: Literal["user", "org"]
    entity_id: str
    option: str
    on: bool
    # Échéance du don, `YYYY-MM-DD` (fin de journée UTC) ou horodatage ISO. OMIS =
    # l'échéance en place n'est PAS touchée — deux autres surfaces re-posent un don
    # sans rien savoir des dates, et leur geste anodin ne doit pas effacer la
    # borne posée ici. Chaîne VIDE = don perpétuel (efface une échéance).
    expires_at: Optional[str] = None


# ── Handlers (core, (ctx, inp) -> dict) ──────────────────────────────────────
def _list_users(ctx: ResolvedCtx, inp: UserListInput) -> dict:
    users = db.list_users_with_grants()  # inclut les grants pour la matrice users × keys
    for u in users:
        u["effective_role"] = access.get_user_role(u["sub"])  # rôle effectif (OTO_MCP_ADMIN_SUB)
    if inp.query:
        q = inp.query.lower()
        users = [u for u in users
                 if q in (u.get("email") or "").lower()
                 or q in (u.get("name") or "").lower()
                 or q in u["sub"].lower()]
    return {"users": users}


def _user_detail(ctx: ResolvedCtx, inp: UserGetInput) -> dict:
    target = _resolve_target(inp.target)
    u = db.get_user(target)
    if not u:
        raise AuthzDenied(404, "unknown_user", f"Compte {target!r} inconnu.")
    # Contexte PERSISTÉ de la cible (org/équipe maison) — PAS current_org/current_group,
    # qui renverraient le contexte view-as/session du REQUÉRANT admin (fuite vécue
    # 2026-06-24 : la fiche montrait l'option de l'org du requérant, pas de la cible).
    target_org = org_store.get_active_org(target)
    target_group = group_store.get_active_group(target)
    status = access.status_for(target, org=target_org, group=target_group)
    orgs = org_store.list_orgs_for_user(target)
    # Messagerie Unipile PAR ORG (l'option est per-org ; un user peut être dans N orgs) :
    # un bloc par org, option/canaux calculés CONTRE cette org (jamais current_org).
    from ..tools import unipile
    unipile_orgs = unipile.admin_status_by_org(target, orgs)
    return {
        "sub": target, "email": u.get("email"), "name": u.get("name"),
        "role": status["role"], "active_org": status.get("active_org"),
        # L'état de PAUSE, sur la fiche plutôt que sur une surface à part : c'est ici
        # qu'on regarde un compte, et un compte neutralisé dont la fiche n'en dit rien
        # se diagnostique en devinant. `suspended_at` est déjà une chaîne ISO (le
        # driver rend tout en texte) — pas de `.isoformat()` par-dessus.
        "suspended": bool(u.get("suspended_at")),
        "suspended_at": u.get("suspended_at"),
        "suspended_by": u.get("suspended_by"),
        "suspended_reason": u.get("suspended_reason"),
        "orgs": orgs,
        "providers": status["providers"],
        "grants": db.list_grants_for_user(target),
        "option_comps": db.list_option_comps("user", target),  # couche 3 (comp user)
        "unipile_orgs": unipile_orgs,   # état messagerie par org (b)
    }


def _set_role(ctx: ResolvedCtx, inp: SetRoleInput) -> dict:
    if inp.role not in access.ROLES:
        raise AuthzDenied(400, "invalid_role", f"Rôle invalide. Valides : {list(access.ROLES)}.")
    target = _resolve_target(inp.target)
    if not db.get_user(target):
        raise AuthzDenied(404, "unknown_user", f"Compte {target!r} inconnu.")
    db.set_user_role(target, inp.role)
    return {"ok": True, "sub": target, "role": inp.role}


def _reset_mfa(ctx: ResolvedCtx, inp: ResetMfaInput) -> dict:
    target = _resolve_target(inp.target)
    if not db.get_user(target):
        raise AuthzDenied(404, "unknown_user", f"Compte {target!r} inconnu.")
    from ..auth.facade import reset_user_mfa
    removed = reset_user_mfa(target)
    return {"ok": True, "sub": target, "removed": removed}


def _has_platform_instance(provider: str) -> bool:
    return bool(credentials_store.list_platform_instances(provider))


def _grant_key(ctx: ResolvedCtx, inp: GrantKeyInput) -> dict:
    target = _resolve_target(inp.target)
    if not db.get_user(target):
        raise AuthzDenied(404, "unknown_user", f"Compte {target!r} inconnu.")
    if not _has_platform_instance(inp.provider):
        raise AuthzDenied(404, "unknown_key", f"Aucune clé plateforme `{inp.provider}`.")
    dq = max(1, inp.daily_quota) if inp.daily_quota is not None else None
    credentials_store.platform_grant(inp.provider, f"user:{target}", daily_quota=dq)
    return {"ok": True, "sub": target, "provider": inp.provider, "daily_quota": dq}


def _revoke_key(ctx: ResolvedCtx, inp: RevokeKeyInput) -> dict:
    target = _resolve_target(inp.target)
    credentials_store.platform_revoke(inp.provider, f"user:{target}")
    return {"ok": True, "sub": target, "provider": inp.provider}


def _grant_org_key(ctx: ResolvedCtx, inp: OrgGrantKeyInput) -> dict:
    if not org_store.get_org(inp.org_id):
        raise AuthzDenied(404, "unknown_org", f"Org #{inp.org_id} inconnue.")
    if not _has_platform_instance(inp.provider):
        raise AuthzDenied(404, "unknown_key", f"Aucune clé plateforme `{inp.provider}`.")
    dq = max(1, inp.daily_quota) if inp.daily_quota is not None else None
    credentials_store.platform_grant(inp.provider, f"org:{inp.org_id}", daily_quota=dq)
    return {"ok": True, "org_id": inp.org_id, "provider": inp.provider, "daily_quota": dq}


def _revoke_org_key(ctx: ResolvedCtx, inp: OrgRevokeKeyInput) -> dict:
    credentials_store.platform_revoke(inp.provider, f"org:{inp.org_id}")
    return {"ok": True, "org_id": inp.org_id, "provider": inp.provider}


def _parse_expiry(inp: "OptionInput", eid: str) -> object:
    """L'échéance demandée, vers ce qu'attend `db.set_option_comp`.

    Trois valeurs d'entrée, trois sens distincts — et c'est le `None` qui est piégeux,
    d'où l'explicite : `None`/omis = **ne touche pas** à l'échéance en place (les
    surfaces qui re-posent un don ne doivent pas l'effacer sans le savoir) ; `""` =
    don perpétuel ; une date = borne.

    ⚠️ **Refuse de borner le don d'une org hébergée par un tenant tiers.** Poser une
    échéance sur les clients d'un partenaire, c'est décider à sa place de ce qu'il
    leur retire et quand. Le refus est ici, sur l'ÉCRITURE, et pas seulement sur
    l'affichage : une limite de périmètre qui ne vit que dans un écran finit par être
    contournée par la première console qui écrit sans passer par l'écran.
    """
    if inp.expires_at is None:
        return db.KEEP_EXPIRY
    brut = inp.expires_at.strip()
    if not brut:
        return None
    if inp.entity_type == "org" and not billing_grants.org_is_ours(int(eid)):
        raise AuthzDenied(
            409, "partner_org_out_of_scope",
            f"L'org #{eid} est hébergée par un tenant tiers : ses titulaires sont les "
            "clients de ce partenaire. Une échéance ne se pose pas sur eux depuis ici. "
            "Le don lui-même reste posable, sans terme.")
    try:
        # `YYYY-MM-DD` seul = FIN de la journée : « offert jusqu'au 31 octobre » veut
        # dire que le 31 octobre est encore couvert. Minuit couperait un jour trop tôt.
        if len(brut) == 10:
            return datetime.strptime(brut, "%Y-%m-%d").replace(
                hour=23, minute=59, second=59, tzinfo=timezone.utc)
        return datetime.fromisoformat(brut.replace("Z", "+00:00"))
    except ValueError:
        raise AuthzDenied(400, "invalid_body",
                          f"expires_at invalide : {inp.expires_at!r}. Attendu "
                          "'YYYY-MM-DD' ou un horodatage ISO 8601.")


def _set_option(ctx: ResolvedCtx, inp: OptionInput) -> dict:
    eid = str(inp.entity_id)
    if inp.entity_type == "user" and not db.get_user(eid):
        raise AuthzDenied(404, "unknown_user", f"Compte {eid!r} inconnu.")
    if inp.entity_type == "org":
        try:
            if not org_store.get_org(int(eid)):
                raise AuthzDenied(404, "unknown_org", f"Org #{eid} inconnue.")
        except (ValueError, TypeError):
            raise AuthzDenied(400, "invalid_body", "entity_id d'org doit être un entier.")
    if inp.on:
        db.set_option_comp(inp.entity_type, eid, inp.option, granted_by=ctx.sub,
                           expires_at=_parse_expiry(inp, eid))
        key = _compose_platform_grant(ctx, inp, eid)
    else:
        db.clear_option_comp(inp.entity_type, eid, inp.option)
        key = _compose_platform_revoke(inp, eid)
    return {"ok": True, "entity_type": inp.entity_type, "entity_id": eid,
            "option": inp.option, "on": inp.on, "platform_key": key,
            "visible_next_session": _visible_next_session(ctx, inp, eid)}


def _visible_next_session(ctx: ResolvedCtx, inp: "OptionInput", eid: str) -> bool:
    """L'effet de ce grant sur une BOÎTE À OUTILS déjà ouverte : maintenant, ou à la
    prochaine connexion ? (signal #660, 02/09/2026.)

    Une option gouverne des surfaces masquées (`BETA_TOOLS`), et la visibilité d'une
    session se calcule au HANDSHAKE. Poser l'option rendait donc `ok: true` pendant
    que l'outil restait absent de la session — et rien ne permettait de distinguer
    « l'option n'a pas pris » de « elle a pris, la session ne l'a pas encore vue ».
    Un booléen qui NOMME lequel des deux, c'est la moitié du diagnostic qui manquait.

    `False` = la session de l'appelant est concernée, et `refresh_visibility` vient de
    lui repousser sa liste d'outils (best-effort : un hoquet retombe sur la prochaine
    session, ce que `True` décrit de toute façon).
    `True` = le bénéficiaire est un AUTRE compte, ou une org qui n'est pas l'org
    effective de l'appelant : aucune session ouverte ne peut être rafraîchie d'ici, et
    l'outil apparaîtra à la reconnexion du bénéficiaire.
    """
    if not ctx.sub:
        return True
    if inp.entity_type == "user":
        return eid != ctx.sub
    try:
        return int(eid) != access.current_org(ctx.sub)
    except (TypeError, ValueError):
        return True


def _compose_platform_grant(ctx: ResolvedCtx, inp: OptionInput, eid: str) -> Optional[dict]:
    """Composition opérateur (ADR 0024) : « offrir l'option » ne doit pas laisser un
    **état mort**. Comper l'option (couche 3, `has_option`) sans donner la clé
    (couche 2) = `has_option`=true mais aucune clé à résoudre → 404 au `/connect`
    (bouton « Connecter » inerte). Pour un connecteur en mode **plateforme** (revente),
    on accorde donc AUSSI la clé plateforme — le grant = droit d'usage de la clé
    partagée. Les deux couches restent **séparées en base** (orthogonales, ADR 0024) ;
    c'est l'ACTION admin qui les compose. Renvoie un compte-rendu, jamais la clé.

    `None` = l'option n'est pas un connecteur en mode plateforme (rien à composer ;
    p.ex. une option non liée à un connecteur) → comp simple."""
    con = providers.connector_for_provider(inp.option)
    if not con or "platform" not in con.auth_modes:
        return None
    if not credentials_store.list_platform_instances(inp.option):
        # Connecteur revente sans clé plateforme posée → la comp seule resterait un
        # état mort. On le signale au lieu de le masquer (cf. feedback governance UI).
        return {"granted": False, "reason": "no_platform_key",
                "hint": f"Aucune clé plateforme {inp.option!r} posée — pose-la au dashboard "
                        "(/platform/connectors) pour que l'option soit utilisable."}
    if inp.entity_type == "user":
        credentials_store.platform_grant(inp.option, f"user:{eid}")  # ADR 0044 §F R4
        # État d'un TIERS → son org maison, jamais current_org du requérant
        # (seam acteur-scopé ADR 0023 ; scope membre ADR 0033).
        byo = db.has_member_api_key(eid, org_store.get_active_org(eid), inp.option)
    else:
        credentials_store.platform_grant(inp.option, f"org:{eid}")
        byo = org_store.has_org_secret(int(eid), inp.option)
    out = {"granted": True, "provider": inp.option}
    if byo:
        # L'entité a sa propre clé (BYO) → en résolution sa clé prime sur la
        # plateforme, et le gate d'option est court-circuité : l'option ET le
        # grant sont **inertes**. On le dit plutôt que de faire croire à un effet.
        out["byo_inert"] = True
    return out


def _compose_platform_revoke(inp: OptionInput, eid: str) -> Optional[dict]:
    """Symétrique de `_compose_platform_grant` : retirer la comp d'un connecteur en
    mode plateforme retire aussi le(s) grant(s) de sa clé plateforme — « retirer
    l'option » = retirer l'accès revente d'un bloc (couche 2 + couche 3)."""
    con = providers.connector_for_provider(inp.option)
    if not con or "platform" not in con.auth_modes:
        return None
    scope = f"user:{eid}" if inp.entity_type == "user" else f"org:{eid}"
    credentials_store.platform_revoke(inp.option, scope)  # ADR 0044 §F R4
    return {"revoked": True}


CAPABILITIES += [
    Capability(
        key="platform.user.list", handler=_list_users, Input=UserListInput,
        authz=PLATFORM_ADMIN,
        description="[platform admin] List all accounts (with their platform-key grants and "
                    "effective role). Optional `query` filters by email/name/sub substring.",
        rest=RestBinding("GET", "/api/admin/users"),
    ),
    Capability(
        key="platform.user.get", handler=_user_detail, Input=UserGetInput,
        authz=PLATFORM_ADMIN,
        description="[platform admin] Full account fiche by email or sub: identity, effective "
                    "per-provider access, platform-key grants, unlocked namespaces, paid-option comps.",
        rest=RestBinding("GET", "/api/admin/users/{sub}", _SUB),
    ),
    Capability(
        key="platform.user.set_role", handler=_set_role, Input=SetRoleInput,
        authz=SUPER_ADMIN,
        description="[super admin] Set an account's platform role (member|admin|super_admin). "
                    "target = email or sub.",
        rest=RestBinding("POST", "/api/admin/users/{sub}/role", _SUB),
    ),
    Capability(
        key="platform.user.reset_mfa", handler=_reset_mfa, Input=ResetMfaInput,
        Output=ResetMfaOutput, authz=SUPER_ADMIN,
        description="[super admin] Reset a user's MFA — remove ALL their second-factor "
                    "enrollments (authenticator app, backup codes, passkey). Account-recovery "
                    "gesture: use when they've lost their authenticator AND their backup codes, "
                    "with no other way in. Their org's mandatory-MFA policy (if any) still "
                    "applies — they'll be prompted to enroll a fresh factor on next sign-in. "
                    "target = email or sub. Never reveals any factor value, only the types removed.",
        rest=RestBinding("POST", "/api/admin/users/{sub}/reset-mfa", _SUB),
    ),
    Capability(
        key="platform.key.grant", handler=_grant_key, Input=GrantKeyInput,
        authz=SUPER_ADMIN,
        description="[super admin] Grant a platform key (by id) to a user, with an optional "
                    "per-day quota. target = email or sub. Never reveals the key.",
        rest=RestBinding("POST", "/api/admin/users/{sub}/grants/{provider}", _SUB),
    ),
    Capability(
        key="platform.key.revoke", handler=_revoke_key, Input=RevokeKeyInput,
        authz=SUPER_ADMIN,
        description="[super admin] Revoke a user's grant of a connector's platform key.",
        rest=RestBinding("DELETE", "/api/admin/users/{sub}/grants/{provider}", _SUB),
    ),
    Capability(
        key="platform.org.grant_key", handler=_grant_org_key, Input=OrgGrantKeyInput,
        authz=SUPER_ADMIN,
        description="[super admin] Share a connector's platform key with a WHOLE org — every member "
                    "resolves it (metered per-member). Optional per-day quota.",
        rest=RestBinding("POST", "/api/admin/orgs/{id}/grants/{provider}", _ID),
    ),
    Capability(
        key="platform.org.revoke_key", handler=_revoke_org_key, Input=OrgRevokeKeyInput,
        authz=SUPER_ADMIN,
        description="[super admin] Revoke an org's share of a connector's platform key.",
        rest=RestBinding("DELETE", "/api/admin/orgs/{id}/grants/{provider}", _ID),
    ),
    Capability(
        key="platform.option.set", handler=_set_option, Input=OptionInput,
        authz=SUPER_ADMIN, refresh_visibility=True,
        description="[super admin] Grant (on=true) or remove (on=false) a connector option as a FREE "
                    "comp for a user or org (e.g. option='unipile'). Read by access.has_option "
                    "(no billing — option governance is admin-only). entity_type='user'|'org', entity_id=sub|org_id. "
                    "For a platform-mode connector this ALSO grants/revokes its platform key (so "
                    "the option is actually usable, not a dead has_option without a key); the "
                    "`platform_key` field reports what happened (granted / no_platform_key / "
                    "byo_inert / revoked). An option can also GATE tools (e.g. 'beta'), and a tool "
                    "list is computed at handshake: `visible_next_session: true` means the change "
                    "lands on a session other than yours and shows up when its owner reconnects — "
                    "false means your own tool list was just refreshed. Neither is a failure. "
                    "`expires_at` bounds the gift ('YYYY-MM-DD' = end of that day UTC, so the "
                    "date itself is still covered; ISO timestamp also accepted). OMITTED leaves "
                    "any existing deadline UNTOUCHED — re-granting never silently clears one; "
                    "empty string makes the gift open-ended. Past the deadline the option stops "
                    "being granted, the row stays, and clearing the date reopens it. Refused "
                    "with 409 `partner_org_out_of_scope` on an org hosted by a third-party "
                    "tenant: its owners are that partner's customers, not ours.",
        mcp="oto_admin_set_option",
        rest=RestBinding("POST", "/api/admin/option-comps", {}),
    ),
]
