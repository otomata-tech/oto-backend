"""Autorisation de compte connecteur partagé (otomata-private#55) — surface du
PROPRIÉTAIRE : accorder / révoquer à un user nommé (OU un groupe entier, extension
2026-09) le droit d'opérer SON compte Unipile sur un canal (agence multi-clients,
compte d'org opéré par une équipe, freelance externe). **Cross-org assumé** : le
grantee n'a PAS besoin de partager une org avec le propriétaire — on partage son
PROPRE compte, à qui on veut.

`grantee` = sub OU email d'un user, OU `group:<id>` pour un groupe — auquel cas
TOUS SES MEMBRES ACTUELS opèrent le compte, en fan-out DYNAMIQUE (l'appartenance
est relue en live à chaque appel, jamais une liste figée au grant : rejoindre ou
quitter le groupe change l'accès sans reprêt individuel). L'issue d'origine (#55)
demandait déjà « membres nommés OU un département » — le groupe n'avait jamais été
livré (ADR 0051 l'avait laissé orthogonal au partage d'instance, sans trancher la
cible du grant lui-même).

Deny-by-default, révocation à effet immédiat (le grant est revalidé à chaque appel
dans la résolution, cf. `connector_identities.resolve_operated_account_id`), audité
(`granted_by`/`granted_at`). Autz `SUB_ONLY` : « réservé au propriétaire » est
garanti PAR CONSTRUCTION — `owner_sub := ctx.sub`, jamais accepté d'un param client
(même verrou structurel que l'injection `org_id` des combinateurs). Aucune escalade
org_admin : seul le propriétaire du compte accorde (exigence #55) — vrai pour une
cible groupe comme pour un user nommé.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel

from ... import db, group_store
from .._authz import SUB_ONLY
from .._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from ..registry import CAPABILITIES

Channel = Literal["linkedin", "whatsapp", "telegram", "instagram", "messenger", "twitter"]


def _provider_for(channel: str) -> str:
    """Canal front → provider DB (source unique : `tools/unipile.UNIPILE_CHANNELS`).
    Import paresseux — pas de dépendance module-level capacités → runtime tools."""
    from ...tools.unipile import UNIPILE_CHANNELS
    return UNIPILE_CHANNELS[channel]


def _parse_group_target(grantee: str) -> Optional[int]:
    """`grantee` au format `group:<id>` cible un groupe plutôt qu'un user — même
    permissivité cross-org que le grantee individuel (aucune exigence que le
    propriétaire soit lui-même membre du groupe nommé : il partage SON PROPRE
    compte, à qui/quoi il veut). None si `grantee` n'a pas cette forme."""
    if not grantee.startswith("group:"):
        return None
    raw = grantee[len("group:"):]
    if not raw.isdigit():
        raise AuthzDenied(400, "invalid_group_target",
                          f"Cible de groupe invalide : {grantee!r} (attendu group:<id>)")
    return int(raw)


def _un_seul_porteur(email: str) -> Optional[dict]:
    """La fiche du compte portant cette adresse — ou un REFUS si elle en désigne
    plusieurs. `None` quand personne ne la porte : l'appelant décide (l'octroi
    lève 404, la révocation tolère et retombe sur la chaîne fournie).

    ⚠️ Une adresse ne désigne pas un compte. Deux comptes peuvent la porter — le
    nôtre et celui d'un tenant, ou deux des nôtres (mesuré : dix adresses, vingt
    comptes, dont une paire sans aucun tenant). En choisir un en silence, c'est
    accorder l'accès à son compte connecteur au mauvais destinataire, ou croire
    l'avoir retiré au bon. Le `grantee` accepte DÉJÀ un sub : le refus a donc une
    sortie immédiate, et il la nomme.
    """
    porteurs = db.get_users_by_email(email)
    if len(porteurs) > 1:
        subs = ", ".join(f"`{u['sub']}`" for u in porteurs)
        raise AuthzDenied(
            400, "ambiguous_email",
            f"L'adresse `{email}` désigne {len(porteurs)} comptes : {subs}. "
            "Reprends avec le `sub` de celui que tu vises — `grantee` l'accepte.")
    return porteurs[0] if porteurs else None


def _resolve_grantee(ctx: ResolvedCtx, grantee: str) -> dict:
    """`grantee` = sub OU email → fiche user. Le propriétaire partage SON PROPRE
    compte (owner := ctx.sub par construction) → il peut l'accorder à N'IMPORTE
    QUEL user oto, **y compris hors de ses orgs** (cross-org assumé : agence /
    freelance externe). Seuls garde-fous : l'user doit exister, et pas de
    self-grant (tu opères déjà ton compte)."""
    if "@" in grantee:
        user = _un_seul_porteur(grantee)
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
    grantee: str                         # sub, email, ou `group:<id>` (fan-out live)


class GrantedByMe(BaseModel):
    """Une autorisation que J'AI accordée : « untel — ou tout un groupe — peut
    opérer mon compte sur ce canal ». Exactement un des deux couples
    (`grantee_sub`/`grantee_email`/`grantee_name`) ou (`grantee_group_id`/
    `grantee_group_name`) est renseigné selon la cible du grant."""
    # ⚠️ `provider` n'est PAS le `channel` de l'entrée : c'est le provider DB, en
    # MAJUSCULES (`LINKEDIN`, `WHATSAPP`…). On accorde par `channel=linkedin` et on
    # relit `provider="LINKEDIN"` — un client qui compare les deux tel quel ne
    # matche jamais.
    provider: str
    # État LIVE du compte (LEFT JOIN), pas le snapshot d'audit du grant : `null`
    # si le canal a été déconnecté depuis — le grant existe encore mais est INERTE.
    account_id: Optional[str] = None
    account_name: Optional[str] = None
    grantee_sub: Optional[str] = None
    grantee_email: Optional[str] = None     # null si l'user n'a pas de ligne `users`
    grantee_name: Optional[str] = None
    grantee_group_id: Optional[int] = None
    grantee_group_name: Optional[str] = None
    granted_by: Optional[str] = None
    granted_at: Optional[str] = None
    # DÉRIVÉ de `account_id IS NOT NULL` : `false` = j'ai déconnecté le canal, le
    # grant dort. Ce n'est ni une révocation ni une erreur — reconnecter le
    # ressuscite tel quel.
    active: bool


class GrantedToMe(BaseModel):
    """Une autorisation que J'AI REÇUE : un compte d'autrui que je peux opérer."""
    provider: str                           # provider DB en MAJUSCULES (cf. GrantedByMe)
    owner_sub: str
    owner_email: Optional[str] = None
    owner_name: Optional[str] = None
    account_id: Optional[str] = None        # null = le propriétaire a déconnecté le canal
    account_name: Optional[str] = None
    # L'org sous laquelle le PROPRIÉTAIRE a connecté ce compte — dit d'OÙ vient le
    # partage. Le grant lui-même n'est scopé à aucune org (cross-org assumé) : ce
    # n'est donc pas un filtre d'accès.
    owner_org_id: Optional[int] = None
    owner_org_name: Optional[str] = None
    granted_at: Optional[str] = None
    active: bool
    # None = grant nominatif. Sinon, le groupe dont l'appartenance PORTE cet accès
    # (fan-out dynamique — un départ du groupe le fait disparaître au prochain appel).
    via_group_id: Optional[int] = None
    via_group_name: Optional[str] = None


class AccountGrants(BaseModel):
    """Les deux faces du partage de compte connecteur (#55), du point de vue du
    caller. Deny-by-default : deux listes vides = personne n'opère rien."""
    granted_by_me: list[GrantedByMe]
    granted_to_me: list[GrantedToMe]


class AccountGrantCreated(BaseModel):
    """Écho d'une autorisation accordée. Exactement l'un de `grantee_sub` (cible
    user) ou `grantee_group_id` (cible groupe) est renseigné."""
    ok: bool
    channel: str                            # le canal FRONT tel que passé (minuscules)
    account_id: str                         # le compte visé, snapshot au moment du grant
    grantee_sub: Optional[str] = None       # sub RÉSOLU (l'entrée pouvait être un email)
    grantee_email: Optional[str] = None
    grantee_group_id: Optional[int] = None
    grantee_group_name: Optional[str] = None
    # Limitation documentée, renvoyée telle quelle : le grant autorise, il ne
    # fournit pas la clé. Le(s) bénéficiaire(s) doi(ven)t encore joindre ce compte
    # avec LEUR clé (partagée org/plateforme = OK ; une clé BYO perso ne le voit
    # pas → 404 à l'appel).
    note: str


class AccountGrantRevoked(BaseModel):
    """Écho d'une révocation. Idempotent : `revoked=false` = il n'y avait pas de
    grant à retirer, pas un refus. Exactement l'un de `grantee_sub`/`grantee_group_id`
    est renseigné, selon la cible passée en entrée."""
    ok: bool
    channel: str
    # ⚠️ Écho de l'entrée quand elle n'a pas pu être résolue : un email INCONNU
    # est renvoyé tel quel ici (aucune erreur — le retrait ne fait que ne rien
    # trouver, là où `grant` aurait levé un 404). Un `grantee_sub` contenant un
    # « @ » + `revoked:false` est donc le signe d'une cible mal nommée, pas d'un
    # grant déjà retiré.
    grantee_sub: Optional[str] = None
    grantee_group_id: Optional[int] = None
    revoked: bool


def _list(ctx: ResolvedCtx, inp: AccountGrantsListInput) -> dict:
    return {
        "granted_by_me": (db.list_account_grants_by_owner(ctx.sub)
                          + db.list_account_group_grants_by_owner(ctx.sub)),
        "granted_to_me": db.list_account_grants_to(ctx.sub),
    }


def _grant(ctx: ResolvedCtx, inp: AccountGrantInput) -> dict:
    provider = _provider_for(inp.channel)
    # Scope membre (ADR 0033) : le compte du propriétaire vit dans SON org de
    # contexte — `ctx.org_id` est injecté par SUB_ONLY (= access.current_org).
    account_id = db.get_unipile_account_id(ctx.sub, ctx.org_id, provider)
    if not account_id:
        raise AuthzDenied(404, "channel_not_connected",
                          f"Tu n'as pas de compte {inp.channel} connecté — connecte-le "
                          "d'abord (dashboard, carte du connecteur).")
    group_id = _parse_group_target(inp.grantee)
    if group_id is not None:
        group = group_store.get_group(group_id)
        if not group:
            raise AuthzDenied(404, "unknown_group", f"Groupe inconnu : {inp.grantee}")
        db.set_account_group_grant(ctx.sub, provider, account_id, group_id,
                                   granted_by=ctx.sub)
        return {
            "ok": True, "channel": inp.channel, "account_id": account_id,
            "grantee_group_id": group_id, "grantee_group_name": group["name"],
            "note": "Chaque membre ACTUEL du groupe opère ce compte via le "
                    "sélecteur d'identité (oto_identity op=set) ou un pin de "
                    "projet — l'accès suit l'appartenance au groupe, en live.",
        }
    user = _resolve_grantee(ctx, inp.grantee)
    db.set_account_grant(ctx.sub, provider, account_id, user["sub"], granted_by=ctx.sub)
    return {
        "ok": True, "channel": inp.channel, "account_id": account_id,
        "grantee_sub": user["sub"], "grantee_email": user.get("email"),
        # Limitation documentée : la clé du grantee doit joindre ce compte (clé
        # partagée org/plateforme = OK ; owner sur une clé BYO perso ≠ 404 à l'appel).
        "note": "Le membre autorisé opère ce compte via le sélecteur d'identité "
                "(oto_identity op=set) ou un pin de projet.",
    }


def _revoke(ctx: ResolvedCtx, inp: AccountGrantInput) -> dict:
    provider = _provider_for(inp.channel)
    group_id = _parse_group_target(inp.grantee)
    if group_id is not None:
        revoked = db.clear_account_group_grant(ctx.sub, provider, group_id)
        db.clear_operated_pointers_to_group(ctx.sub, provider, group_id)
        return {"ok": True, "channel": inp.channel, "grantee_group_id": group_id,
                "revoked": revoked}
    if "@" in inp.grantee:
        # ⚠️ La révocation aussi : sur une adresse ambiguë, révoquer « un des deux »
        # laisse l'accès au second — et le propriétaire croit l'avoir retiré. Le
        # refus est donc le même ici que pour l'octroi, pour la raison inverse.
        user = _un_seul_porteur(inp.grantee)
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
        authz=SUB_ONLY, Output=AccountGrants,
        description="List the connector account authorizations you granted (who may operate "
                    "your Unipile accounts, per channel) and those granted to you (accounts "
                    "you may operate). Deny-by-default: no grant = nobody but the owner.",
        rest=RestBinding("GET", "/api/me/connector-accounts/grants"),
    ),
    Capability(
        key="connectors.account_grants.grant", handler=_grant, Input=AccountGrantInput,
        authz=SUB_ONLY, Output=AccountGrantCreated,
        description="[account owner] Authorize an oto user OR a whole group (grantee = email/sub, "
                    "or `group:<id>` for every CURRENT member, dynamically — including someone or "
                    "a group OUTSIDE your orgs, e.g. an external freelancer or agency) to OPERATE "
                    "your connected account on a channel (linkedin, whatsapp, …), acting as you. "
                    "Only the owner can grant; revocable anytime with immediate effect; audited.",
        rest=RestBinding("POST", "/api/me/connector-accounts/{channel}/grants"),
    ),
    Capability(
        key="connectors.account_grants.revoke", handler=_revoke, Input=AccountGrantInput,
        authz=SUB_ONLY, Output=AccountGrantRevoked,
        description="[account owner] Revoke a member's (or a group's, `group:<id>`) authorization "
                    "to operate your account on a channel. Immediate: the next call under your "
                    "identity fails explicitly. Idempotent.",
        rest=RestBinding("DELETE", "/api/me/connector-accounts/{channel}/grants"),
    ),
]
