"""Les règles d'autz de la couche capacité (ADR 0009 §7) — liste FERMÉE.

Chaque règle prend `(RawCtx, input)` et renvoie un `ResolvedCtx`, ou lève
`AuthzDenied` (neutre). Elles **réutilisent** la logique d'autz existante
(`access`, `org_store`, et le résolveur de hiérarchie `roles`) — source unique,
pas de duplication.

L'escalade descendante (platform_admin > org_admin > group_admin > member) est
portée par `roles.py` (ADR 0012), pas recopiée ici : `ORG_ADMIN_OF`,
`GROUP_ADMIN_OF`, etc. délèguent au résolveur central. Ajouter un palier = un
seul endroit.

Depuis le retrait du transport stdio (2026-06-13) le serveur est toujours
authentifié : plus de branche `sub is None` → accès complet. `sub` absent = refus.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict

from .. import access, db, group_store, roles, tenancy
from ._types import AuthzDenied, RawCtx, ResolvedCtx


def _refus_org_admin(org_id, autres: Optional[str] = None) -> AuthzDenied:
    """LE refus « il faut être administrateur de cette org » — une seule phrase.

    Il s'écrivait à quatre endroits sous **deux** formulations : « de ton org active »
    et « de l'org #N ». Même mur, deux phrases — et un appelant en déduit deux causes.
    Relevé au journal (#681) : entre deux refus, un appelant a changé deux fois de
    stratégie en dix minutes, croyant que le second refus disait autre chose que le
    premier. Il cherchait, lui, un palier d'écriture autre que l'org.

    La phrase NOMME l'org : « ton org active » oblige à deviner laquelle, et c'est
    précisément l'ambiguïté qui fait retenter au lieu de comprendre.

    ⚠️ **`autres` répare ce que la phrase ci-dessus laissait entier** (04/09/2026).
    Elle disait QUI a le droit, jamais QUEL AUTRE CHEMIN existe — et c'est le second
    qui manque à celui qui la lit. Mesuré sur un cas réel : une membre d'org a tenté
    d'écrire une procédure le 31/08, réessayé le 02/09, et n'a trouvé le palier ÉQUIPE
    que le 04/09 — quatre jours, trois refus, pour un geste qui lui était ouvert depuis
    le début. Le défaut est décrit mot pour mot dans le paragraphe précédent, écrit
    lors du lot qui l'a relevé : on avait unifié la phrase sans lui donner d'issue.

    Un refus qui ne porte pas le geste n'arrête pas la demande, il la déplace.
    """
    phrase = f"Réservé à un administrateur de l'org #{org_id}."
    return AuthzDenied(403, "forbidden", phrase + (f" {autres}" if autres else ""))


#: Le refus d'une vue « en tant que » BORNÉE à une org (oto#270) : un org_admin qui
#: consulte un membre ne voit que ce que ce membre voit dans l'org O. Un seul code pour
#: le middleware (`api/routes.py`), l'adaptateur REST et les listes « compte entier ».
CODE_HORS_VUE = "view_as_hors_org"


def refus_hors_vue(quoi: str) -> AuthzDenied:
    """LE refus d'une lecture qui sortirait de l'org d'une vue bornée — il la nomme."""
    from .. import session_org
    borne = session_org.current_view_as_bound_org()
    return AuthzDenied(403, CODE_HORS_VUE,
                       f"Vue « en tant que » bornée à l'org #{borne} : {quoi} sort de "
                       "cette org, la lecture est refusée.")


def _require_sub(raw: RawCtx) -> str:
    if not raw.sub:
        raise AuthzDenied(401, "auth_required", "Authentification requise.")
    # Une identité de SERVICE n'est pas un compte : toute règle qui en exige un la
    # refuse, et seule `SERVICE_ROLE` la lit. Second verrou après `allow_service`.
    from ..auth import service_identity
    if service_identity.current() is not None:
        raise AuthzDenied(403, "service_forbidden",
                          "Ce geste demande un compte, pas une identité de service.")
    return raw.sub


# ── Plancher de rôle PLATEFORME d'une règle ──────────────────────────────────
#
# Ce que la couche capacité déclare, c'est QUI a le droit. La visibilité de session,
# elle, veut savoir autre chose : « existe-t-il un appel que ce user pourrait faire
# aboutir ? » — sinon montrer l'outil ne fait qu'alourdir son contexte. Les deux
# questions ont la même source, et une seule : la règle d'autz. Le nom d'un outil,
# lui, ne porte aucun droit (`oto_admin_org_member` accorde `remove` à un org_admin).
#
# Trois crans ORDONNÉS, et rien d'autre : `None` = rien à voir avec le rôle plateforme
# (l'accès dépend de la CIBLE — une org, une équipe, une ressource — que le handshake
# ne connaît pas), `operator` = `PLATFORM_ADMIN`, `super` = `SUPER_ADMIN`.
#
# ⚠️ Le défaut est `None`, donc **fail-open côté visibilité** : une règle future qui
# oublie de se déclarer rend son outil visible, jamais appelable — l'autz continue de
# refuser à l'appel. C'est le bon sens du fail : ADR 0031 dit que la visibilité est une
# gouvernance, pas une barrière, et l'erreur coûteuse est de CACHER un geste légitime
# (c'est celle qu'on répare ici), pas d'en montrer un de trop.
PLATFORM_FLOORS: tuple[Optional[str], ...] = (None, "operator", "super")

_RANG_PAR_ROLE_PLATEFORME = {"member": 0, "admin": 1, "super_admin": 2}


def platform_floor(rule) -> Optional[str]:
    """Le cran de rôle plateforme SANS LEQUEL aucun appel de `rule` ne peut aboutir."""
    return getattr(rule, "platform_floor", None)


def _lowest_floor(rules) -> Optional[str]:
    """Plancher d'un combinateur = le plus BAS de ses branches.

    C'est l'exigence pour que l'outil serve **au moins une fois** — pas pour qu'il
    serve partout. `admin.org_member` réunit quatre ops `ORG_ADMIN_OF` et un `list`
    plateforme : son plancher est donc `None`, et son `op=list` continue de répondre
    403 à un org_admin. Prendre le plus HAUT re-créerait exactement le défaut réparé :
    un outil caché à cause d'une op qui n'est pas celle qu'on voulait faire."""
    floors = [platform_floor(r) for r in rules]
    return min(floors, key=PLATFORM_FLOORS.index) if floors else None


def meets_platform_floor(floor: Optional[str], role_plateforme: str) -> bool:
    """Ce rôle plateforme atteint-il ce plancher ? Un rôle inconnu vaut `member`."""
    return (_RANG_PAR_ROLE_PLATEFORME.get(role_plateforme, 0)
            >= PLATFORM_FLOORS.index(floor))


def SUB_ONLY(raw: RawCtx, inp: Optional[BaseModel] = None) -> ResolvedCtx:
    """Tout user authentifié (datastore, méta user-tools, oto_use_org)."""
    sub = _require_sub(raw)
    return ResolvedCtx(sub=sub, org_id=access.current_org(sub),
                       role=access.get_user_role(sub))


def PROJECT_SHARED_READ(raw: RawCtx, inp: Optional[BaseModel] = None) -> ResolvedCtx:
    """`SUB_ONLY`, PLUS le destinataire d'un projet publié sans login (ADR 0032).

    L'en-tête de ce module dit « plus de branche `sub is None` » — vrai du transport,
    faux depuis les endpoints de projet publiés, arrivés après. Le cas n'avait été
    traité que pour le datastore, d'où un `oto_doc` listé sur un endpoint partagé et
    refusé à 100 % des appels (feedback #310).

    Sans `sub` : on n'accepte QUE le contexte anonyme d'un projet dont le propriétaire
    a explicitement exposé les pages (`mcp_expose_docs`), et le `ResolvedCtx` porte le
    projet — c'est le handler qui borne ensuite les ops à la lecture ET le périmètre à
    CE projet. Aucun opt-in ⇒ refus normal. Fail-closed de bout en bout.
    """
    if raw.sub:
        return SUB_ONLY(raw, inp)
    from .. import subdomain_project
    pid = subdomain_project.current_anon_project_id()
    if pid is None or not subdomain_project.current_anon_docs_exposed():
        raise AuthzDenied(401, "auth_required", "Authentification requise.")
    return ResolvedCtx(sub=None, org_id=subdomain_project.current_anon_org(), role=None)


def SERVICE_ROLE(role: str):
    """Une identité de SERVICE portant `role` (oto-backend#1068) — jamais un compte,
    fût-il super admin, et réciproquement un service ne passe aucune autre règle.

    Le service est reconnu par ce que l'authentification REST a POSÉ après avoir
    vérifié son jeton (`auth.service_identity`), jamais par la forme de son `sub`.
    Il n'a pas d'org : `org_id=None` est un fait, la cible vient de l'input."""
    from ..auth import service_identity
    if role not in service_identity.ROLES:
        raise ValueError(f"rôle de service inconnu : {role!r}")

    def rule(raw: RawCtx, inp: Optional[BaseModel] = None) -> ResolvedCtx:
        p = service_identity.current()
        if p is None:
            raise AuthzDenied(403, "service_required",
                              f"Réservé à l'identité de service « {role} ».")
        if raw.sub != p["sub"]:
            raise AuthzDenied(401, "service_identity_mismatch",
                              "le principal authentifié n'est pas le service posé.")
        if role not in p["roles"]:
            raise AuthzDenied(403, "service_role_missing",
                              f"Ce service ne porte pas le rôle « {role} ».")
        return ResolvedCtx(sub=p["sub"], org_id=None, role=f"service:{role}")
    # Lu par l'adaptateur REST pour ouvrir l'authentification au service.
    rule.accepts_service = True
    return rule


COMMERCE_SERVICE = SERVICE_ROLE("commerce")


def accepts_service(rule) -> bool:
    """Au moins une branche de `rule` lit une identité de service (`BY_OP` expose
    ses branches ; `ADMIN_BY_OP` ne les expose pas et n'en accepte donc pas)."""
    if getattr(rule, "accepts_service", False):
        return True
    return any(accepts_service(r) for r in getattr(rule, "autz_branches", ()))


def ORG_MEMBER(raw: RawCtx, inp: Optional[BaseModel] = None) -> ResolvedCtx:
    """Membre d'une org active — injecte `org_id` depuis l'état serveur (jamais
    d'un param client). Verrouille l'IDOR cross-org par construction."""
    sub = _require_sub(raw)
    org_id = access.current_org(sub)
    if org_id is None:
        raise AuthzDenied(400, "no_active_org",
                          "Aucune org active — choisis-en une avec oto_use_org.")
    return ResolvedCtx(sub=sub, org_id=org_id, role=access.get_user_role(sub))


def WORKER_OR_ORG_MEMBER(raw: RawCtx, inp: Optional[BaseModel] = None) -> ResolvedCtx:
    """Un worker de PLATEFORME — ou un membre d'org (`ORG_MEMBER`, inchangé).

    Le worker est reconnu par ce que l'authentification REST a POSÉ après avoir
    vérifié son secret de machine en base (`auth.platform_worker`), jamais par
    une marque sur un compte ni par la forme de son `sub`. Il n'a pas d'org, et
    `org_id=None` est ici un fait, pas un manque : c'est le backend qui choisit,
    à chaque sondage, la campagne de n'importe quelle org à lui servir.

    ⚠️ Le contexte ne peut pas être posé côté MCP (seule la face REST vérifie
    un `otow_`) : par là, cette règle EST `ORG_MEMBER`.
    """
    from ..auth import platform_worker
    w = platform_worker.current()
    if w is not None:
        if raw.sub != w["worker_sub"]:
            # Deux vérités pour une requête : on ne choisit pas, on refuse.
            raise AuthzDenied(401, "worker_identity_mismatch",
                              "le principal authentifié n'est pas le worker posé.")
        return ResolvedCtx(sub=w["worker_sub"], org_id=None,
                           role="platform_worker", platform_worker=True)
    return ORG_MEMBER(raw, inp)


def ORG_ADMIN(raw: RawCtx, inp: Optional[BaseModel] = None) -> ResolvedCtx:
    """Org-admin de l'org ACTIVE — écriture self-service scopée à l'org active
    (miroir écriture d'`ORG_MEMBER`). `org_id` injecté depuis l'état serveur, jamais
    d'un param client. Escalade super_admin via `roles.is_org_admin` (parité exacte
    avec le legacy `_resolve_org_write`/`_active_org_edit` : seul le super escalade)."""
    sub = _require_sub(raw)
    org_id = access.current_org(sub)
    if org_id is None:
        raise AuthzDenied(400, "no_active_org",
                          "Aucune org active — choisis-en une avec oto_use_org.")
    if not roles.is_org_admin(sub, org_id):
        raise _refus_org_admin(org_id)
    return ResolvedCtx(sub=sub, org_id=org_id, role=access.get_user_role(sub))


def PLATFORM_ADMIN(raw: RawCtx, inp: Optional[BaseModel] = None) -> ResolvedCtx:
    """Admin opérationnel (admin ou super_admin) — supervision plateforme sans
    l'escalade en masse vers les orgs tierces (réservée à SUPER_ADMIN)."""
    sub = _require_sub(raw)
    if not access.is_platform_operator(sub):
        raise AuthzDenied(403, "forbidden", "Réservé à un admin plateforme.")
    return ResolvedCtx(sub=sub, org_id=access.current_org(sub),
                       role=access.get_user_role(sub))


def SUPER_ADMIN(raw: RawCtx, inp: Optional[BaseModel] = None) -> ResolvedCtx:
    """Super admin uniquement — le tout-puissant (rôles plateforme, keys, tokens,
    écriture sur orgs tierces, création d'org, entitlements)."""
    sub = _require_sub(raw)
    if not access.is_super_admin(sub):
        raise AuthzDenied(403, "forbidden", "Réservé au super admin.")
    return ResolvedCtx(sub=sub, org_id=access.current_org(sub),
                       role=access.get_user_role(sub))


def _refus_publication_bibliotheque() -> AuthzDenied:
    """LE refus « publier dans la bibliothèque publique est réservé à la plateforme »
    — une seule phrase, que lèvent la règle `LIBRARY_PUBLISHER` et le filet de
    `guide_library._publish` : le même mur ne se dit pas de deux façons.

    Il dit ce qui reste ouvert ET à qui : les procédures personnelles le sont à tout
    compte, le fork demande org_admin. Promettre le fork sans sa condition enverrait
    un simple membre vers un second refus (#632 : le fait, puis au plus une
    condition)."""
    return AuthzDenied(
        403, "publication_reservee_a_la_plateforme",
        "Publier dans la bibliothèque publique est réservé aux super-administrateurs de "
        "la plateforme : c'est une vitrine éditée par la plateforme. Ce qui reste "
        "ouvert : tes procédures personnelles, et — si tu es org_admin — forker une "
        "entrée de la bibliothèque dans ton org.")


def LIBRARY_PUBLISHER(raw: RawCtx, inp: Optional[BaseModel] = None) -> ResolvedCtx:
    """Publier dans la bibliothèque publique de guides : un super_admin plateforme,
    depuis une org active.

    La bibliothèque publique est une vitrine éditée par la plateforme — ses entrées
    sont signées Otomata. Le rôle se vérifie dans la règle que la capacité DÉCLARE,
    pas seulement dans le handler : c'est la règle que lisent `platform_floor` et
    `capacite_autorise` pour annoncer le droit, et un droit annoncé par une autre
    condition que celle qui refuse finit par dériver en silence (#695).

    Ordre des refus : le rôle AVANT l'org active. Quand deux refus se disputent une
    entrée, celui qui n'ouvre aucune porte parle en premier (#632) — dire « choisis
    une org » à qui ne peut pas publier ne mènerait qu'à ce même refus, une étape plus
    loin. L'org reste requise pour le super_admin : le corps publié se lit dans une
    procédure de l'org active, injectée depuis l'état serveur comme `ORG_MEMBER`
    (jamais d'un param client)."""
    sub = _require_sub(raw)
    if not access.is_super_admin(sub):
        raise _refus_publication_bibliotheque()
    org_id = access.current_org(sub)
    if org_id is None:
        raise AuthzDenied(400, "no_active_org",
                          "Aucune org active — choisis-en une avec oto_use_org.")
    return ResolvedCtx(sub=sub, org_id=org_id, role=access.get_user_role(sub))


# Les règles dont AUCUN appel n'aboutit sans un rôle plateforme : elles se déclarent
# ici, à côté de leur définition, pour qu'un changement de garde et son plancher ne
# puissent pas partir chacun de leur côté. `LIBRARY_PUBLISHER` exige en plus une org
# active, mais son plancher de rôle reste `super`.
PLATFORM_ADMIN.platform_floor = "operator"
SUPER_ADMIN.platform_floor = "super"
LIBRARY_PUBLISHER.platform_floor = "super"


def ADMIN_BY_OP(by_op: dict, *, field: str = "op"):
    """Autz **op-aware** : choisit la règle d'autz selon `input.<field>` (typiquement
    `op`). Permet à un outil consolidé `*_op` de réunir des verbes à paliers d'autz
    différents (ex. lecture `PLATFORM_ADMIN`, mutation `SUPER_ADMIN`) **sans** redescendre
    l'autz dans le handler : l'autz reste DÉCLARÉE au niveau de la capacité, juste
    paramétrée par op (esprit ADR 0009 §7 préservé — pas de drift, pas d'oubli). Chaque
    branche est une règle fermée de ce module ; un op hors map = refus net (jamais
    fail-open). La validation de `op` reste portée par le `Literal` de l'Input."""
    def rule(raw: RawCtx, inp: Optional[BaseModel] = None) -> ResolvedCtx:
        op = getattr(inp, field, None) if inp is not None else None
        chosen = by_op.get(op)
        if chosen is None:
            raise AuthzDenied(400, "unsupported_op",
                              f"op `{op}` non supporté (attendu : {sorted(by_op)}).")
        return chosen(raw, inp)
    rule.platform_floor = _lowest_floor(by_op.values())
    return rule


def BY_OP(rules: dict, *, fields: tuple[str, ...] = ("op",)):
    """Généralisation d'`ADMIN_BY_OP` (ADR 0047) : la règle d'autz est choisie par la
    valeur COMBINÉE de `input.<fields>` — clé simple pour un champ (`op`), tuple pour
    plusieurs (ex. `(op, scope)` sur la console connecteurs, où `list` org-scopée est
    membre mais `set` est admin, et le palier diffère encore entre org et équipe).
    Même contrat : chaque branche est une règle fermée, clé hors map = refus net."""
    def rule(raw: RawCtx, inp: Optional[BaseModel] = None) -> ResolvedCtx:
        vals = tuple(getattr(inp, f, None) for f in fields) if inp is not None else ()
        key = vals[0] if len(vals) == 1 else vals
        chosen = rules.get(key)
        if chosen is None:
            raise AuthzDenied(400, "unsupported_op",
                              f"combinaison `{key}` non supportée (attendu : "
                              f"{sorted(str(k) for k in rules)}).")
        return chosen(raw, inp)
    rule.platform_floor = _lowest_floor(rules.values())
    # Sur quels CHAMPS cette règle se branche, et vers quoi — pour qu'un cliquet
    # puisse vérifier qu'une entrée qui porte un axe (`scope`) a bien une autz qui le
    # LIT. Sans ça l'appariement n'existe que dans la tête de celui qui l'a écrit, et
    # poser l'axe sur une entrée gardée par une règle d'org rouvrirait le trou sans
    # qu'une ligne d'autz ait bougé (#681). Introspection SEULE : rien ici ne change
    # une décision d'autz.
    rule.autz_fields = fields
    rule.autz_branches = tuple(rules.values())
    return rule


def RESOURCE_GOVERN(*, type_field: str = "resource_type", id_field: str = "resource_id",
                    op_field: str = "op", list_ops: tuple[str, ...] = ("list",)):
    """Gouvernance d'une ressource possédée (ADR 0030) : owner ∪ escalade `roles.py`,
    résolu par `ownership.can_govern(sub, resource_type, resource_id)`. Couvre owner
    self-service ET super_admin/org_admin/group_admin en une règle. Les ops de
    `list_ops` (qui n'ont pas de `resource_id`) sont autorisées à tout authentifié —
    le handler FILTRE aux ressources gouvernables. Import paresseux d'`ownership`
    (évite tout cycle au chargement des modules de capacités)."""
    def rule(raw: RawCtx, inp: Optional[BaseModel] = None) -> ResolvedCtx:
        sub = _require_sub(raw)
        op = getattr(inp, op_field, None) if inp is not None else None
        if op in list_ops:
            return ResolvedCtx(sub=sub, org_id=access.current_org(sub),
                               role=access.get_user_role(sub))
        rtype = getattr(inp, type_field, None) if inp is not None else None
        rid = getattr(inp, id_field, None) if inp is not None else None
        if not rtype or rid is None:
            raise AuthzDenied(400, "missing_resource",
                              "`resource_type` et `resource_id` requis.")
        from .. import ownership
        # Famille inconnue : le refus est DÉCLARÉ (`unsupported_resource_type`, 400)
        # et le handler sait le rendre — mais il ne sortait que sur `op=list`, la
        # seule op qui saute cette règle. Partout ailleurs `can_govern` → `_kind`
        # levait un `ValueError` nu, que l'adaptateur REST sert en 500 : mesuré le
        # 2026-09-08 en production sur `resource_type="procedure"` (retour d'outil
        # #809). Le refus ÉNUMÈRE les familles acceptées — c'est la seule chose qui
        # répare l'appelant (à l'époque, la procédure ne se gouvernait que sous son
        # nom d'avant #519 ; elle s'appelle `procedure` depuis oto#65). Le message est le MÊME que celui
        # du handler (`resources._check_type`) : une même saisie fautive ne peut pas
        # se lire de deux façons selon l'op.
        #
        # Les familles sont celles que la surface PUBLIE (`resources_contract.KIND_OF`),
        # traduites ici en kind d'`ownership` : la procédure s'appelle `procedure` pour
        # l'appelant et garde sa valeur stockée d'avant #519 (otomata-tech/oto#65).
        from .resources_contract import KIND_OF
        if rtype not in KIND_OF:
            raise AuthzDenied(
                400, "unsupported_resource_type",
                f"type `{rtype}` non supporté ({list(KIND_OF)}).")
        if not ownership.can_govern(sub, KIND_OF[rtype], str(rid)):
            raise AuthzDenied(403, "forbidden",
                              "Gouvernance de cette ressource refusée.")
        return ResolvedCtx(sub=sub, org_id=access.current_org(sub),
                           role=access.get_user_role(sub))
    return rule


def _field_int(inp: Optional[BaseModel], field: str, code: str, label: str) -> int:
    val = getattr(inp, field, None) if inp is not None else None
    if val is None:
        raise AuthzDenied(400, code, f"Champ `{field}` requis.")
    return int(val)


def ORG_MEMBER_OF(field: str):
    """Membre de l'org désignée par `input.<field>` (lecture d'une org par id de
    path, ≠ org active) — escalade platform_admin incluse via `roles`. Miroir
    lecture d'`ORG_ADMIN_OF`."""
    def rule(raw: RawCtx, inp: Optional[BaseModel] = None) -> ResolvedCtx:
        sub = _require_sub(raw)
        org_id = _field_int(inp, field, "missing_org", field)
        if not roles.is_org_member(sub, org_id):
            raise AuthzDenied(403, "forbidden", f"Réservé aux membres de l'org #{org_id}.")
        return ResolvedCtx(sub=sub, org_id=org_id, role=access.get_user_role(sub))
    return rule


def ORG_ADMIN_OF(field: str):
    """Org-admin de l'org désignée par `input.<field>` — escalade platform_admin
    incluse via `roles` (ADR 0012). Porte la garde « dernier admin » au niveau
    handler/store."""
    def rule(raw: RawCtx, inp: Optional[BaseModel] = None) -> ResolvedCtx:
        sub = _require_sub(raw)
        org_id = _field_int(inp, field, "missing_org", field)
        if not roles.is_org_admin(sub, org_id):
            raise _refus_org_admin(org_id)
        return ResolvedCtx(sub=sub, org_id=org_id, role=access.get_user_role(sub))
    return rule


def ORG_ADMIN_OF_OR_OPERATOR(field: str):
    """`ORG_ADMIN_OF`, OU l'opérateur plateforme (`admin` | `super_admin`) — pour une
    LECTURE de supervision d'une org (le journal de ses membres, oto#145). L'`admin`
    opérationnel n'a pas l'escalade vers les orgs tierces (`roles.is_platform_admin`
    = super_admin seul) : cette règle lui ouvre la lecture, jamais l'écriture — elle
    ne se pose que sur une capacité qui ne modifie rien.

    Plancher plateforme `None` : l'org_admin passe sans rôle plateforme."""
    base = ORG_ADMIN_OF(field)

    def rule(raw: RawCtx, inp: Optional[BaseModel] = None) -> ResolvedCtx:
        sub = _require_sub(raw)
        org_id = _field_int(inp, field, "missing_org", field)
        if access.is_platform_operator(sub):
            return ResolvedCtx(sub=sub, org_id=org_id, role=access.get_user_role(sub))
        return base(raw, inp)
    return rule


def ORG_ADMIN_OF_LIVE(field: str):
    """`ORG_ADMIN_OF`, PLUS le refus d'une org **ARCHIVÉE** — la mutation d'un espace
    que toutes les lectures déclarent hors d'atteinte (signal d'usage #467, 15/08).

    Le défaut, vécu sur l'org #229 : `oto_org op=update` a RÉUSSI et l'a renommée,
    pendant que `_org=229` répondait « Tu n'es membre d'aucune org #229 » et qu'elle
    n'apparaissait dans aucun listing. Un même espace était donc à la fois mutable et
    inexistant, selon l'axe emprunté.

    La racine est une asymétrie de seam, pas une garde oubliée : les lectures joignent
    `orgs` et filtrent `archived_at IS NULL`, tandis que `get_org_role` lit `org_members`
    SEULE — `archived_at` ne l'atteint jamais, donc `roles.is_org_admin` reste True sur
    une org archivée et `ORG_ADMIN_OF` laisse passer. On ne redresse pas le rôle
    lui-même (cf. `org_store.is_archived_org` : sur un soft-delete il reste vrai, et
    `org.archive` en dépend pour son idempotence) — on redresse ce que la capacité en
    déduit, ICI, au niveau où l'autz se déclare (ADR 0009 §7), jamais dans un handler.

    ⚠️ **Ne pas généraliser à `org.archive`** : son contrat public promet qu'archiver
    deux fois répond `ok:true, archived:false` (« c'était déjà fait »). Lui donner cette
    règle changerait un idempotent documenté en 409, et casserait tout client qui
    retente une suppression dont il a perdu la réponse.

    409 (et non 403) : le droit est bien là — c'est l'ÉTAT de la ressource qui refuse.
    Un « Réservé à un org_admin » serait faux ET muet, et enverrait chercher du côté
    des permissions un problème qui n'y est pas."""
    base = ORG_ADMIN_OF(field)

    def rule(raw: RawCtx, inp: Optional[BaseModel] = None) -> ResolvedCtx:
        ctx = base(raw, inp)   # appartenance/escalade d'abord : un non-admin n'apprend
                               # pas au passage qu'une org archivée existe.
        from .. import org_store
        if org_store.is_archived_org(ctx.org_id):
            raise AuthzDenied(
                409, "org_archived",
                f"Espace #{ctx.org_id} archivé : il ne se modifie plus. L'archivage l'a "
                "sorti de tous les listings et a rendu sa place à ton quota de création — "
                "le renommer ne récupère rien. Sa restauration n'est pas self-service "
                "(les données restent en base) : crée un nouvel espace, ou fais restaurer "
                "celui-ci par un administrateur.")
        return ctx
    return rule


def ORG_ADMIN_OPT(field: str, autres: Optional[str] = None):
    """Écriture org-admin **self-service par défaut, épinglable explicitement**.

    Si `input.<field>` (un org_id) est fourni → sémantique `ORG_ADMIN_OF` : garde
    admin sur l'org NOMMÉE et l'injecte — **robuste au reset de session** (une perte
    de connexion qui fait retomber `current_org` sur la maison n'écrit plus sur la
    mauvaise org, otomata-private#69). Sinon → sémantique `ORG_ADMIN` : org active
    depuis l'état serveur. Même garde `roles.is_org_admin` (escalade platform_admin)
    dans les deux branches → aucun changement de privilège : un org explicite dont on
    n'est pas admin est refusé exactement comme l'org active.

    `autres` = les chemins ouverts à qui n'est PAS admin, dans les mots de la surface
    gardée. La règle ne peut pas les deviner : elle sait qui a le droit, pas ce que
    l'appelant cherchait à faire."""
    def rule(raw: RawCtx, inp: Optional[BaseModel] = None) -> ResolvedCtx:
        sub = _require_sub(raw)
        explicit = getattr(inp, field, None) if inp is not None else None
        if explicit is not None:
            org_id = int(explicit)
            if not roles.is_org_admin(sub, org_id):
                raise _refus_org_admin(org_id, autres)
            return ResolvedCtx(sub=sub, org_id=org_id, role=access.get_user_role(sub))
        org_id = access.current_org(sub)
        if org_id is None:
            raise AuthzDenied(400, "no_active_org",
                              "Aucune org active — choisis-en une avec oto_use_org, "
                              "ou passe `org` explicitement.")
        if not roles.is_org_admin(sub, org_id):
            raise _refus_org_admin(org_id, autres)
        return ResolvedCtx(sub=sub, org_id=org_id, role=access.get_user_role(sub))
    return rule


def ORG_MEMBER_OPT(field: str):
    """Lecture membre **self-service par défaut, épinglable explicitement** — miroir
    lecture d'`ORG_ADMIN_OPT`.

    Si `input.<field>` (un org_id) est fourni → sémantique `ORG_MEMBER_OF` : garde
    d'appartenance sur l'org NOMMÉE et l'injecte (lecture cross-org par jeton `_org=`,
    ADR 0038 — un accompagnant hors de l'org cible charge un guide nommé par slug).
    Sinon → org active depuis l'état serveur, **gracieux si absente** (`org_id=None`,
    comme `SUB_ONLY`) : le bundle de session (slug omis) reste servi vide hors org, et
    un `get` par slug sans org lève `no_active_org` au handler. Même garde
    `roles.is_org_member` (escalade platform_admin) → aucun changement de privilège."""
    def rule(raw: RawCtx, inp: Optional[BaseModel] = None) -> ResolvedCtx:
        sub = _require_sub(raw)
        explicit = getattr(inp, field, None) if inp is not None else None
        if explicit is not None:
            org_id = int(explicit)
            if not roles.is_org_member(sub, org_id):
                raise AuthzDenied(403, "forbidden", f"Réservé aux membres de l'org #{org_id}.")
            return ResolvedCtx(sub=sub, org_id=org_id, role=access.get_user_role(sub))
        return ResolvedCtx(sub=sub, org_id=access.current_org(sub),
                           role=access.get_user_role(sub))
    return rule


def _group_opt(field: str, allowed, refus: str):
    """Fabrique commune de `GROUP_MEMBER_OPT` / `GROUP_ADMIN_OPT` : équipe explicite
    `input.<field>`, sinon l'équipe ACTIVE de la session, puis la garde `allowed`."""
    def rule(raw: RawCtx, inp: Optional[BaseModel] = None) -> ResolvedCtx:
        sub = _require_sub(raw)
        explicit = getattr(inp, field, None) if inp is not None else None
        group_id = int(explicit) if explicit is not None else access.current_group(sub)
        if group_id is None:
            raise AuthzDenied(400, "no_active_group",
                              "Aucune équipe active — choisis-en une avec oto_use_group, "
                              f"ou passe `{field}` explicitement.")
        if not allowed(sub, group_id):
            raise AuthzDenied(403, "forbidden", refus.format(id=group_id))
        # `org_id` reste l'org ACTIVE (ce que servait la règle d'org sur cette même
        # surface) : l'équipe visée est portée par `group_id`, et c'est elle que le
        # handler lit. Y mettre l'org PARENTE de l'équipe changerait en silence la clé
        # `org_id` de réponses qui ne parlent pas d'équipe.
        return ResolvedCtx(sub=sub, org_id=access.current_org(sub), group_id=group_id,
                           role=access.get_user_role(sub))
    return rule


def GROUP_MEMBER_OPT(field: str):
    """**Appartenance** à l'équipe, **self-service par défaut, épinglable explicitement** —
    miroir équipe d'`ORG_MEMBER_OPT`. Escalade `roles.can_read_group` (membre de
    l'équipe, org_admin du parent, platform_admin).

    ⚠️ Ce n'est pas une règle de LECTURE, c'est une règle d'ACTEUR : elle garde aussi
    l'ÉCRITURE d'une procédure d'équipe (#681). Éditer une procédure, c'est faire son
    travail — celui qui la déroule est un membre, pas un chef, et lui refuser d'écrire
    revient à réserver l'apprentissage à qui n'exécute pas. Le geste est réversible
    (chaque écriture ajoute une version, `from_version` restaure la précédente), donc
    l'ouvrir ne coûte rien d'irrattrapable. Ce qui sépare les deux gestes n'est pas la
    surface, c'est le VERBE : la suppression, elle, reste sur `GROUP_ADMIN_OPT`."""
    return _group_opt(field, roles.can_read_group,
                      "Réservé aux membres de l'équipe #{id}.")


def GROUP_ADMIN_OPT(field: str):
    """**Administration** de l'équipe, **self-service par défaut, épinglable
    explicitement** — miroir équipe d'`ORG_ADMIN_OPT` (oto-backend#681). Escalade
    `roles.can_admin_group` (chef d'équipe, org_admin du parent, platform_admin).

    Réservée aux gestes qu'aucune version ne défait — sur les procédures, la
    SUPPRESSION, qui emporte l'historique. ⚠️ Ne pas la remettre sur l'écriture : le
    rôle de chef emporte par ailleurs les CLÉS PARTAGÉES de l'équipe, donc une garde
    d'écriture trop grossière force une élévation de droits dans un domaine sans
    rapport pour le seul motif d'annoter un mode d'emploi. C'est arrivé, en vrai, chez
    un client — deux fois, la première réponse ayant été « deviens administrateur de
    toute l'organisation ».

    Le palier existait déjà sous `guides._owner_for_write` ; il est ici pour se DÉCLARER
    au niveau de la capacité (ADR 0009 §7) plutôt que de redescendre dans un handler."""
    return _group_opt(field, roles.can_admin_group,
                      "Réservé au chef de l'équipe #{id} (ou à un org_admin du parent).")


def TENANT_ADMIN_OF(field: str, *, platform):
    """Admin du TENANT désigné par `input.<field>` (un slug), OU la règle plateforme
    `platform` (`PLATFORM_ADMIN` pour lire, `SUPER_ADMIN` pour écrire) — L-clés PR 2.

    La règle plateforme est essayée d'abord ; seul son **403** laisse la main au rôle
    de tenant (un 400/401 est rendu tel quel). Le rôle se lit sur le **sub qualifié**
    (`tenancy.tenant_of`), jamais sur le rattachement d'une org (lot L1) : un admin
    déclaré sur `pilote` ne passe que pour `slug='pilote'`, et un compte nu ne passe
    jamais par ce chemin — il n'a pas de tenant tiers.

    Plancher plateforme `None` : l'accès dépend d'une CIBLE (le tenant) que le
    handshake ne connaît pas. ⚠️ Ne pas la mettre dans un combinateur d'outil MCP
    servi à tous : le plancher du combinateur descendrait à `None` et l'outil
    entrerait dans le handshake de chaque compte. L'admin de tenant agit par la face
    REST (son tableau de bord)."""
    def rule(raw: RawCtx, inp: Optional[BaseModel] = None) -> ResolvedCtx:
        sub = _require_sub(raw)
        slug = (getattr(inp, field, None) or "").strip() if inp is not None else ""
        if not slug:
            raise AuthzDenied(400, "missing_slug", f"Champ `{field}` requis.")
        try:
            return platform(raw, inp)
        except AuthzDenied as refus:
            if refus.status != 403:
                raise
        if tenancy.current().tenant_of(sub) != slug or not db.is_tenant_admin(slug, sub):
            raise AuthzDenied(403, "forbidden", f"Réservé à un admin du tenant `{slug}`.")
        return ResolvedCtx(sub=sub, org_id=access.current_org(sub),
                           role=access.get_user_role(sub))
    rule.platform_floor = None
    return rule


def TENANT_ADMIN_OF_TARGET(field: str, *, platform):
    """Admin du tenant **DE LA CIBLE** désignée par `input.<field>` (un sub), OU la
    règle plateforme `platform`.

    Sœur de `TENANT_ADMIN_OF`, et la différence est tout l'intérêt : là-bas le
    périmètre est un slug que l'appelant écrit dans sa requête ; ici il est **dérivé
    de la cible**. La cible vient de l'appelant — c'est normal, c'est ce qu'il
    désigne — mais le périmètre vient du serveur (ADR 0066-R3). Sans ça, la garde
    vérifierait seulement que l'appelant a écrit deux fois le même mot.

    Le tenant se lit sur le PRÉFIXE du sub (`tenancy.tenant_of`), donc sans toucher
    la base : un sub nu relève du tenant primaire et n'est **jamais** atteignable par
    ce chemin, quel que soit l'admin de tenant qui le vise. C'est le même invariant
    que `TENANT_ADMIN_OF`, et il n'est pas décoratif — c'est lui qui empêche qu'un
    partenaire agisse sur un compte de la plateforme.

    ⚠️ La cible est un **sub**, jamais une adresse électronique, alors que les autres
    consoles de compte acceptent les deux. Motif : une adresse ne désigne pas un
    compte de façon unique — le chantier qui a demandé ce geste porte huit personnes
    présentes DEUX fois, une identité chez nous et une chez le partenaire, sous la
    même adresse. Résoudre par adresse choisirait pour l'opérateur, exactement là où
    il doit choisir lui-même. En prime, le tenant reste dérivable sans lecture.

    Plancher plateforme `None` : l'accès dépend d'une CIBLE que le handshake ne
    connaît pas. ⚠️ Sur une surface MCP, ce plancher fait entrer l'outil dans la
    boîte de chaque compte. C'est un coût de surface servie, à assumer explicitement
    — et c'est le seul moyen qu'un admin de tenant puisse APPELER le geste depuis un
    agent : un outil masqué n'est pas seulement invisible, fastmcp en refuse aussi
    l'appel."""
    def rule(raw: RawCtx, inp: Optional[BaseModel] = None) -> ResolvedCtx:
        sub = _require_sub(raw)
        cible = (getattr(inp, field, None) or "").strip() if inp is not None else ""
        if not cible:
            raise AuthzDenied(400, "missing_target", f"Champ `{field}` requis.")
        try:
            return platform(raw, inp)
        except AuthzDenied as refus:
            if refus.status != 403:
                raise
        registre = tenancy.current()
        slug = registre.tenant_of(cible)
        if (slug == tenancy.PRIMARY_SLUG or registre.tenant_of(sub) != slug
                or not db.is_tenant_admin(slug, sub)):
            raise AuthzDenied(403, "forbidden",
                              "Réservé à un admin du tenant dont relève ce compte.")
        return ResolvedCtx(sub=sub, org_id=access.current_org(sub),
                           role=access.get_user_role(sub))
    rule.platform_floor = None
    return rule


def GROUP_MEMBER_OF(field: str):
    """Lecture d'un groupe désigné par `input.<field>` : membre du groupe, OU
    org_admin du groupe parent, OU platform_admin (escalade descendante `roles`).
    Injecte `group_id` + l'`org_id` parent dans le ResolvedCtx."""
    def rule(raw: RawCtx, inp: Optional[BaseModel] = None) -> ResolvedCtx:
        sub = _require_sub(raw)
        group_id = _field_int(inp, field, "missing_group", field)
        # Autorisation AVANT existence — la convention du palier org (`ORG_MEMBER_OF`
        # ne teste que l'appartenance). L'ordre inverse faisait apprendre à un
        # non-membre SI l'équipe #N existe : mince (un entier, sans nom ni contenu),
        # mais c'était une divergence non décidée entre deux paliers qui appliquent
        # par ailleurs la même parti pris de refus non-disclosant (#300).
        # `can_read_group` rend False sur un groupe inexistant ⟹ même 403 dans les
        # deux cas, ce qui est exactement le but.
        if not roles.can_read_group(sub, group_id):
            raise AuthzDenied(403, "forbidden", f"Réservé aux membres du groupe #{group_id}.")
        g = group_store.get_group(group_id)
        if g is None:
            # Autorisé sur un groupe absent : incohérence de données, pas un refus.
            raise AuthzDenied(404, "unknown_group", f"Groupe #{group_id} inconnu.")
        return ResolvedCtx(sub=sub, org_id=g["org_id"], group_id=group_id,
                           role=access.get_user_role(sub))
    return rule


def GROUP_ADMIN_OF(field: str):
    """Écriture sur un groupe désigné par `input.<field>` : chef d'équipe
    (`group_admin`), OU org_admin du groupe parent, OU platform_admin (escalade
    descendante `roles`, ADR 0012). Injecte `group_id` + `org_id` parent."""
    def rule(raw: RawCtx, inp: Optional[BaseModel] = None) -> ResolvedCtx:
        sub = _require_sub(raw)
        group_id = _field_int(inp, field, "missing_group", field)
        # Même ordre que `GROUP_MEMBER_OF` ci-dessus : autorisation d'abord (#300).
        if not roles.can_admin_group(sub, group_id):
            raise AuthzDenied(403, "forbidden",
                              f"Réservé au chef d'équipe (ou org_admin) du groupe #{group_id}.")
        g = group_store.get_group(group_id)
        if g is None:
            raise AuthzDenied(404, "unknown_group", f"Groupe #{group_id} inconnu.")
        return ResolvedCtx(sub=sub, org_id=g["org_id"], group_id=group_id,
                           role=access.get_user_role(sub))
    return rule


# ── Annoncer un droit sans le recopier ───────────────────────────────────────

class _SondeAutz(BaseModel):
    """Entrée minimale d'une sonde d'autz : les seuls champs que la règle LIT.

    Pas l'`Input` de la capacité — celui-ci exige de la donnée de GESTE (un corps, un
    slug, une version) que personne n'a à fabriquer pour poser une question de droit.
    Les règles de ce module ne lisent leur entrée que par `getattr(inp, champ, None)`.
    """
    model_config = ConfigDict(extra="allow")


def capacite_autorise(cle: str, sub: Optional[str], **champs) -> bool:
    """La capacité `cle` laisserait-elle passer `sub` avec ces champs d'entrée ?

    Sert à **annoncer** un droit (un `can_*` servi à un écran) sans le recopier. Un
    drapeau de droit pose exactement la question d'un refus — « ce geste-là
    aboutirait-il ? » — et tant qu'il y répond par SA PROPRE condition, les deux
    dérivent en silence. C'est arrivé sur les procédures (#695) : la garde d'écriture
    est descendue au membre de l'équipe, le drapeau servi est resté sur le critère de
    l'administration, et l'écran a caché à une opératrice un geste que le serveur lui
    accordait — sans qu'une ligne du fichier qui sert le drapeau ait bougé. On exécute
    donc la règle d'autz **déclarée par la capacité** ; il n'y a plus de copie à tenir
    à jour, et déplacer une garde déplace le drapeau avec elle.

    ⚠️ **N'appelle jamais le handler** : seule la règle tourne, et une règle ne fait
    que lire (rôles, org active). Tout `AuthzDenied` vaut `False`, quel que soit son
    statut — un 400 « aucune org active » ferme le geste aussi sûrement qu'un 403.

    ⚠️ Une `cle` inconnue **lève** (`KeyError`) au lieu de rendre `False` : un drapeau
    qui nomme une capacité disparue doit casser bruyamment. Le rendre faux
    ressusciterait exactement le défaut d'origine — une porte fermée à tort, que rien
    ne signale.
    """
    from .registry import CAPABILITIES
    cap = next((c for c in CAPABILITIES if c.key == cle), None)
    if cap is None:
        raise KeyError(f"capacité inconnue : {cle!r} — un droit servi la nomme.")
    try:
        cap.authz(RawCtx(sub=sub), _SondeAutz(**champs))
    except AuthzDenied:
        return False
    return True
