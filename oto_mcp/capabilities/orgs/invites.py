"""Capacités d'invitation d'org (onboarding SaaS, ADR 0009).

Émission : une invitation part par mail avec un lien `/invitation/<token>`
(jeton long, 256 bits, seul son hash est persisté) ; sans envoi, l'émetteur
partage ce même lien lui-même. Le code court partageable a existé et a été
RETIRÉ le 15/09/2026 (oto-backend#560 : 7 caractères, ~34 bits, brute-forçable) —
le token long est désormais l'UNIQUE façon d'entrer.

create/list/revoke gatés `ORG_ADMIN_OF` (platform-admin par escalade) ; accept en
`SUB_ONLY` (modèle bearer : le token suffit, cf. `org_store.accept_invitation`).

Deux gestes NÉGATIFS, à ne pas confondre — c'est la distinction que #654 a fait
naître : la **révocation** (`ORG_ADMIN_OF`) est l'émetteur qui retire ce qu'il a
émis, le **refus** (`SUB_ONLY` + adresse confrontée) est l'invité qui dit non.
Jusqu'au 2026-09-01 seul le premier existait : une personne qui ne voulait pas
rejoindre n'avait aucun moyen de le dire, et son invitation restait en attente.
"""
from __future__ import annotations

import logging
import os

from typing import Literal, Optional

from pydantic import BaseModel

from ... import config, db, org_store
from ...auth import annuaire
from ... import email as email_mod  # alias : le param `email` d'emit_invitation masquerait le module
from .._authz import ORG_ADMIN_OF, SUB_ONLY
from .._types import AuthzDenied, Capability, DeclaredError, ResolvedCtx, RestBinding
from ..registry import CAPABILITIES

logger = logging.getLogger(__name__)

_ID = {"id": "org_id"}
_INVITE_TTL_DAYS = int(os.environ.get("OTO_MCP_INVITE_TTL_DAYS", "7"))


def _invite_base(front_base: str | None = None) -> str:
    """Base PUBLIQUE des liens d'invitation partagés (court, marketing).
    `front_base` = le front qui héberge l'org (`orgs.front_base_url`, ex. un tenant tiers) ;
    absent = la base que l'instance DÉCLARE (`config.invite_base_url`, sans défaut)."""
    return front_base.rstrip("/") if front_base else config.invite_base_url()


def _nominal_url(token: str, email_addr: str | None = None, *,
                 front_base: str | None = None) -> str:
    """Lien d'une invitation nominative : `/invitation/<token>`. Augmenté d'un
    magic-link Logto (OTT) quand on connaît l'email invité → connexion sans saisie
    de jeton. Sans email = lien nu, partageable à la main.

    ⚠️ L'OTT est minté sur NOTRE Logto (`LOGTO_ENDPOINT`, un seul global) et
    n'authentifie que contre lui. Une org sous front tiers a son propre émetteur
    (ex. `auth.<tenant>.ai` depuis le 2026-08-03) — un OTT oto y serait inerte,
    soit un échec de connexion silencieux pour l'invité. Donc pas de magic-link dès
    que l'org porte un `front_base_url` : le jeton nu suffit (modèle bearer).
    Le jour de l'étage tenant (ADR 0052), la condition devient « l'émetteur du tenant
    est le nôtre » — dont la présence d'un front tiers n'est ici qu'un proxy."""
    url = f"{_invite_base(front_base)}/invitation/{token}"
    if email_addr and not front_base:
        return annuaire.magic_url(url, email_addr.strip())
    return url


def _same_address(a: str | None, b: str | None) -> bool:
    """Les deux adresses désignent-elles la même boîte (strip + casse) ?

    Comparaison SEULE, jamais de validation : on décide ici d'un droit à partir de
    deux valeurs déjà stockées (l'email de l'invitation, celui du compte), pas de la
    forme d'une saisie — lever un `invalid_email` sur une donnée en base rendrait un
    400 incompréhensible à qui ne fait que cliquer « refuser ». Une adresse absente
    n'égale RIEN, pas même une autre absence : une invitation anonyme n'est adressée
    à personne, donc à personne en particulier."""
    a, b = (a or "").strip().lower(), (b or "").strip().lower()
    return bool(a) and a == b


def _norm_email(raw: str | None, *, required: bool) -> str | None:
    e = (raw or "").strip().lower() or None
    if required and not e:
        raise AuthzDenied(400, "invalid_email", "Email requis pour un envoi par mail.")
    if e is not None and "@" not in e:
        raise AuthzDenied(400, "invalid_email", "Email invalide.")
    return e


# --- Sorties ----------------------------------------------------------------

class InvitationEmitted(BaseModel):
    """Invitation créée. Forme commune aux TROIS niveaux de la cascade
    (plateforme / org / équipe) — d'où le champ `role` unique, qui porte le rôle
    d'équipe quand il y en a un, sinon le rôle d'org.

    ⚠️ **`emailed: false` confond deux causes** : « tu n'as pas demandé d'envoi »
    (`send_email=false`, cas nominal du partage manuel) et « l'envoi a échoué ». Rien
    dans la réponse ne les distingue — un front qui affiche « mail envoyé » sur
    `ok: true` peut mentir. Traiter `emailed: false` comme « à partager soi-même ».

    ⚠️ `invite_url` est le lien **NU** (`/invitation/<token>`) : le token qu'il porte
    EST le secret porteur — le détenir suffit à rejoindre l'org (il n'y a pas de
    vérification que l'accepteur est bien `email`). À traiter comme un jeton, pas
    comme un identifiant. Le magic-link Logto, quand il est minté, ne part que dans
    le MAIL — le partager depuis cette réponse ne transporte donc jamais la
    connexion sans saisie. Et une org rattachée à un front tiers n'en obtient aucun
    (l'OTT serait inerte sur son émetteur).

    Une adresse déjà membre, ou déjà invitée (invitation encore valide), est refusée
    en 409 — `already_member` / `already_invited`, l'invitation existante dans
    `details` (depuis le 29/08/2026, #622 ; avant, un 200 et un code de plus). Une
    invitation expirée, consommée ou révoquée ne bloque pas."""
    ok: bool
    # Email NORMALISÉ (strip + minuscules), ou None quand l'invitation est un simple
    # lien à partager.
    email: Optional[str] = None
    role: str
    invite_url: str
    emailed: bool


# Le palier d'une invitation, DÉRIVÉ par `org_store.invitations._scope_of` (seul
# émetteur, quatre branches) — pas une colonne (#437).
InvitationScope = Literal["platform", "org", "team", "resource"]


class InvitationEntry(BaseModel):
    """Une invitation en attente."""
    id: int
    email: Optional[str] = None
    # NOT NULL en base (défaut `org_member`), lu tel quel par les deux listes (#437) —
    # même sur une invitation d'équipe, où c'est le rôle d'org qu'elle donnera.
    org_role: str
    group_role: Optional[str] = None
    org_id: Optional[int] = None
    group_id: Optional[int] = None
    invited_by: Optional[str] = None
    source: Optional[str] = None
    created_at: Optional[str] = None
    expires_at: Optional[str] = None
    org_name: Optional[str] = None
    group_name: Optional[str] = None
    # Dérivé (team si group_id, sinon org si org_id, sinon platform). Sur CETTE
    # capacité il vaut toujours "org" — les invitations d'équipe en sont exclues.
    scope: InvitationScope


class Invitations(BaseModel):
    """Invitations d'ORG en attente. ⚠️ **Une liste vide ne veut pas dire « personne
    n'a été invité »** : trois populations en sont absentes — les invitations
    d'ÉQUIPE (`group_id` non nul, servies par l'écran équipe), les **acceptées** et
    les **expirées**. Cette vue est une file d'attente, pas un historique."""
    invitations: list[InvitationEntry]


class InvitationRevoked(BaseModel):
    """Révocation. Non idempotente : une invitation inconnue **ou déjà acceptée** rend
    un 404 (`unknown_invitation`) — accepter puis révoquer est impossible, il faut
    retirer le membre. `revoked` réécho l'id demandé."""
    ok: bool
    revoked: int


class InvitationDeclined(BaseModel):
    """Invitation REFUSÉE (oto-backend#654). L'invitation est fermée : elle quitte
    la file de l'émetteur, et plus aucun chemin ne peut la consommer (ni `accept`, ni la reprise automatique au
    signup par l'email).

    ⚠️ **Refuser n'est pas quitter.** Aucune appartenance n'est créée ni retirée : si
    la personne était déjà membre de l'org par ailleurs, elle l'est toujours. Pour
    partir, c'est `DELETE /api/me/orgs/{id}/membership`.

    ⚠️ **`org_id: null` avec `ok: true`** = invitation de PLATEFORME refusée (il n'y
    avait pas d'org à rejoindre) — même piège que sur l'acceptation.

    Le refus n'est **pas définitif pour la personne, seulement pour CETTE
    invitation** : l'émetteur peut en émettre une nouvelle immédiatement (une
    invitation refusée ne déclenche plus le 409 `already_invited` de #622). Il n'est
    en revanche **pas notifié** — la plateforme n'a pas encore de préférences de
    notification ; il voit seulement l'invitation quitter sa file, comme s'il l'avait
    révoquée.

    Re-refuser la même invitation est idempotent (même réponse)."""
    ok: bool
    declined: bool
    scope: InvitationScope
    org_id: Optional[int] = None
    group_id: Optional[int] = None
    name: Optional[str] = None                  # nom de l'org qu'on ne rejoindra pas


class InvitationAccepted(BaseModel):
    """Invitation consommée (modèle bearer : le token SUFFIT, l'identité de
    l'accepteur n'est pas confrontée à l'email invité).

    ⚠️ **`org_id: null` avec `ok: true` est un succès qui ne rejoint rien** : c'est une
    invitation de PLATEFORME (attribution d'onboarding), consommée sans adhésion. Un
    front qui redirige vers `/orgs/{org_id}` construira une URL invalide.

    Quand `org_id` est présent, l'org rejointe devient bien l'org MAISON — `active_org`
    en est l'écho fidèle (`set_active_org` est appelé explicitement), pas une supposition.

    Ré-accepter par le MÊME sub est idempotent (même réponse) ; par un autre, c'est un
    410 `invalid_or_expired`."""
    ok: bool
    org_id: Optional[int] = None
    org_role: Optional[str] = None
    group_id: Optional[int] = None
    group_role: Optional[str] = None
    active_org: Optional[int] = None
    name: Optional[str] = None
    # Partage EN ATTENTE d'UN objet (29/09/2026) : `org_id` null, l'accès à cet
    # objet est donné (`resource_role`), aucune org n'est rejointe ; `name` = le nom
    # de l'objet.
    resource_type: Optional[str] = None
    resource_id: Optional[str] = None
    resource_role: Optional[str] = None
    # L'org d'où l'invité OUVRE cet objet, pour que le front y mène —
    # `/org/<resource_org_id>/apps/<resource_id>` : celle où l'objet vit s'il en est
    # membre, sinon son org active (un objet partagé à une personne se lit par son
    # numéro, d'où qu'elle soit). Null quand aucune n'est connue.
    resource_org_id: Optional[int] = None


# --- Inputs -----------------------------------------------------------------

class InviteCreateInput(BaseModel):
    org_id: int
    email: str | None = None
    role: str = "org_member"
    send_email: bool = True


class InviteListInput(BaseModel):
    org_id: int


class InviteRevokeInput(BaseModel):
    org_id: int
    invite_id: int


class InviteAcceptInput(BaseModel):
    token: str | None = None


class InviteRejectInput(BaseModel):
    """Même façon de désigner l'invitation que l'acceptation — c'est la symétrie
    que le front tiers demandait (#654). Rien de plus : le refus ne prend pas
    d'org_id, l'invitation porte déjà sa cible."""
    token: str | None = None


# --- Émission partagée (cascade plateforme/org/équipe) ----------------------

def emit_invitation(ctx: ResolvedCtx, *, org_id: int | None, email: str | None,
                    send_email: bool, source: str, role: str,
                    target_name: str | None,
                    group_id: int | None = None,
                    group_role: str | None = None) -> dict:
    """Cœur partagé d'émission d'une invitation, commun aux 3 niveaux de la cascade
    (plateforme/org/équipe). Crée la ligne (scope dérivé des cibles), forge le lien
    `/invitation/<token>` et, si demandé, envoie le mail (`target_name` = ce qu'on
    rejoint, None = plateforme → « rejoindre <marque de l'instance> »).

    Le front destinataire (base du lien, marque du mail) est **dérivé de l'org cible**
    (`orgs.front_*`), jamais déclaré par l'appelant : une invitation ne peut pas
    prétendre venir d'un front auquel l'org n'appartient pas, et les 3 niveaux en
    héritent sans rien porter. Sans org (invitation plateforme pure) = la marque
    que l'instance déclare (`email_brand.marque_instance`)."""
    email_addr = _norm_email(email, required=send_email)
    front_base, brand = org_store.org_front(org_id)
    _, token = org_store.create_invitation(
        org_id, email_addr, role, invited_by=ctx.sub, ttl_days=_INVITE_TTL_DAYS,
        source=source, group_id=group_id, group_role=group_role)
    share_url = _nominal_url(token, front_base=front_base)
    emailed = False
    if send_email and email_addr:
        inviter = (db.get_user(ctx.sub) or {}).get("email")
        # Locale du DESTINATAIRE, si connue (oto-backend#700) : un invité qui a
        # déjà un compte oto (ré-invitation, autre org) a peut-être déjà posé sa
        # préférence via `me.locale.set`. Une adresse jamais vue n'a pas encore
        # de ligne `users` ⟹ locale=None ⟹ le gabarit sert la langue que la marque
        # déclare (`OTO_BRAND_LANGUE`, `tenants.brand.langue`), FR à défaut — la
        # détection de langue pour un contact jamais loggé reste hors scope.
        locale = (db.get_user_by_email(email_addr) or {}).get("locale")
        emailed = email_mod.send_invite_email(
            email_addr, target_name, _nominal_url(token, email_addr, front_base=front_base),
            inviter, brand=brand, locale=locale)
    return {"ok": True, "email": email_addr, "role": group_role or role,
            "invite_url": share_url, "emailed": emailed}


# --- Handlers ---------------------------------------------------------------

def _refuse_member_or_invited(org_id: int, email_addr: str | None) -> None:
    """Le refus #622 (29/08/2026), sur l'adresse NORMALISÉE : inviter un membre actuel
    n'a pas de sens, et une deuxième invitation vivante pour la même adresse est un
    deuxième secret porteur. Sans adresse (lien à partager), rien à comparer.
    Membre d'abord : il n'y a rien à renvoyer, la personne est déjà là. Invitée
    ensuite, avec de quoi RENVOYER l'existante — jamais son token, qui suffit à
    rejoindre l'org. Expirée, consommée ou révoquée = plus dans la file = pas un
    doublon."""
    if not email_addr:
        return
    if org_store.get_org_member_by_email(org_id, email_addr):
        raise AuthzDenied(409, "already_member",
                          f"{email_addr} est déjà membre de cette org : rien à inviter.")
    inv = org_store.find_pending_invitation(org_id, email_addr)
    if inv:
        raise AuthzDenied(
            409, "already_invited",
            f"{email_addr} a déjà une invitation en attente (#{inv['id']}, expire le "
            f"{inv['expires_at']}) : la renvoyer ou la révoquer plutôt qu'en émettre "
            "une deuxième.",
            details={"invitation": {"id": inv["id"], "created_at": inv["created_at"],
                                    "expires_at": inv["expires_at"]}})


def _invite_create(ctx: ResolvedCtx, inp: InviteCreateInput) -> dict:
    if inp.role not in org_store.ORG_ROLES:
        raise AuthzDenied(400, "invalid_role", f"Rôle invalide : {inp.role!r}.")
    org = org_store.get_org(inp.org_id)
    if not org:
        raise AuthzDenied(404, "unknown_org", f"Org #{inp.org_id} inconnue.")
    _refuse_member_or_invited(inp.org_id, _norm_email(inp.email, required=inp.send_email))
    return emit_invitation(ctx, org_id=inp.org_id, email=inp.email,
                           send_email=inp.send_email, source="org_admin",
                           role=inp.role, target_name=org["name"])


def _invite_list(ctx: ResolvedCtx, inp: InviteListInput) -> dict:
    return {"invitations": org_store.list_invitations(inp.org_id)}


def _invite_revoke(ctx: ResolvedCtx, inp: InviteRevokeInput) -> dict:
    if not org_store.revoke_invitation(inp.org_id, inp.invite_id):
        raise AuthzDenied(404, "unknown_invitation", "Invitation introuvable ou déjà acceptée.")
    return {"ok": True, "revoked": inp.invite_id}


def _invite_accept(ctx: ResolvedCtx, inp: InviteAcceptInput) -> dict:
    """Accepte une invitation d'org par token mail. Modèle bearer : le secret suffit.

    `active_org` est **lu après l'écriture**, jamais recopié de l'invitation : depuis
    oto#161 accepter ne déplace plus une maison réelle établie, et annoncer l'org
    rejointe comme maison serait un accusé de réception faux — le front s'y fierait
    pour router, l'agent pour supposer où tombent ses appels sans axe."""
    if not inp.token:
        raise AuthzDenied(400, "missing_token", "Aucun token d'invitation fourni.")
    res = org_store.accept_invitation(inp.token, ctx.sub)
    if not res:
        raise AuthzDenied(410, "invalid_or_expired", "Invitation invalide, expirée ou déjà utilisée.")
    if res.get("resource_type"):  # partage en attente d'UN objet : aucune org rejointe
        return {"ok": True, "resource_type": res["resource_type"],
                "resource_id": res.get("resource_id"),
                "resource_role": res.get("resource_role"),
                "resource_org_id": _org_ou_l_ouvrir(ctx.sub, res["resource_type"],
                                                    res.get("resource_id")),
                "name": res.get("resource_name"),
                "active_org": org_store.get_active_org(ctx.sub)}
    org = org_store.get_org(res["org_id"]) if res.get("org_id") else None
    if res.get("org_id"):
        # L'invité par lien a reçu une org perso au premier appel, avant que le jeton
        # n'arrive : vierge, elle est relâchée (une org rejointe suffit de maison).
        # Un échec lève : l'adhésion est faite, mais taire la panne laisserait à
        # l'invité un second espace sans que personne ne l'apprenne.
        org_store.liberer_perso_vierge(ctx.sub, int(res["org_id"]))
    return {"ok": True, "org_id": res.get("org_id"), "org_role": res.get("org_role"),
            "group_id": res.get("group_id"), "group_role": res.get("group_role"),
            "active_org": org_store.get_active_org(ctx.sub),
            "name": org["name"] if org else None}


def _org_de_la_ressource(resource_type: Optional[str], rid) -> Optional[int]:
    """L'org où vit un objet partagé, pour y mener l'invité. Best-effort : None plutôt
    qu'un refus — l'accès est donné, seule l'adresse d'arrivée en dépend."""
    if not resource_type or rid is None:
        return None
    try:
        if resource_type == "app":
            from ..apps_manifeste import org_de_l_app
            row = db.apps.owner(int(rid))
            return org_de_l_app(row) if row else None
        from ... import ownership
        from ..resources_contract import KIND_OF
        org, connue = ownership.org_ou_vit(KIND_OF.get(resource_type, resource_type), str(rid))
        return int(org) if connue and org is not None else None
    except Exception:  # noqa: BLE001 — l'adresse d'arrivée ne casse jamais l'acceptation
        logger.warning("org d'un objet partagé illisible (%s %s)", resource_type, rid,
                       exc_info=True)
        return None


def _org_ou_l_ouvrir(sub: str, resource_type: Optional[str], rid) -> Optional[int]:
    from ... import roles
    org = _org_de_la_ressource(resource_type, rid)
    if org is not None and roles.is_org_member(sub, int(org)):
        return int(org)
    return org_store.get_active_org(sub)


def _invite_reject(ctx: ResolvedCtx, inp: InviteRejectInput) -> dict:
    """Refuse une invitation : elle est fermée sans que personne ne rejoigne rien.

    **Le refus n'est PAS bearer, contrairement à l'acceptation, et c'est délibéré.**
    Accepter avec un secret qu'on détient est un geste sur soi ; refuser avec le même
    secret DÉTRUIT l'invitation d'un tiers — un token partagé par erreur deviendrait
    une porte pour annuler l'onboarding de quelqu'un d'autre, sans appartenance créée
    donc sans trace visible. On exige donc que l'invitation soit ADRESSÉE à l'adresse
    du compte appelant. Ce n'est pas un rétrécissement du besoin : une invitation
    nominative est adressée à CETTE adresse (c'est elle que le mail vise), donc la
    personne invitée, connectée avec elle, passe cette garde.

    Corollaire assumé : une invitation ANONYME (émise sans email, lien à partager
    soi-même) ne se refuse pas — elle n'est adressée à personne, et la retirer est le
    geste de son émetteur (révocation).

    Les gardes sont dans l'ORDRE où elles s'appliquent — c'est un contrat, le premier
    refus qui mord est celui qui est rendu."""
    if not inp.token:
        raise AuthzDenied(400, "missing_token", "Aucun token d'invitation fourni.")
    inv = org_store.peek_invitation(token=inp.token)
    # Inconnue, expirée, déjà acceptée, ou refusée par QUELQU'UN D'AUTRE : même
    # réponse que l'acceptation dans les mêmes cas — il n'y a plus rien à refuser.
    if (inv is None or not inv.get("live") or inv.get("accepted_at") is not None
            or (inv.get("declined_at") is not None and inv.get("declined_sub") != ctx.sub)):
        raise AuthzDenied(410, "invalid_or_expired",
                          "Invitation invalide, expirée ou déjà traitée.")
    if not _same_address(inv.get("email"), (db.get_user(ctx.sub) or {}).get("email")):
        raise AuthzDenied(
            403, "not_the_invitee",
            "Cette invitation ne t'est pas adressée : seule la personne invitée peut "
            "la refuser. L'émetteur, lui, peut la révoquer.")
    # Déjà refusée par moi : idempotent, on ne réécrit pas la date du refus.
    if inv.get("declined_at") is None:
        org_store.mark_invitation_declined(inv["id"], ctx.sub)
    return {"ok": True, "declined": True, "scope": inv["scope"],
            "org_id": inv.get("org_id"), "group_id": inv.get("group_id"),
            "name": inv.get("org_name")}


CAPABILITIES += [
    Capability(
        key="org.invite.create", handler=_invite_create, Input=InviteCreateInput,
        authz=ORG_ADMIN_OF("org_id"), Output=InvitationEmitted,
        description="Invite someone to an org you administer (role: org_member|org_admin). "
                    "send_email=true mails a link; false returns the link to share yourself.",
        rest=(RestBinding("POST", "/api/orgs/{id}/invitations", _ID),
              RestBinding("POST", "/api/admin/orgs/{id}/invitations", _ID)),
        errors=(DeclaredError(409, "already_member",
                              "l'adresse est déjà celle d'un membre de l'org"),
                DeclaredError(409, "already_invited",
                              "l'adresse a déjà une invitation valide (non expirée, non "
                              "consommée, non révoquée) — `details.invitation` = "
                              "{id, created_at, expires_at}, jamais le token")),
    ),
    Capability(
        key="org.invite.list", handler=_invite_list, Input=InviteListInput,
        authz=ORG_ADMIN_OF("org_id"), Output=Invitations,
        description="List pending invitations for an org you administer.",
        rest=RestBinding("GET", "/api/orgs/{id}/invitations", _ID),
    ),
    Capability(
        key="org.invite.revoke", handler=_invite_revoke, Input=InviteRevokeInput,
        authz=ORG_ADMIN_OF("org_id"), Output=InvitationRevoked,
        description="Revoke a pending invitation.",
        rest=RestBinding("DELETE", "/api/orgs/{id}/invitations/{inv}",
                         {"id": "org_id", "inv": "invite_id"}),
    ),
    Capability(
        key="org.invite.accept", handler=_invite_accept, Input=InviteAcceptInput,
        authz=SUB_ONLY, Output=InvitationAccepted,
        description=("Accept an invitation by its mail token. An org/team invitation "
                     "joins the org; a pending SHARE of one resource (`resource_type` "
                     "in the answer) gives access to that resource only, and joins "
                     "nothing."),
        rest=RestBinding("POST", "/api/me/invitations/accept"),
    ),
    Capability(
        key="org.invite.reject", handler=_invite_reject, Input=InviteRejectInput,
        authz=SUB_ONLY, Output=InvitationDeclined,
        # Dans l'ORDRE des gardes du handler.
        errors=(DeclaredError(400, "missing_token",
                              "`token` n'a pas été fourni"),
                DeclaredError(410, "invalid_or_expired",
                              "invitation inconnue, expirée, déjà acceptée, ou déjà "
                              "refusée par quelqu'un d'autre"),
                DeclaredError(403, "not_the_invitee",
                              "l'invitation n'est pas adressée à l'adresse de ton "
                              "compte — ou n'est adressée à personne (invitation "
                              "anonyme) : seule la personne invitée refuse, l'émetteur "
                              "révoque")),
        description=(
            "Decline an org invitation by its mail token — closes it WITHOUT "
            "joining anything. Only the invited address can decline (unlike accept, "
            "which is bearer). Declining never adds or removes a membership: to "
            "leave an org you already belong to, use leave_org."),
        rest=RestBinding("POST", "/api/me/invitations/reject"),
    ),
]
