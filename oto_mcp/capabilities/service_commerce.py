"""L'API d'administration du COMMERCE — sous l'identité de service (oto-backend#1069).

Le commerce est un service à part, consommateur de l'API (ADR 0070 §7.4(b)). Il ne lit
ni n'écrit la base du cœur : il passe par ces capacités, REST seules (`mcp=None`, un
tuyau de service, pas un outil d'agent), sous `/api/service/`, gardées par
`COMMERCE_SERVICE` — aucune n'est ouverte à un compte, fût-il super admin.

Ce qu'il y trouve (conception `oto-commerce`, question 4) : les orgs, leurs membres
PAR ANCIENNETÉ avec leur dernière activité (il désigne les membres payants et écrit ses
relances), l'usage par personne sur une fenêtre, et les droits déclarés — de l'org, d'une
personne dans l'org, ou d'une personne dans toutes ses orgs —, qu'il pose, relit et
retire, et la valeur EFFECTIVE d'un droit, telle que la lisent les points d'usage. Il ne
pose jamais de plan ni de prix : le cœur ne sait pas qui paie.

Le service n'a pas d'org (`ResolvedCtx.org_id` est None) : l'org ou la personne visée
vient TOUJOURS du chemin, et son existence est vérifiée ici — jamais déduite d'un état
de session.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from .. import db, entitlements_catalogue as catalogue, group_store, org_store
from ..db import entitlements as db_entitlements
from ._authz import COMMERCE_SERVICE
from ._types import AuthzDenied, Capability, DeclaredError, ResolvedCtx, RestBinding, cap_limit
from .registry import CAPABILITIES

_ID = {"id": "org_id"}

_ORG_INCONNUE = DeclaredError(404, "unknown_org", "l'org n'existe pas ou est archivée")
_COMPTE_INCONNU = DeclaredError(404, "unknown_user", "aucun compte ne porte ce sub")


def _org_ou_404(org_id: int) -> None:
    if not org_store.get_org(org_id) or org_store.is_archived_org(org_id):
        raise AuthzDenied(404, "unknown_org", f"Org #{org_id} inconnue ou archivée.")


def _compte_ou_404(sub: str) -> None:
    if db.get_user(sub) is None:
        raise AuthzDenied(404, "unknown_user", f"Compte {sub!r} inconnu.")


def _iso(v) -> Optional[str]:
    """Une date rendue au service porte TOUJOURS son fuseau (#1073).

    Le store du cœur rend ses dates en TEXTE sans fuseau (« AAAA-MM-JJ HH:MM:SS »,
    `db._conn._str_dict_row`, héritage de SQLite), en UTC puisque la session l'est
    (`TimeZone=UTC`, `_connect_options`). Servies telles quelles, elles sont ambiguës :
    un consommateur ne peut pas les comparer à une date réelle. On les rend en ISO 8601
    avec leur décalage UTC explicite."""
    if v is None:
        return None
    d = v if isinstance(v, datetime) else datetime.fromisoformat(str(v))
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).isoformat()


# ── Les orgs ─────────────────────────────────────────────────────────────────

class ServiceOrgsInput(BaseModel):
    after_id: int = 0
    limit: int = 200

    @field_validator("limit")
    @classmethod
    def _cap(cls, v):
        return cap_limit(v, 1000)


class ServiceOrgRow(BaseModel):
    id: int
    name: str
    created_at: Optional[str] = None
    # Le tenant EFFECTIF de l'org (`db.org_tenant_slug`, union des trois axes) : `oto`
    # pour la nôtre, sinon le slug du tenant tiers qui l'héberge (#1072).
    tenant: str


class ServiceOrgs(BaseModel):
    """Les orgs non archivées d'id > `after_id`, par id croissant. `next_after_id` est
    le curseur de la page suivante, `None` quand la liste est finie.

    ⚠️ Une org dont `tenant` n'est pas `oto` est la cliente d'un partenaire : rien de ce
    qui s'adresse à son titulaire (essai, relance, échéance) ne doit la toucher."""
    orgs: list[ServiceOrgRow]
    next_after_id: Optional[int] = None


def _orgs(ctx: ResolvedCtx, inp: ServiceOrgsInput) -> dict:
    rows = org_store.list_orgs_page(inp.after_id, inp.limit + 1)
    page, suite = rows[:inp.limit], len(rows) > inp.limit
    return {"orgs": [{"id": r["id"], "name": r["name"], "created_at": _iso(r["created_at"]),
                      "tenant": db.org_tenant_slug(r["id"])}
                     for r in page],
            "next_after_id": page[-1]["id"] if suite else None}


# ── Les membres ──────────────────────────────────────────────────────────────

class ServiceOrgInput(BaseModel):
    org_id: int


class ServiceMemberRow(BaseModel):
    sub: str
    email: Optional[str] = None
    org_role: str
    is_active: bool
    joined_at: str
    last_activity_at: Optional[str] = None


class ServiceMembers(BaseModel):
    """Les membres de l'org PAR ANCIENNETÉ (`joined_at`, puis `sub`) — l'ordre dans
    lequel le commerce désigne les membres payants. `last_activity_at` = dernier appel
    d'outil émis sous CETTE org, `None` s'il n'y en a jamais eu."""
    org_id: int
    members: list[ServiceMemberRow]


def _members(ctx: ResolvedCtx, inp: ServiceOrgInput) -> dict:
    _org_ou_404(inp.org_id)
    return {"org_id": inp.org_id,
            "members": [{**r, "joined_at": _iso(r["joined_at"]),
                         "last_activity_at": _iso(r["last_activity_at"])}
                        for r in db.org_members_by_seniority(inp.org_id)]}


# ── Les équipes ──────────────────────────────────────────────────────────────

class ServiceGroupRow(BaseModel):
    id: int
    name: str
    # TOUS les membres de l'équipe — pas seulement ceux dont elle est l'équipe ACTIVE
    # (`is_active`) : une limite de dépense d'équipe est un pot commun de ses membres
    # (`docs/limites-de-depense.md`).
    members: list[str]


class ServiceGroups(BaseModel):
    org_id: int
    groups: list[ServiceGroupRow]


def _groups(ctx: ResolvedCtx, inp: ServiceOrgInput) -> dict:
    _org_ou_404(inp.org_id)
    return {"org_id": inp.org_id, "groups": [
        {"id": g["id"], "name": g["name"],
         "members": [m["sub"] for m in group_store.list_group_members(g["id"])]}
        for g in group_store.list_groups(inp.org_id)]}


# ── L'usage ──────────────────────────────────────────────────────────────────

class ServiceUsageInput(BaseModel):
    org_id: int
    # Fenêtre [since, until), ISO 8601. Omis : du 1er du mois (UTC) à maintenant.
    since: Optional[datetime] = None
    until: Optional[datetime] = None


class ServiceUsageRow(BaseModel):
    sub: str
    calls: int
    platform_calls: int


class ServiceUsage(BaseModel):
    """Par personne, les appels d'outil RÉUSSIS émis sous l'org sur la fenêtre, et
    ceux d'entre eux passés sur une clé de plateforme. Une personne sans appel n'y
    figure pas ; un ancien membre qui a appelé, si."""
    org_id: int
    since: str
    until: str
    by_person: list[ServiceUsageRow]


def _usage(ctx: ResolvedCtx, inp: ServiceUsageInput) -> dict:
    _org_ou_404(inp.org_id)
    until = inp.until or datetime.now(timezone.utc)
    since = inp.since or until.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if since >= until:
        raise AuthzDenied(400, "invalid_window", "`since` doit précéder `until`.")
    return {"org_id": inp.org_id, "since": since.isoformat(), "until": until.isoformat(),
            "by_person": db.org_usage_by_person(inp.org_id, since, until)}


# ── Les droits déclarés ──────────────────────────────────────────────────────

class ServiceEntitlementRow(BaseModel):
    right_key: str
    source: str
    value: int
    sub: Optional[str] = None
    starts_at: Optional[str] = None
    expires_at: Optional[str] = None
    granted_by: Optional[str] = None
    granted_at: Optional[str] = None


class ServiceEntitlements(BaseModel):
    """Toutes les lignes de l'org, portée org (`sub` absent) et personnes, échues et à
    venir comprises : le commerce voit ce qu'il a posé, dates non filtrées."""
    org_id: int
    entitlements: list[ServiceEntitlementRow]


def _ligne(r: dict) -> dict:
    return {k: (_iso(r[k]) if k in ("starts_at", "expires_at", "granted_at") else r[k])
            for k in ServiceEntitlementRow.model_fields}


def _entitlements(ctx: ResolvedCtx, inp: ServiceOrgInput) -> dict:
    _org_ou_404(inp.org_id)
    return {"org_id": inp.org_id,
            "entitlements": [_ligne(r) for r in db_entitlements.list_for_org(inp.org_id)]}


class ServiceEntitlementKey(BaseModel):
    org_id: int
    right_key: str
    source: str
    # La personne visée dans l'org ; omise = le droit vaut pour toute l'org.
    sub: Optional[str] = None


class ServiceEntitlementPutInput(ServiceEntitlementKey):
    value: int
    starts_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None


_SOURCE_INCONNUE = DeclaredError(
    400, "unknown_source", f"`source` hors de la liste fermée ({', '.join(catalogue.SOURCES)})")
_ERREURS_CLE = (
    _ORG_INCONNUE,
    _SOURCE_INCONNUE,
    DeclaredError(404, "not_a_member", "la personne `sub` n'est pas membre de l'org"),
)
_CLE_INCONNUE = DeclaredError(400, "entitlement_unknown_key", "clé hors catalogue")
_ERREURS_POSE = (
    _CLE_INCONNUE,
    DeclaredError(400, "entitlement_value_invalid", "valeur hors du genre de la clé"),
    DeclaredError(400, "invalid_window", "`expires_at` ne suit pas `starts_at`"),
)
_AUCUNE_LIGNE = DeclaredError(404, "unknown_entitlement", "aucune ligne à retirer")


def _source_valide(source: str) -> None:
    if source not in catalogue.SOURCES:
        raise AuthzDenied(400, "unknown_source",
                          f"Source {source!r} inconnue (attendu l'une de "
                          f"{', '.join(catalogue.SOURCES)}).")


def _cle_valide(inp: ServiceEntitlementKey) -> None:
    _org_ou_404(inp.org_id)
    _source_valide(inp.source)
    if inp.sub is not None and org_store.get_org_role(inp.org_id, inp.sub) is None:
        raise AuthzDenied(404, "not_a_member",
                          f"{inp.sub!r} n'est pas membre de l'org #{inp.org_id}.")


def _poser(ctx: ResolvedCtx, org_id: Optional[int], inp) -> dict:
    """La pose commune aux portées (`org_id` None = la personne partout), la clé déjà
    jugée : fenêtre, catalogue, puis `grant`. Rend la ligne telle que stockée."""
    if inp.expires_at and inp.starts_at and inp.expires_at <= inp.starts_at:
        raise AuthzDenied(400, "invalid_window", "`expires_at` doit suivre `starts_at`.")
    # Le catalogue juge AVANT la pose (qui le rejugerait) : chaque refus a son code ici.
    try:
        catalogue.droit(inp.right_key)
    except ValueError as e:
        raise AuthzDenied(400, "entitlement_unknown_key", str(e))
    try:
        catalogue.valeur_valide(inp.right_key, inp.value)
    except ValueError as e:
        raise AuthzDenied(400, "entitlement_value_invalid", str(e))
    db_entitlements.grant(org_id, inp.right_key, inp.source, value=inp.value,
                          sub=inp.sub, starts_at=inp.starts_at, expires_at=inp.expires_at,
                          granted_by=ctx.sub)
    lignes = (db_entitlements.list_for_person_everywhere(inp.sub) if org_id is None
              else db_entitlements.list_for_org(org_id))
    ligne = next(r for r in lignes
                 if (r["right_key"], r["source"], r["sub"]) == (inp.right_key, inp.source, inp.sub))
    return _ligne(ligne)


def _put(ctx: ResolvedCtx, inp: ServiceEntitlementPutInput) -> dict:
    _cle_valide(inp)
    return _poser(ctx, inp.org_id, inp)


def _delete(ctx: ResolvedCtx, inp: ServiceEntitlementKey) -> dict:
    _cle_valide(inp)
    if not db_entitlements.revoke(inp.org_id, inp.right_key, inp.source, sub=inp.sub):
        raise AuthzDenied(404, "unknown_entitlement",
                          f"Aucun droit {inp.right_key!r} de source {inp.source!r} "
                          f"posé sur {'cette personne' if inp.sub else 'cette org'}.")
    return {"ok": True}


# ── Les droits d'une personne dans toutes ses orgs ───────────────────────────
#
# La troisième portée (`org_id` NULL) : un droit qui appartient à la personne quelle
# que soit l'org où elle agit. Chaque ligne se LISTE par UNE seule route : une ligne
# d'org ou de personne dans l'org par `/orgs/{id}/entitlements`, une ligne de personne
# partout par `/users/{sub}/entitlements`.

class ServiceUserEntitlements(BaseModel):
    """Les lignes de la personne PARTOUT (`org_id` nul), échues et à venir comprises —
    ni ses lignes dans une org, ni les lignes d'org (celles de
    `/api/service/orgs/{id}/entitlements`)."""
    sub: str
    entitlements: list[ServiceEntitlementRow]


class ServiceUserEntitlementKey(BaseModel):
    sub: str
    right_key: str
    source: str


class ServiceUserEntitlementPutInput(ServiceUserEntitlementKey):
    value: int
    starts_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None


def _cle_personne_valide(inp: ServiceUserEntitlementKey) -> None:
    _compte_ou_404(inp.sub)
    _source_valide(inp.source)


def _user_entitlements(ctx: ResolvedCtx, inp: ServiceUserInput) -> dict:
    _compte_ou_404(inp.sub)
    return {"sub": inp.sub,
            "entitlements": [_ligne(r) for r in
                             db_entitlements.list_for_person_everywhere(inp.sub)]}


def _user_put(ctx: ResolvedCtx, inp: ServiceUserEntitlementPutInput) -> dict:
    _cle_personne_valide(inp)
    return _poser(ctx, None, inp)


def _user_delete(ctx: ResolvedCtx, inp: ServiceUserEntitlementKey) -> dict:
    _cle_personne_valide(inp)
    if not db_entitlements.revoke(None, inp.right_key, inp.source, sub=inp.sub):
        raise AuthzDenied(404, "unknown_entitlement",
                          f"Aucun droit {inp.right_key!r} de source {inp.source!r} "
                          "posé sur cette personne dans toutes ses orgs.")
    return {"ok": True}


# ── la valeur EFFECTIVE d'un droit (#1096) ───────────────────────────────────
#
# Ce que le cœur APPLIQUE pour (personne, org, droit), et d'où ça vient : de quoi
# prouver, avant le retrait de la lecture héritée, que les lignes d'un producteur
# suffisent. Lecture seule. La valeur sort du MÊME code que les points d'usage
# (`access.valeur_expliquee`, dont `value_for` n'est que la valeur).

class ServiceEffectiveInput(BaseModel):
    sub: str
    right_key: str
    # L'org où la personne agit ; omise = la personne hors de toute org.
    org_id: Optional[int] = None


class ServiceEffectiveRow(BaseModel):
    portee: str = Field(description="org | personne_dans_org | personne_partout")
    source: str
    valeur: int


class ServiceLegacyOptionComps(BaseModel):
    personne: bool = Field(description="Un don vivant posé sur le compte.")
    org: Optional[bool] = Field(None, description="Un don vivant posé sur l'org ; null "
                                                  "hors org.")


class ServiceLegacySeats(BaseModel):
    plafond: int = Field(description="Le plafond que le branchement applique ; `0` = "
                                     "sans plafond (sens hérité).")


class ServiceLegacyRegistry(BaseModel):
    cle_ouverte: bool = Field(description="`platform_key_open` du registre.")
    quota_du_jour: int = Field(description="`quota_for` ; `0` = illimité (sens hérité).")


class ServiceLegacyRead(BaseModel):
    """Ce que rend la lecture HÉRITÉE du droit, UN SEUL champ servi selon la clé :
    `option_comps` (droits oui/non), `plafond_messagerie` (`unipile_seats`, dans une
    org), `registre` (`platform_key:<connecteur>`). ⚠️ Part avec la lecture héritée."""
    option_comps: Optional[ServiceLegacyOptionComps] = None
    plafond_messagerie: Optional[ServiceLegacySeats] = None
    registre: Optional[ServiceLegacyRegistry] = None


class ServiceEffective(BaseModel):
    """`valeur` = ce que lisent les points d'usage pour (personne, org, droit) ; `par`
    = les lignes valides lues ; `defaut` = le défaut d'instance a fourni la valeur de
    l'org ; `lecture_directe` = la lecture héritée, `null` s'il n'y en a pas pour
    cette clé (ou hors org pour `unipile_seats`)."""
    sub: str
    org_id: Optional[int] = None
    right_key: str
    valeur: int
    par: list[ServiceEffectiveRow]
    defaut: bool
    lecture_directe: Optional[ServiceLegacyRead] = None


def _lecture_directe(sub: str, org_id: Optional[int], cle: str) -> Optional[dict]:
    """La lecture HÉRITÉE de `cle` pour la personne dans l'org, ou None s'il n'y en a
    pas. ⚠️ TEMPORAIRE : part avec la lecture héritée (dons `option_comps`, plafond
    de messagerie de l'org, registre des clés de plateforme), valeurs dans LEUR sens
    (`0` « illimité » compris, limite (b))."""
    from .. import access, providers, unipile_connect
    if catalogue.droit(cle).genre is catalogue.Genre.OUI_NON:
        return {"option_comps": {
            "personne": db.has_option_comp("user", sub, cle),
            "org": None if org_id is None else db.has_option_comp("org", str(org_id), cle)}}
    if cle == catalogue.UNIPILE_SEATS:
        if org_id is None:
            return None
        return {"plafond_messagerie": {"plafond": unipile_connect.plafond_de_comptes(org_id)}}
    if cle.startswith(catalogue.PLATFORM_KEY_PREFIX):
        connecteur = cle[len(catalogue.PLATFORM_KEY_PREFIX):]
        return {"registre": {"cle_ouverte": providers.REGISTRY[connecteur].platform_key_open,
                             "quota_du_jour": access.quota_for(connecteur)}}
    return None


def _user_effective(ctx: ResolvedCtx, inp: ServiceEffectiveInput) -> dict:
    _compte_ou_404(inp.sub)
    if inp.org_id is not None:
        _org_ou_404(inp.org_id)
    try:
        catalogue.droit(inp.right_key)
    except ValueError as e:
        raise AuthzDenied(400, "entitlement_unknown_key", str(e))
    from .. import access
    lu = access.valeur_expliquee(inp.sub, inp.org_id, inp.right_key)
    return {"sub": inp.sub, "org_id": inp.org_id, "right_key": inp.right_key,
            "valeur": lu.valeur, "defaut": lu.defaut,
            # Les trois champs du contrat (`ServiceEffectiveRow`), nommés : une ligne
            # lue en porte davantage (son échéance, oto-app#13), que ce contrat ne
            # sert pas.
            "par": [{"portee": l.portee, "source": l.source, "valeur": l.valeur}
                    for l in lu.par],
            "lecture_directe": _lecture_directe(inp.sub, inp.org_id, inp.right_key)}


class ServiceOk(BaseModel):
    ok: bool


# ── l'export pour la reprise (#1085) — TEMPORAIRE ────────────────────────────
#
# L'état de facturation que porte encore le cœur, pour que oto-commerce le reprenne
# par l'API (conception, Q4 : il ne lit pas la base du cœur). Répété d'abord à blanc :
# le commerce dérive les droits de cet état et les compare à ceux que le cœur porte.
# ⚠️ Part avec le code de facturation du cœur (retrait, lot 6).

class ServiceBillingExport(BaseModel):
    """L'état de facturation entier, en un instantané. Dates en ISO 8601 UTC explicite.
    `plans` : la grille des anciens paliers (prix HT mensuel en centimes, droits qu'il
    ouvre), pour qu'un abonné gardé à son prix le reste sans recopie de la grille."""
    plans: dict
    subscriptions: list[dict]
    contracts: list[dict]
    option_comps: list[dict]
    identities: list[dict]
    payments: list[dict]
    invoices: list[dict]
    purchase_acceptances: list[dict]


class ServiceNoInput(BaseModel):
    pass


def _billing_export(ctx: ResolvedCtx, inp: ServiceNoInput) -> dict:
    from .. import billing
    from ..db import billing as db_billing
    return {"plans": {cle: {"amount_ht": p["amount"], "currency": p["currency"],
                            "interval": p["interval"], "rights": list(billing.plan_rights(cle))}
                      for cle, p in billing.PLANS.items()},
            **db_billing.export_commerce()}


# ── le rôle plateforme d'un compte (#1087) ──────────────────────────────────
#
# Le commerce sert une API d'administration plateforme (le bloc « commerce » de la fiche
# d'org, dashboard) ; le rôle plateforme vit ici, dans l'annuaire du cœur. Il le relit à
# chaque requête, jamais ne le copie.

class ServiceUserInput(BaseModel):
    sub: str


class ServiceUser(BaseModel):
    sub: str
    role: str = Field(description="Le rôle PLATEFORME : member | admin | super_admin.")


def _user(ctx: ResolvedCtx, inp: ServiceUserInput) -> dict:
    _compte_ou_404(inp.sub)
    from .. import access
    return {"sub": inp.sub, "role": access.get_user_role(inp.sub)}


_CHEMIN_DROIT = "/api/service/orgs/{id}/entitlements/{right_key}/{source}"
_CHEMIN_DROIT_PERSONNE = "/api/service/users/{sub}/entitlements/{right_key}/{source}"
_CHEMIN_DROIT_PERSONNE_EFFECTIF = "/api/service/users/{sub}/entitlements/{right_key}/effective"

CAPABILITIES += [
    Capability(key="service.orgs.list", handler=_orgs, Input=ServiceOrgsInput,
               authz=COMMERCE_SERVICE, mcp=None, Output=ServiceOrgs,
               description="[service commerce] Non-archived orgs by increasing id, "
                           "paginated by `after_id`, each with its effective `tenant` "
                           "(`oto` = ours; anything else belongs to a partner).",
               rest=RestBinding("GET", "/api/service/orgs")),
    Capability(key="service.org.groups", handler=_groups, Input=ServiceOrgInput,
               authz=COMMERCE_SERVICE, mcp=None, Output=ServiceGroups,
               errors=(_ORG_INCONNUE,),
               description="[service commerce] The org's teams by name, each with every "
                           "member's `sub` (whichever team each has active).",
               rest=RestBinding("GET", "/api/service/orgs/{id}/groups", _ID)),
    Capability(key="service.org.members", handler=_members, Input=ServiceOrgInput,
               authz=COMMERCE_SERVICE, mcp=None, Output=ServiceMembers,
               errors=(_ORG_INCONNUE,),
               description="[service commerce] Members of an org by seniority "
                           "(`joined_at`), with email and last activity under this org.",
               rest=RestBinding("GET", "/api/service/orgs/{id}/members", _ID)),
    Capability(key="service.org.usage", handler=_usage, Input=ServiceUsageInput,
               authz=COMMERCE_SERVICE, mcp=None, Output=ServiceUsage,
               errors=(_ORG_INCONNUE,
                       DeclaredError(400, "invalid_window", "`since` n'est pas avant `until`")),
               description="[service commerce] Successful tool calls per person under an "
                           "org over [since, until) (default: current month, UTC), and "
                           "how many ran on a platform key.",
               rest=RestBinding("GET", "/api/service/orgs/{id}/usage", _ID)),
    Capability(key="service.org.entitlements.list", handler=_entitlements,
               Input=ServiceOrgInput, authz=COMMERCE_SERVICE, mcp=None,
               Output=ServiceEntitlements, errors=(_ORG_INCONNUE,),
               description="[service commerce] Declared entitlements of an org, org-wide "
                           "and per person, expired and future included.",
               rest=RestBinding("GET", "/api/service/orgs/{id}/entitlements", _ID)),
    Capability(key="service.org.entitlement.put", handler=_put,
               Input=ServiceEntitlementPutInput, authz=COMMERCE_SERVICE, mcp=None,
               Output=ServiceEntitlementRow,
               errors=_ERREURS_CLE + _ERREURS_POSE,
               description="[service commerce] Set (idempotent upsert) one entitlement: "
                           "one row per (org, person, right, source). `sub` omitted = the "
                           "whole org. Returns the stored row.",
               rest=RestBinding("PUT", _CHEMIN_DROIT, _ID)),
    Capability(key="service.org.entitlement.delete", handler=_delete,
               Input=ServiceEntitlementKey, authz=COMMERCE_SERVICE, mcp=None,
               Output=ServiceOk,
               errors=_ERREURS_CLE + (_AUCUNE_LIGNE,),
               description="[service commerce] Remove one entitlement row (org, person, "
                           "right, source); other sources and scopes stay.",
               rest=RestBinding("DELETE", _CHEMIN_DROIT, _ID)),
    Capability(key="service.users.get", handler=_user, Input=ServiceUserInput,
               authz=COMMERCE_SERVICE, mcp=None, Output=ServiceUser,
               errors=(_COMPTE_INCONNU,),
               description="[service commerce] The PLATFORM role of an account (member, "
                           "admin, super_admin), re-read on each request.",
               rest=RestBinding("GET", "/api/service/users/{sub}")),
    Capability(key="service.user.entitlements.list", handler=_user_entitlements,
               Input=ServiceUserInput, authz=COMMERCE_SERVICE, mcp=None,
               Output=ServiceUserEntitlements, errors=(_COMPTE_INCONNU,),
               description="[service commerce] Declared entitlements of a person across "
                           "all their orgs (org-independent rows only), expired and "
                           "future included.",
               rest=RestBinding("GET", "/api/service/users/{sub}/entitlements")),
    Capability(key="service.user.entitlement.put", handler=_user_put,
               Input=ServiceUserEntitlementPutInput, authz=COMMERCE_SERVICE, mcp=None,
               Output=ServiceEntitlementRow,
               errors=(_COMPTE_INCONNU, _SOURCE_INCONNUE) + _ERREURS_POSE,
               description="[service commerce] Set (idempotent upsert) one entitlement "
                           "that belongs to a person whatever the org: one row per "
                           "(person, right, source). Returns the stored row.",
               rest=RestBinding("PUT", _CHEMIN_DROIT_PERSONNE)),
    Capability(key="service.user.entitlement.delete", handler=_user_delete,
               Input=ServiceUserEntitlementKey, authz=COMMERCE_SERVICE, mcp=None,
               Output=ServiceOk,
               errors=(_COMPTE_INCONNU, _SOURCE_INCONNUE, _AUCUNE_LIGNE),
               description="[service commerce] Remove one person-wide entitlement row "
                           "(person, right, source); org rows and the person's rows "
                           "within an org stay.",
               rest=RestBinding("DELETE", _CHEMIN_DROIT_PERSONNE)),
    Capability(key="service.user.entitlement.effective", handler=_user_effective,
               Input=ServiceEffectiveInput, authz=COMMERCE_SERVICE, mcp=None,
               Output=ServiceEffective,
               errors=(_COMPTE_INCONNU, _ORG_INCONNUE, _CLE_INCONNUE),
               description="[service commerce] The EFFECTIVE value of one right for a "
                           "person acting in `org_id` (omitted = outside any org), as the "
                           "core's usage points read it, with the valid rows it comes "
                           "from, whether the instance default supplied the org value, "
                           "and the legacy direct read (temporary). Read-only.",
               rest=RestBinding("GET", _CHEMIN_DROIT_PERSONNE_EFFECTIF)),
    Capability(key="service.billing.export", handler=_billing_export, Input=ServiceNoInput,
               authz=COMMERCE_SERVICE, mcp=None, Output=ServiceBillingExport,
               description="[service commerce, temporary] The core's billing state "
                           "(subscriptions, contracts, option comps, identities, payments, "
                           "purchase acceptances, legacy plan grid), for the takeover by "
                           "the commerce service. Removed with the core's billing code.",
               rest=RestBinding("GET", "/api/service/billing/export")),
]
