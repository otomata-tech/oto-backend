"""Autorisation de compte connecteur partagé (otomata-private#55) — surface du
PROPRIÉTAIRE : accorder / révoquer à un user nommé le droit d'opérer SON compte
Unipile sur un canal (agence multi-clients, compte d'org opéré par une équipe,
freelance externe). **Cross-org assumé** : le grantee n'a PAS besoin de partager
une org avec le propriétaire — on partage son PROPRE compte, à qui on veut.

Deny-by-default, révocation à effet immédiat (le grant est revalidé à chaque appel
dans la résolution, cf. `connector_identities.resolve_operated_account_id`), audité
(`granted_by`/`granted_at`). Autz `SUB_ONLY` : « réservé au propriétaire » est
garanti PAR CONSTRUCTION — `owner_sub := ctx.sub`, jamais accepté d'un param client
(même verrou structurel que l'injection `org_id` des combinateurs). Aucune escalade
org_admin : seul le propriétaire du compte accorde (exigence #55).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from .. import db
from ._authz import SUB_ONLY
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

Channel = Literal["linkedin", "whatsapp", "telegram", "instagram", "messenger", "twitter"]


def _provider_for(channel: str) -> str:
    """Canal front → provider DB (source unique : `tools/unipile.UNIPILE_CHANNELS`).
    Import paresseux — pas de dépendance module-level capacités → runtime tools."""
    from ..tools.unipile import UNIPILE_CHANNELS
    return UNIPILE_CHANNELS[channel]


def _resolve_grantee(ctx: ResolvedCtx, grantee: str) -> dict:
    """`grantee` = sub OU email → fiche user. Le propriétaire partage SON PROPRE
    compte (owner := ctx.sub par construction) → il peut l'accorder à N'IMPORTE
    QUEL user oto, **y compris hors de ses orgs** (cross-org assumé : agence /
    freelance externe). Seuls garde-fous : l'user doit exister, et pas de
    self-grant (tu opères déjà ton compte)."""
    if "@" in grantee:
        user = db.get_user_by_email(grantee)
    else:
        user = db.get_user(grantee)
    if not user:
        raise AuthzDenied(404, "unknown_user", f"Utilisateur inconnu : {grantee}")
    if user["sub"] == ctx.sub:
        raise AuthzDenied(400, "self_grant", "Tu opères déjà ton propre compte.")
    return user


class AccountGrantsListInput(BaseModel):
    pass


class AccountGrantInput(BaseModel):
    channel: Channel
    grantee: str                         # sub OU email du membre autorisé


def _list(ctx: ResolvedCtx, inp: AccountGrantsListInput) -> dict:
    return {
        "granted_by_me": db.list_account_grants_by_owner(ctx.sub),
        "granted_to_me": db.list_account_grants_to(ctx.sub),
    }


def _grant(ctx: ResolvedCtx, inp: AccountGrantInput) -> dict:
    provider = _provider_for(inp.channel)
    user = _resolve_grantee(ctx, inp.grantee)
    # Scope membre (ADR 0033) : le compte du propriétaire vit dans SON org de
    # contexte — `ctx.org_id` est injecté par SUB_ONLY (= access.current_org).
    account_id = db.get_unipile_account_id(ctx.sub, ctx.org_id, provider)
    if not account_id:
        raise AuthzDenied(404, "channel_not_connected",
                          f"Tu n'as pas de compte {inp.channel} connecté — connecte-le "
                          "d'abord (dashboard, carte du connecteur).")
    db.set_account_grant(ctx.sub, provider, account_id, user["sub"], granted_by=ctx.sub)
    return {
        "ok": True, "channel": inp.channel, "account_id": account_id,
        "grantee_sub": user["sub"], "grantee_email": user.get("email"),
        # Limitation documentée : la clé du grantee doit joindre ce compte (clé
        # partagée org/plateforme = OK ; owner sur une clé BYO perso ≠ 404 à l'appel).
        "note": "Le membre autorisé opère ce compte via le sélecteur d'identité "
                "(oto_set_connector_identity) ou un pin de projet.",
    }


def _revoke(ctx: ResolvedCtx, inp: AccountGrantInput) -> dict:
    provider = _provider_for(inp.channel)
    if "@" in inp.grantee:
        user = db.get_user_by_email(inp.grantee)
        grantee_sub = user["sub"] if user else inp.grantee
    else:
        grantee_sub = inp.grantee
    revoked = db.clear_account_grant(ctx.sub, provider, grantee_sub)
    # Hygiène : efface le pointeur du grantee s'il opérait ce compte. Le backstop
    # ne repose PAS dessus (grant re-checké à chaque appel).
    db.clear_operated_pointers_to(ctx.sub, provider, grantee_sub)
    return {"ok": True, "channel": inp.channel, "grantee_sub": grantee_sub,
            "revoked": revoked}


CAPABILITIES += [
    Capability(
        key="connectors.account_grants.list", handler=_list, Input=AccountGrantsListInput,
        authz=SUB_ONLY,
        description="List the connector account authorizations you granted (who may operate "
                    "your Unipile accounts, per channel) and those granted to you (accounts "
                    "you may operate). Deny-by-default: no grant = nobody but the owner.",
        mcp="oto_list_account_grants",
        rest=RestBinding("GET", "/api/me/connector-accounts/grants"),
    ),
    Capability(
        key="connectors.account_grants.grant", handler=_grant, Input=AccountGrantInput,
        authz=SUB_ONLY,
        description="[account owner] Authorize any oto user (grantee = email or sub — including "
                    "someone OUTSIDE your orgs, e.g. an external freelancer or agency) to OPERATE "
                    "your connected account on a channel (linkedin, whatsapp, …), acting as you. "
                    "Only the owner can grant; revocable anytime with immediate effect; audited.",
        mcp="oto_grant_account_access",
        rest=RestBinding("POST", "/api/me/connector-accounts/{channel}/grants"),
    ),
    Capability(
        key="connectors.account_grants.revoke", handler=_revoke, Input=AccountGrantInput,
        authz=SUB_ONLY,
        description="[account owner] Revoke a member's authorization to operate your account "
                    "on a channel. Immediate: their next call under your identity fails "
                    "explicitly. Idempotent.",
        mcp="oto_revoke_account_access",
        rest=RestBinding("DELETE", "/api/me/connector-accounts/{channel}/grants"),
    ),
]
