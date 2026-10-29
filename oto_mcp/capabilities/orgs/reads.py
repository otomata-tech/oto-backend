"""Capacités de lecture du domaine orgs (ADR 0009, barreau 2d).

Pas de divergence d'autz — mais des formes de réponse divergentes (MCP éclaté
vs REST agrégé). On unifie en **superset** : le handler renvoie toutes les clés
que chaque face consommait → ni le dashboard ni les agents MCP ne cassent.

Surfaces asymétriques préservées : `org.get` est REST-only (le MCP n'avait pas
d'agrégat, mais des tools list séparés, conservés MCP-only). `org.get` (membre)
et `org.admin.get` (platform) partagent la fiche, diffèrent par autz+path — et la
fiche admin y ajoute les droits déclarés de l'org (`_org_admin_detail`).
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel

import logging

from ... import access, billing, db, org_store, session_org
from ...org_store import ORG_ROLES
from ...tool_visibility import BETA_OPTION
from .. import droits_admin
from .._authz import ORG_MEMBER_OF, PLATFORM_ADMIN, SUB_ONLY
# Le quota de création vit avec la capacité qui REFUSE (`org.create`) : le lire ici
# plutôt que le recalculer est ce qui garantit que la liste et le refus ne pourront
# jamais annoncer deux nombres différents (#464).
from .core import org_quota
from .._types import Capability, ResolvedCtx, RestBinding

from ..registry import CAPABILITIES

_ID = {"id": "org_id"}


class NoInput(BaseModel):
    pass


class OrgIdInput(BaseModel):
    org_id: int


# Le domaine FERMÉ d'un rôle d'org (validé à l'écriture par `ORG_ROLES`) :
# le contrat le dit, pour qu'un client ne recopie pas l'énuméré à la main (#437).
OrgRole = Literal[ORG_ROLES]

# ── Formes de réponse ────────────────────────────────────────────────────────
# Rappel transverse : tous les horodatages de ces réponses sortent du row factory
# `db/_conn._str_dict_row` en `"YYYY-MM-DD HH:MM:SS"` — **pas** de l'ISO-8601 : ni
# `T`, ni offset (le `tzinfo` est RETIRÉ, pas converti). Ne pas les passer à un
# `Date.parse` qui suppose UTC.

class MyOrgEntry(BaseModel):
    """Une org dont tu es membre. **Superset assumé de deux contrats historiques** :
    `id` == `org_id` et `my_role` == `role` — même valeur, deux noms, aucun second
    concept derrière. Les orgs ARCHIVÉES n'apparaissent jamais ici."""
    id: int
    org_id: int
    name: str
    # Logo EFFECTIF : l'upload s'il existe, SINON une URL logo.dev dérivée du domaine
    # de marque déclaré. Une valeur non-nulle ne prouve donc pas qu'un logo a été
    # téléversé (c'est `logo_custom` de `org.get` qui le dit). None ⟹ monogramme.
    logo_url: Optional[str] = None
    member_count: int
    my_role: OrgRole
    role: OrgRole
    # `true` sur l'org MAISON (le défaut persistant), jamais sur une org « de
    # session » — ADR 0038 a retiré tout état de session côté serveur.
    active: bool
    # Le compte est-il BÊTA dans cette org (option `beta`, seam `access.has_option` :
    # comp user OU comp org OU plan) ? Par ORG et non sur /api/me : le front consulte
    # l'org de l'URL, pas l'org maison. C'est ce qui décide si une surface bêta
    # (`BETA_TOOLS` : le nouvel univers de contenu, `oto_resource_v2`, `oto_function`)
    # se MONTRE — la visibilité MCP masque la liste d'outils, mais un front ne lit pas
    # cette liste ; sans ce champ il ne peut que tout montrer ou rien. ⚠️ Il ne décide
    # PLUS des Agents (flottes, déclencheurs) : ouverts à toute org depuis le 24/09/2026.
    beta: bool = False
    # `true` = l'espace PERSONNEL posé d'office à l'inscription d'un compte
    # (`orgs.personal_of`, même source que `OrgBrief.personal` de la fiche), QUEL QUE
    # SOIT son nom : une org créée à la main et nommée « Perso » reste `false` (#1216).
    # ⚠️ L'étiquette est celle de l'org, pas « la mienne » : si un collègue t'a invité
    # dans son espace personnel, il figure ici avec `true` lui aussi.
    personal: bool


class OrgQuota(BaseModel):
    """Où tu en es du plafond d'espaces CRÉÉS, lisible **avant** de s'y cogner (#464).

    `created` ne compte que ce qui occupe encore une place : ni les espaces archivés
    (l'archivage rend sa place), ni ton espace personnel (posé d'office, non
    supprimable en tant que tel). `created` peut donc être plus petit que le nombre
    d'entrées d'`orgs` ci-dessus — l'espace perso y figure, il est bien à toi.
    ⚠️ Le plafond porte sur les espaces que TU as créés : rejoindre celui d'autrui
    n'en consomme aucun, et une org dont tu es membre sans l'avoir créée n'y entre pas.
    `remaining == 0` ⟹ la prochaine création sera refusée (429 `org_quota`).
    Super_admin, ou admin de ton propre tenant : pas de plafond réel — `cap` vaut
    alors une borne haute fixe (1 000 000), toujours un entier."""
    created: int
    cap: int
    remaining: int


class MyOrgs(BaseModel):
    orgs: list[MyOrgEntry]
    quota: OrgQuota
    # Id de l'org maison. **None est un état atteignable** (aucune ligne
    # d'appartenance active), pas une erreur : le repli est l'espace personnel.
    active_org: Optional[int] = None


class OrgMemberEntry(BaseModel):
    """Un membre de l'org.

    ⚠️ **`active` et `suspended` ne parlent pas de la même chose**, et le premier est
    un faux ami de longue date : `active` dit que CETTE org est l'org MAISON de cette
    personne — un membre parfaitement en état y est `false` dès qu'il travaille par
    défaut ailleurs. C'est `suspended` qui dit que le compte est neutralisé.

    Un membre en pause **reste dans la liste**, marqué. Le retirer serait détruire une
    appartenance qu'on a justement choisi de ne pas détruire, et laisserait un
    administrateur devant des documents signés par quelqu'un qui n'apparaît plus nulle
    part. Il ne compte en revanche dans aucune facture — non pas par décision de ce
    lot, mais parce qu'aucune facture ne compte les membres : l'abonnement est un
    forfait par org (`billing.PLANS`), et le seul compteur de « sièges » du dépôt
    (`db.count_unipile_accounts_for_org`) porte sur des comptes de messagerie."""
    sub: str
    # None quand aucune ligne `users` ne correspond (invité jamais connecté, compte
    # machine) — le membre existe quand même.
    email: Optional[str] = None
    name: Optional[str] = None
    avatar_url: Optional[str] = None
    role: OrgRole
    active: bool
    # Compte mis en pause (`users.suspended_at`). Faux pour tout le monde tant qu'un
    # administrateur n'a pas posé le geste.
    suspended: bool = False
    # Opérateur PLATEFORME (`admin`/`super_admin`, `access.is_operator_role` — même
    # prédicat que celui qui fait refuser une cible opératrice en vue bornée,
    # `api.routes._juger_vue_bornee`, oto#270) : le front s'en sert pour ne PAS
    # proposer « voir en tant que » sur ce membre, un geste que le backend refuserait
    # de toute façon (403 `view_as_hors_org`, ses droits débordant toute org).
    # ⚠️ **Servi au seul org_admin de CETTE org** (et à un opérateur plateforme, déjà
    # au courant) — `None` pour un membre ordinaire : le statut « opérateur » d'un
    # tiers n'est pas une donnée que la fiche org doit annoncer à tout le monde, et
    # seul l'org_admin en a l'usage (le bouton « voir en tant que » qu'il pilote).
    is_platform_operator: Optional[bool] = None


class OrgSecretEntry(BaseModel):
    """Un credential d'org POSÉ — jamais sa valeur : le coffre ne rend aucun secret
    par API. `base_url` est **absent de l'objet** (pas nul) hors connecteur remote."""
    provider: str
    set_by: Optional[str] = None
    set_at: Optional[str] = None
    base_url: Optional[str] = None


class OrgBilling(BaseModel):
    """Abonnement de l'org (ADR 0043). ⚠️ `subscribed: true` couvre **`active` ET
    `past_due`** : un impayé en cours de relance reste « abonné » ici — c'est
    `status` qu'il faut lire pour savoir s'il faut alerter. Sans abonnement, seuls
    `subscribed:false` + `plans` reviennent (tous les autres champs absents)."""
    subscribed: bool
    # Catalogue public des paliers — présent UNIQUEMENT quand il n'y a pas
    # d'abonnement (c'est l'écran « choisir un plan »).
    plans: Optional[list[dict]] = None
    plan: Optional[str] = None
    label: Optional[str] = None
    amount: Optional[int] = None        # centimes
    currency: Optional[str] = None
    interval: Optional[str] = None
    status: Optional[str] = None        # active | past_due | canceled | …
    method: Optional[str] = None
    # 'mollie' (payé sur la plateforme) | 'comp' (offert) | 'contract' (réglé hors
    # plateforme : licences, fin et référence dans `contract`).
    provider: Optional[str] = None
    # `true` = plan FORCÉ par un admin plateforme (offert), aucun paiement derrière
    # et aucune échéance tirée. Ne pas le présenter comme un abonnement payant.
    comp: Optional[bool] = None
    # Abonnement réglé hors plateforme : `seats`, `unit_amount`, `interval`,
    # `starts_at`, `ends_at` (null = reconduction tacite), `reference`.
    contract: Optional[dict] = None
    current_period_end: Optional[str] = None
    next_billing_at: Optional[str] = None
    grace_until: Optional[str] = None
    canceled_at: Optional[str] = None


class OrgBrief(BaseModel):
    """Identité de l'org."""
    id: int
    name: str
    # Cf. `MyOrgEntry.logo_url` : EFFECTIF (upload > logo.dev du domaine).
    logo_url: Optional[str] = None
    # `true` = un logo a réellement été téléversé (gate du bouton « retirer le logo »).
    # C'est la seule clé qui distingue un logo posé d'un logo dérivé du domaine.
    logo_custom: bool
    # Chaîne VIDE quand non renseigné (pas `null`) : le store écrit `""`.
    description: str
    domain: Optional[str] = None
    industry: str
    location: str
    # `true` = espace personnel : non archivable, recréé au boot s'il manque.
    personal: bool
    member_count: int
    # ⚠️ Clé **ABSENTE** (pas nulle) quand l'appelant n'est pas membre — cas réel sur
    # la face `/api/admin/orgs/{id}`, où un admin plateforme lit une org étrangère.
    my_role: Optional[OrgRole] = None
    # Fin d'essai poussée par le commerce (`service.org.trial`), ISO 8601 en UTC
    # explicite, `null` = pas d'essai en cours. Lue seulement : le cœur n'agit pas
    # dessus.
    trial_ends_at: Optional[str] = None


class OrgDetail(BaseModel):
    """Fiche d'org, servie à l'identique aux deux faces (membre et admin plateforme) :
    l'autz change, la forme non."""
    org: OrgBrief
    members: list[OrgMemberEntry]
    secrets: list[OrgSecretEntry]
    # Options payantes OFFERTES à l'org (comp admin), par nom d'option. Une liste vide
    # ne veut pas dire « aucune option » : un plan payant en ouvre sans passer par là.
    option_comps: list[str]
    billing: OrgBilling


class OrgAdminDetail(OrgDetail):
    """La fiche d'org vue par un admin plateforme : celle des membres, plus les DROITS
    déclarés de l'org (oto-app#13). ⚠️ `entitlements` est à côté de `billing`, pas
    dedans : `billing` et `option_comps` sont hérités et partent avec le retrait de la
    facturation du cœur (oto-backend#1104) ; les droits restent."""
    # Toutes les lignes posées DANS l'org — portée org et personnes dans l'org — échues
    # et à venir comprises (une console doit voir un droit échu). Les dons faits à une
    # personne PARTOUT sont sur sa fiche, ou dans `GET /api/admin/entitlements`.
    entitlements: list[droits_admin.EntitlementRow]


def _members(org_id: int, *, exposer_operateur: bool) -> list[dict]:
    """`exposer_operateur` : servir `is_platform_operator` (org_admin ou opérateur
    plateforme qui lit cette fiche) ou le taire (`None`, simple membre — cf. le
    docstring d'`OrgMemberEntry`). ZÉRO requête ajoutée pour ce champ : le rôle
    plateforme (`users.role`) est DÉJÀ dans la ligne, lue pour email/name/avatar — une
    seule requête, `org_store.list_org_members_with_users` (jointure `users`), tient
    toute la liste, là où un `db.get_user` par membre la suivait ; et c'est cette ligne
    qui sert aussi ce verdict, via le MÊME prédicat qu'utilise
    `_juger_vue_bornee` pour refuser une cible opératrice (`access.is_operator_role`,
    équivalent d'`access.is_platform_operator` sur un rôle déjà en main)."""
    out = []
    for m in org_store.list_org_members_with_users(org_id):
        out.append({"sub": m["sub"], "email": m["email"], "name": m["name"],
                    "avatar_url": m["avatar_url"],
                    "role": m["org_role"], "active": m["is_active"],
                    # Gratuit : la ligne `users` vient de la même jointure.
                    "suspended": bool(m["suspended_at"]),
                    "is_platform_operator": (
                        access.is_operator_role(m["sub"], m["user_role"])
                        if exposer_operateur else None)})
    return out


logger = logging.getLogger(__name__)


def _beta_dans(sub: str, org_id: int) -> bool:
    """`beta` par org — fail-CLOSED et TRACÉ. C'est l'appel d'amorçage du front :
    une panne du seam des options ne doit pas blanchir le sélecteur d'espaces,
    et une bêta qui s'ouvrirait sur un hoquet ne se verrait pas."""
    try:
        return access.has_option(sub, BETA_OPTION, org=org_id)
    except Exception:
        logger.warning("beta lookup fail-CLOSED for %s in org %s", sub, org_id,
                       exc_info=True)
        return False


def _list_my_orgs(ctx: ResolvedCtx, inp: NoInput) -> dict:
    orgs, active = [], None
    # Vue bornée à O (oto#270) : la cible n'est vue que dans O — ses autres orgs n'y
    # figurent pas, et O n'est « active » que si c'est bien sa maison.
    borne = session_org.current_view_as_bound_org()
    for o in org_store.list_orgs_for_user(ctx.sub):
        if borne is not None and int(o["org_id"]) != borne:
            continue
        if o["is_active"]:
            active = o["org_id"]
        orgs.append({  # superset REST(id/member_count/my_role) + MCP(org_id/role/active)
            "id": o["org_id"], "org_id": o["org_id"], "name": o["name"],
            # logo EFFECTIF : upload sinon dérivé logo.dev du domaine déclaré.
            "logo_url": org_store.effective_logo_url(o),
            "member_count": len(org_store.list_org_members(o["org_id"])),
            "my_role": o["org_role"], "role": o["org_role"], "active": o["is_active"],
            # `org=` EXPLICITE : calcul contre CETTE org, jamais contre current_org
            # (le seam le prévoit pour la fiche admin — même besoin ici).
            "beta": _beta_dans(ctx.sub, o["org_id"]),
            # Lu dans la même requête que la liste (`list_orgs_for_user`), jamais
            # par org : même étiquette que la fiche (`is_personal_org`).
            "personal": o["personal"],
        })
    # Le quota voyage avec la liste : c'est l'outil par lequel un agent regarde ses
    # espaces, donc le seul endroit où il peut apprendre qu'il approche du mur sans
    # tenter une création pour le découvrir (#464, 2ᵉ demande).
    return {"orgs": orgs, "active_org": active, "quota": org_quota(ctx.sub)}


def _list_all_orgs(ctx: ResolvedCtx, inp: NoInput) -> dict:
    return {"orgs": [
        {**o, "logo_url": org_store.effective_logo_url(o),
         "member_count": len(org_store.list_org_members(o["id"]))}
        for o in org_store.list_all_orgs()
    ]}


def _org_detail(ctx: ResolvedCtx, inp: OrgIdInput) -> dict:
    org = org_store.get_org(inp.org_id)
    if not org:
        from .._types import AuthzDenied
        raise AuthzDenied(404, "unknown_org", f"Org #{inp.org_id} inconnue.")
    my_role = org_store.get_org_role(inp.org_id, ctx.sub)
    # `is_platform_operator` par membre (ci-dessous) : servi à l'org_admin de CETTE
    # org (le seul à agir dessus, « voir en tant que ») et à un opérateur plateforme
    # (déjà au courant, `org.admin.get`) — pas à un simple membre. Une requête, pas
    # une par membre (cf. `_members`).
    exposer_operateur = my_role == "org_admin" or access.is_platform_operator(ctx.sub)
    # La liste des membres est lue UNE fois : elle donne aussi `member_count`.
    membres = _members(inp.org_id, exposer_operateur=exposer_operateur)
    # `logo_url` = EFFECTIF (upload > logo.dev du domaine) ; `logo_custom` dit
    # au front si un upload existe (gate du bouton « remove logo »).
    brief = {"id": org["id"], "name": org["name"],
             "logo_url": org_store.effective_logo_url(org),
             "logo_custom": bool(org.get("logo_url")),
             "description": org.get("description") or "",
             "domain": org.get("domain"),
             "industry": org.get("industry") or "",
             "location": org.get("location") or "",
             # espace perso : non supprimable (gate du bouton « supprimer l'org »).
             "personal": org_store.is_personal_org(org["id"]),
             "member_count": len(membres),
             "trial_ends_at": org_store.get_trial_ends_at(org["id"])}
    if my_role is not None:
        brief["my_role"] = my_role
    return {
        "org": brief,
        "members": membres,
        "secrets": org_store.list_org_secrets(inp.org_id),
        # Options payantes offertes (comp admin) au niveau ORG (couche abonnement).
        "option_comps": db.list_option_comps("org", str(inp.org_id)),
        # Plan/abonnement de l'org (ADR 0043) — pilote le cockpit admin (forcer/
        # retirer un plan comp). `subscribed=False` + `plans` si aucun abonnement.
        "billing": billing.status(inp.org_id),
    }


def _org_admin_detail(ctx: ResolvedCtx, inp: OrgIdInput) -> dict:
    """La fiche d'org, plus ses droits déclarés : servie au SEUL admin plateforme
    (`org.admin.get`, `oto_admin_org op=get`) — la fiche d'un membre n'en dit rien."""
    fiche = _org_detail(ctx, inp)
    return {**fiche, "entitlements": droits_admin.droits_de_l_org(inp.org_id)}


CAPABILITIES += [
    Capability(key="org.list", handler=_list_my_orgs, Input=NoInput, authz=SUB_ONLY,
               Output=MyOrgs,
               description=(
                   "List the organizations you belong to and which one is active. "
                   "`personal: true` marks an automatic personal space, created at "
                   "sign-up for one account, whatever its name: an org someone named "
                   "\"Perso\" by hand stays `personal: false`. It is normally yours; a "
                   "teammate's personal space you were invited into is `true` too."),
               mcp="oto_list_orgs", rest=RestBinding("GET", "/api/me/orgs")),
    # MCP fusionné dans oto_admin_org(op=list). REST conservé (dashboard).
    Capability(key="org.admin.list", handler=_list_all_orgs, Input=NoInput, authz=PLATFORM_ADMIN,
               description="[platform admin] List all organizations.",
               rest=RestBinding("GET", "/api/admin/orgs")),
    # org.get : REST-only, deux faces (membre vs platform), handler partagé.
    Capability(key="org.get", handler=_org_detail, Input=OrgIdInput, authz=ORG_MEMBER_OF("org_id"),
               Output=OrgDetail,
               rest=RestBinding("GET", "/api/orgs/{id}", _ID)),
    Capability(key="org.admin.get", handler=_org_admin_detail, Input=OrgIdInput,
               authz=PLATFORM_ADMIN, Output=OrgAdminDetail,
               rest=RestBinding("GET", "/api/admin/orgs/{id}", _ID)),
    # org.member.list (MCP-only) fusionné dans oto_admin_org_member(op=list).
    # org.secret.list (MCP-only) retiré du MCP (2026-06-25) : le dashboard lit les
    # secrets via la fiche org (org.admin.get → _org_detail). Pose = dashboard-only.
    # org.entitlement.list (MCP-only) fusionné dans oto_admin_namespace_access(op=list, scope=org).
    # La fiche org d'un MEMBRE (`_org_detail`) ne porte PAS de droits : org, members,
    # secrets, option_comps, billing (#437). La fiche ADMIN les ajoute
    # (`_org_admin_detail`, oto-app#13).
]
