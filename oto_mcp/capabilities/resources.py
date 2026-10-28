"""Gouvernance générique des ressources possédées (ADR 0030).

Une capacité unique `oto_resource(op, …)` réunit lister / inspecter / transférer /
(dé)partager une ressource possédée, quel que soit son type. L'autz est **déclarée**
via `RESOURCE_GOVERN` : owner ∪ escalade `roles.py` (`ownership.can_govern`) pour les
ops ciblées ; `list` ouvert à tout authentifié, le handler FILTRE aux ressources
gouvernables. C'est le chemin qui ferme le trou « un super_admin ne peut pas
transférer un datastore » et qui alimente l'object-browser admin.

Plan GOUVERNANCE uniquement (transférer/lister/partager **sans lire** le contenu) —
la lecture du contenu d'une ressource perso reste l'exception auditée view-as
(ADR 0023). Ce que cette surface DÉCLARE — l'énuméré des trois familles, la forme de
chaque réponse, les refus publiés — vit dans `resources_contract`.
"""
from __future__ import annotations

import logging
import os
from typing import Literal, Optional

from pydantic import BaseModel, Field, PrivateAttr, model_validator

from .. import (access, db, deprecations, email, group_store, org_store, ownership,
               partage_en_attente, roles)
from ..access import heritage
from . import _portee, _publication
from ._authz import RESOURCE_GOVERN
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .docs import partage as page
from .registry import CAPABILITIES
from .resources_contract import KIND_OF, REFUS, REFUS_TYPE_INCONNU, ResourceOut
from .. import config

log = logging.getLogger(__name__)

# ADR 0048 — projection RÔLE ↔ permission (plan contenu) + audiences de publication.
_PERMISSION_OF_ROLE = {"viewer": "read", "editor": "write", "manager": "write"}
_ROLE_FROM_PERMISSION = {"read": "viewer", "write": "editor"}
_PUBLICATION_AUDIENCE = {"public": "anonymous", "secret": "secret"}  # projets uniquement


class ResourceInput(BaseModel):
    """L'entrée de la surface HÉRITÉE. ⚠️ **Son schéma servi est FIGÉ** —
    `tests/resources_input_legacy.json`. Tout durcissement (champ rendu obligatoire,
    motif ajouté, énuméré resserré) va sur `ResourceInputV2` (`resources_v2.py`), qui
    porte aussi le POURQUOI : le durcir ici a cassé de vrais appelants (#756/#774).

    ⚠️ Le défaut de `resource_type` est **connu pour faux et conservé** : omettre le
    champ vise `datastore_namespace`, donc `transfer`/`share` agissent sur une AUTRE
    ressource. Il est documenté dans la description servie plutôt que retiré.
    """
    op: Literal["list", "get", "transfer", "share", "unshare"]
    # ⚠️ Le destinataire par IDENTIFIANT — la sortie d'une adresse ambiguë. Dix
    # adresses portent deux comptes (mesuré le 05/09/2026), dont une paire sans
    # aucun tenant : partager sur l'une d'elles donnait l'accès à l'un des deux,
    # en silence.
    #
    # Ajouté ICI, sur la surface que les gens utilisent, et pas sur `v2` — ADR
    # 0068, décision d'Alexis « pas de v2 ». Le cliquet du schéma servi n'interdit
    # pas d'ajouter : il protège d'une rupture qui FAIT ÉCHOUER un appel (#756
    # rendait un champ obligatoire). Un champ optionnel ne casse aucun appelant ;
    # l'empreinte est regravée dans le même commit, comme le 04/09 pour
    # `permission`.
    sub: str = Field(default="", description=(
        "Identifiant du compte destinataire, quand une adresse en désigne "
        "plusieurs. À passer SEUL, jamais avec `email`."))
    resource_type: str = "datastore_namespace"
    resource_id: Optional[str] = None
    new_owner_email: Optional[str] = None   # transfer → un utilisateur
    new_owner_org: Optional[int] = None     # transfer → une de SES orgs (ADR 0030, owner_type='org')
    new_owner_group: Optional[int] = None   # transfer → un pôle/équipe (ADR 0049, owner_type='group') — cloisonne la ressource à ses membres
    email: Optional[str] = None             # share / unshare (principal user)
    org_id: Optional[int] = None            # share / unshare (principal ORG — livraison client, #52)
    group_id: Optional[int] = None          # share / unshare (principal ÉQUIPE — groupe d'une org dont tu es membre)
    # ADR 0048 — « Partager » unifié : deux axes orthogonaux.
    # AUDIENCE (spectrum) : où va la ressource. `person`/`team`/`org` → grant ;
    # `public`/`secret` → publication (projets) ; `private` → dépublier. None = grant
    # legacy (le principal vient d'email/org_id/group_id ci-dessus).
    audience: Optional[Literal["private", "person", "team", "org",
                               "public", "secret"]] = None
    # RÔLE du grant (viewer=lecteur, editor=éditeur, manager=gérant/gouvernance grantable).
    role: Optional[Literal["viewer", "editor", "manager"]] = None
    # ADR 0068 — « partager » veut dire « qu'il puisse le lire ». Ce défaut valait
    # `"write"`, annoté « rétro-compat » : un héritage, jamais une intention.
    #
    # ⚠️ Ce champ vit sous le CLIQUET du schéma servi (`resources_input_legacy.json`),
    # posé après l'incident #756 — et le changer ici est un choix examiné, pas un
    # oubli. Ce que le cliquet protège, c'est une rupture qui fait ÉCHOUER un appel
    # (#756 rendait un champ obligatoire) ; un défaut plus restrictif ne fait échouer
    # personne, l'appel réussit avec moins de droit. Et le seul appelant hors backend,
    # le dashboard, passe toujours `role` explicitement (`shareResource(…, role)`), qui
    # PRIME sur `permission` — il n'est donc pas concerné. Vérifié le 04/09, décision
    # d'Alexis : pas de seconde surface pour ça.
    permission: Literal["read", "write"] = "read"   # share — legacy (mappé en rôle)
    # Publication (audience public/secret/org sur un PROJET) : préciser les outils exposés.
    mcp_slug: Optional[str] = None          # préfixe de sous-domaine (facultatif en secret)
    mcp_tools: Optional[list[str]] = None   # allowlist figée (vide = réutilise la liste publiée)
    cascade: bool = False                   # share/transfer d'un PROJET : embarquer ses entités liées (#52)
    # #480 (arbitrage du 23/09) — avec QUELLES clés le bénéficiaire d'un projet y
    # travaille. UN paramètre pour les trois audiences (personne, équipe, org), iso.
    # Optionnel : omis = inchangé sur un partage existant, `own` sur un neuf — un
    # re-partage qui ne dit rien (changer un rôle) ne retire pas un héritage en silence.
    credentials: Optional[Literal["own", "inherit"]] = Field(default=None, description=(
        "Project share only — which keys the recipient works with. `own` (default): "
        "their own keys. `inherit`: also the project owner's org/team keys, bounded "
        "by YOUR rights (you must be a member of the owner org). Omitted = unchanged."))
    # otomata-tech/oto#39 — l'échéance d'un partage, sous le nom qu'elle porte sur les
    # jetons (`ttl_days`). ⚠️ STRICTE, contrairement aux jetons, qui lisent « pas
    # d'expiration » dans toute valeur illisible : ici une échéance mal écrite serait un
    # partage éternel que son auteur croit borné — refusée (`invalid_input`), jamais
    # lue comme « sans échéance ». Omis : un partage vivant garde la sienne, un neuf ou
    # un échu n'en a pas (`db.grant_resource`). Optionnel : le cliquet du schéma servi
    # (`resources_input_legacy.json`) est regravé dans le même commit.
    ttl_days: Optional[int] = Field(default=None, ge=1, strict=True, description=(
        "Share only (person/team/org): the share EXPIRES after this many days (integer "
        "≥ 1) — past it, the recipient loses access. Omitted: an active share keeps its "
        "expiry, a new or expired one has none. Nobody is reminded before it expires."))
    confirm_transfer: bool = False          # transfer : lever la confirmation anti-lockout (perte de contrôle assumée)

    # L'avis de l'alias daté qui a servi l'appel (`deprecations.VALEURS`), ou None.
    # PRIVÉ : il ne figure dans aucun schéma servi, et le cliquet de l'héritée
    # (`resources_input_legacy.json`) ne bouge pas.
    _avertissement: Optional[str] = PrivateAttr(default=None)

    @model_validator(mode="wrap")
    @classmethod
    def _valeur_d_hier(cls, data, handler):
        """Réécrit une valeur dépréciée de `resource_type` AVANT toute autre lecture.

        Avant la validation du champ — donc la surface stricte, dont le `Literal` ne
        publie que les noms d'aujourd'hui, l'accepte aussi jusqu'à la date — et avant
        la règle d'autz, qui ne voit ainsi qu'un seul nom par famille. L'avis est
        gardé pour être SERVI dans la réponse (otomata-tech/oto#65)."""
        avis = None
        if isinstance(data, dict) and "resource_type" in data:
            canonique, avis = deprecations.valeur_canonique(
                "resource_type", data["resource_type"])
            if avis:
                data = {**data, "resource_type": canonique}
        inst = handler(data)
        if avis:
            inst._avertissement = avis
        return inst


def _check_type(resource_type: str) -> None:
    """Refus de famille inconnue, dans le handler — le champ hérité est un `str` libre,
    et c'est ce qui rend son défaut compatible. La surface stricte porte un `Literal` et
    refuse à la validation : les deux ne déclarent donc PAS les mêmes refus."""
    if resource_type not in _OPS:
        raise AuthzDenied(400, "unsupported_resource_type",
                          f"type `{resource_type}` non supporté ({list(KIND_OF)}).")


def _owner_label(owner_type: str, owner_id: str) -> Optional[str]:
    """Libellé lisible d'un owner OU d'un principal de grant (email pour un user,
    nom pour une org/un groupe)."""
    if owner_type == "user":
        u = db.get_user(owner_id)
        return u.get("email") if u else None
    if owner_type == "org":
        try:
            o = org_store.get_org(int(owner_id))
        except (TypeError, ValueError):
            return None
        return o.get("name") if o else None
    if owner_type == "group":
        try:
            g = group_store.get_group(int(owner_id))
        except (TypeError, ValueError):
            return None
        return g.get("name") if g else None
    return None


_TYPE_LABELS = {"project": "projet", "datastore_namespace": "datastore",
                "procedure": "procédure", "doc": "page", "app": "app"}
# Recouvrement EN (oto-backend#700) — seules les clés dont le mot CHANGE : les deux
# autres s'écrivent à l'identique dans les deux langues, pas la peine de les répéter
# ici. Le gabarit d'email ne traduit
# pas un mot qu'on lui donne (`email_templates.send_resource_*`), donc c'est ICI, à
# la source du texte, que la langue du DESTINATAIRE choisit le label.
_TYPE_LABELS_EN_OVERRIDES = {"project": "project", "procedure": "procedure"}


def _resource_name(resource_type: str, rid: str) -> Optional[str]:
    """Nom lisible d'une ressource pour une notification. Best-effort → None."""
    try:
        if resource_type == "project":
            r = db.get_project_by_id(int(rid))
            return r.get("name") if r else None
        if resource_type == "datastore_namespace":
            r = db.get_datastore_by_id(int(rid))
            return r.get("datastore") if r else None
        if resource_type == "procedure":
            r = org_store.get_instruction_by_id(int(rid))
            return (r.get("title") or r.get("slug")) if r else None
        if resource_type == "doc":
            return page.portee(rid).get("ressource_nom")
        if resource_type == "app":
            r = db.apps.get(int(rid))
            return r.get("title") if r else None
    except (TypeError, ValueError):
        return None
    return None


def _notify_grant(sharer_sub: str, resource_type: str, rid: str, to_email: str,
                  dest_sub: Optional[str] = None,
                  *, event: str, permission: Optional[str] = None,
                  lien: Optional[tuple[str, Optional[str]]] = None) -> bool:
    """Prévient par email l'utilisateur qui vient de recevoir un accès (`event`
    ='share') ou la propriété (`event`='transfer') d'une ressource. Best-effort,
    tracé — ne casse JAMAIS l'action métier. Ne notifie que les principals `user` :
    pour une org/un groupe destinataire, « qui reçoit » reste à trancher (#77).

    `lien` = `(url, marque)` imposés : un PARTAGE EN ATTENTE pointe son lien
    d'invitation, pas la racine du produit — le destinataire n'a pas encore de compte."""
    try:
        # L'adresse ET la marque suivent le DESTINATAIRE, pas nous : c'est lui qui
        # ouvre le lien. `front_for` les dérive de son tenant ; `(None, None)` = oto,
        # donc le comportement d'avant pour tout compte de la plateforme.
        # Ici l'adresse suffit — on pointe la racine du produit, pas une vue : aucun
        # patron de chemin n'est nécessaire (contrairement aux liens de projet).
        # ⚠️ Le destinataire est DÉJÀ résolu par l'appelant : le re-chercher par
        # adresse rouvrait la porte qu'on vient de fermer (une adresse peut
        # désigner deux comptes, on prendrait la langue et la marque du mauvais).
        # `dest_sub` vient du grant qu'on vient de poser, il ne peut pas mentir.
        dest_user = db.get_user(dest_sub) or {} if dest_sub else {}
        # Préférence du DESTINATAIRE (oto-backend#700) : None (pas de compte, ou
        # compte sans préférence posée) ⟹ les deux gabarits servent FR, comme
        # avant ce lot.
        locale = dest_user.get("locale")
        base, marque = lien or config.front_for(dest_sub)
        app_url = base or config.dashboard_url()
        brand = marque
        sharer = _owner_label("user", sharer_sub)
        name = _resource_name(resource_type, rid)
        labels = ({**_TYPE_LABELS, **_TYPE_LABELS_EN_OVERRIDES} if locale == "en"
                  else _TYPE_LABELS)
        type_label = labels.get(resource_type, "resource" if locale == "en" else "ressource")
        if event == "transfer":
            return email.send_resource_transferred_email(
                to_email, type_label=type_label, name=name, app_url=app_url,
                sharer=sharer, brand=brand, locale=locale)
        return email.send_resource_shared_email(
            to_email, type_label=type_label, name=name, permission=permission,
            app_url=app_url, sharer=sharer, brand=brand, locale=locale)
    except Exception as e:  # best-effort
        log.warning("notify(%s %s %s → %s) failed: %s",
                    event, resource_type, rid, to_email, e)
        return False


def _enrich_datastore(row: dict) -> dict:
    ns_id = int(row["id"])
    return {
        "resource_type": "datastore_namespace",
        "resource_id": str(ns_id),
        "datastore": row["datastore"],
        "owner_type": row.get("owner_type"),
        "owner_id": row.get("owner_id"),
        "owner_label": _owner_label(row.get("owner_type"), row.get("owner_id")),
        "row_count": db.count_datastore_rows_for_ns(ns_id),
        "created_at": row.get("created_at"),
    }


def _enrich_app(row: dict) -> dict:
    return {
        "resource_type": "app",
        "resource_id": str(row["id"]),
        "title": row.get("title"),
        "owner_type": row.get("owner_type"),
        "owner_id": row.get("owner_id"),
        "owner_label": _owner_label(row.get("owner_type"), row.get("owner_id")),
        "updated_at": str(row["updated_at"]) if row.get("updated_at") else None,
    }


def _enrich_project(row: dict) -> dict:
    return {
        "resource_type": "project",
        "resource_id": str(row["id"]),
        "name": row["name"],
        "owner_type": row.get("owner_type"),
        "owner_id": row.get("owner_id"),
        "owner_label": _owner_label(row.get("owner_type"), row.get("owner_id")),
        "archived_at": row.get("archived_at"),
        "created_at": row.get("created_at"),
    }


def _enrich_guide(row: dict) -> dict:
    # Le propriétaire se lit sur les colonnes qui le portent (`owner_type`/`owner_id`),
    # jamais dérivé d'`org_id` : depuis #681 une procédure peut appartenir à une ÉQUIPE,
    # et `org_id` n'en est alors que l'org PARENTE — l'annoncer comme propriétaire
    # désignait la mauvaise cible dans l'écran de partage.
    otype, oid = str(row["owner_type"]), str(row["owner_id"])
    return {
        "resource_type": "procedure",
        "resource_id": str(row["id"]),
        "slug": row["slug"],
        "title": row.get("title"),
        "owner_type": otype,
        "owner_id": oid,
        "owner_label": _owner_label(otype, oid),
        "version": row.get("version"),
        "updated_at": row.get("updated_at"),
    }


# Dispatch par type de ressource pour list/get (transfer/share/unshare sont déjà
# génériques via le seam `ownership`). Étendre = une entrée ici.
# Lambdas (pas des références directes) → `db.X` est résolu au call-time (testable,
# le monkeypatch de db.X est vu).
_OPS: dict[str, dict] = {
    "datastore_namespace": {
        "list_all": lambda: db.list_all_datastores(),
        "list_for_owners": lambda owners: db.list_datastores_for_owners(owners),
        "get_by_id": lambda i: db.get_datastore_by_id(i),
        "enrich": _enrich_datastore,
    },
    "project": {
        "list_all": lambda: db.list_all_projects(),
        "list_for_owners": lambda owners: db.list_projects_for_owners(owners),
        "get_by_id": lambda i: db.get_project_by_id(i),
        "enrich": _enrich_project,
    },
    # Une procédure appartient à une org OU à une équipe (#681) — les deux paires
    # passent. Le filtre `t == "org"` d'avant retirait de la liste toute procédure
    # d'équipe que l'acteur gouverne pourtant : invisible au partage, invisible au
    # transfert, sans un mot.
    "procedure": {
        "list_all": lambda: org_store.list_all_instructions(),
        "list_for_owners": lambda owners: org_store.list_instructions_for_owners(
            [(t, i) for (t, i) in owners if t in org_store.OWNER_TYPES]),
        "get_by_id": lambda i: org_store.get_instruction_by_id(i),
        "enrich": _enrich_guide,
    },
    # UNE page sans son projet (#1084) — lecture seule, gouvernée par son projet.
    "doc": {**page.OPS, "enrich": lambda row: page.enrich(row, _owner_label)},
    # UNE app (`oto_apps`) : son propriétaire et ses partages à elle.
    "app": {
        "list_all": lambda: db.apps.list_all(),
        "list_for_owners": lambda owners: db.apps.list_for_owners(owners, archived=None),
        "get_by_id": lambda i: db.apps.get(i),
        "enrich": _enrich_app,
    },
}


def _grants_view(resource_type: str, resource_id: str) -> list[dict]:
    heritages = (heritage.modes_du_projet(int(resource_id))
                 if resource_type == "project" else None)

    def _label(g: dict) -> Optional[str]:
        # user → email (déjà joint) ; org/group → nom résolu (le front affiche `label`,
        # jamais un principal_id brut).
        return g.get("email") or _owner_label(g.get("principal_type") or "",
                                              g.get("principal_id") or "")
    return [
        {"principal_type": g.get("principal_type"), "principal_id": g.get("principal_id"),
         "email": g.get("email"), "label": _label(g),
         # ADR 0048 : `role` (viewer/editor/manager) = surface produit ; `permission`
         # (read/write) conservé pour rétro-compat des consommateurs existants.
         "role": g.get("role"), "permission": g.get("permission"),
         "granted_at": g.get("granted_at"),
         # oto#39 : l'échéance (null = sans), et le partage échu reste listé, marqué —
         # comme un jeton expiré, que son propriétaire constate au lieu de le perdre.
         "expires_at": g.get("expires_at"), "expired": bool(g.get("expired")),
         # #480 : avec quelles clés ce bénéficiaire travaille (projets seulement).
         **({"credentials": heritages.get((g.get("principal_type"),
                                           str(g.get("principal_id"))), heritage.OWN)}
            if heritages is not None else {})}
        for g in ownership.list_grants(KIND_OF[resource_type], resource_id)
    ] + [
        # Partages EN ATTENTE (adresse sans compte) : listés, marqués, retirables par
        # `op=unshare` avec la même adresse — jamais une ligne de `resource_grants`.
        {"principal_type": "user", "principal_id": None, "email": p["email"],
         "label": p["email"], "role": p.get("resource_role"),
         "permission": _PERMISSION_OF_ROLE.get(p.get("resource_role") or "", "write"),
         "granted_at": p.get("created_at"), "pending": True,
         "invitation_expires_at": p.get("expires_at")}
        for p in partage_en_attente.lister(KIND_OF[resource_type], resource_id)
    ]


def _resolve_recipient(email: Optional[str], sub: str = "") -> dict:
    """Le compte destinataire — par son identifiant, ou par son adresse.

    ⚠️ Une adresse ne DÉSIGNE PAS un compte : dix en portent deux (mesuré le
    05/09/2026), dont une paire sans aucun tenant. Partager sur une adresse
    ambiguë donnait l'accès à l'un des deux, choisi par l'ordre que rend la base
    — et rien ne le disait au propriétaire. C'est le seul endroit de cette classe
    où une erreur donne accès à des DONNÉES.

    Les deux ensemble sont refusés : ils peuvent se contredire, et rien ne dirait
    lequel a servi.
    """
    email, sub = (email or "").strip(), (sub or "").strip()
    if email and sub:
        raise AuthzDenied(
            400, "email_and_sub",
            "Passe `email` OU `sub`, pas les deux : ils peuvent désigner des "
            "comptes différents, et rien ne dirait lequel a servi.")
    if sub:
        u = db.get_user(sub)
        if not u:
            raise AuthzDenied(404, "unknown_user", f"aucun compte `{sub}`.")
        return u
    if not email:
        raise AuthzDenied(400, "email_required", "`email` requis.")
    porteurs = db.get_users_by_email(email)
    if not porteurs:
        raise AuthzDenied(404, "unknown_user", f"aucun utilisateur oto avec l'email {email}")
    if len(porteurs) > 1:
        subs = ", ".join(f"`{u['sub']}`" for u in porteurs)
        raise AuthzDenied(
            400, "ambiguous_email",
            f"L'adresse `{email}` désigne {len(porteurs)} comptes : {subs}. "
            "Reprends avec `sub` (sans `email`) pour dire lequel tu vises.")
    return porteurs[0]


def _share_principal(sub: str, inp: ResourceInput, *, strict: bool = True) -> tuple[str, str, Optional[str]]:
    """Principal d'un share/unshare : une ÉQUIPE (`group_id` — groupe d'une org dont
    l'ACTEUR est membre : granularité interne, jamais cross-org), une ORG (`org_id` —
    livraison client, #52 ; pas d'exigence d'appartenance : on DONNE un accès, on ne
    s'en prend pas un) OU un user (`email`). Renvoie (principal_type, principal_id,
    label). `strict=False` (unshare) : tolère un groupe supprimé et saute le check
    d'appartenance — on doit pouvoir révoquer un grant orphelin."""
    if inp.group_id is not None:
        g = group_store.get_group(int(inp.group_id))
        if g is None:
            if strict:
                raise AuthzDenied(404, "unknown_group", f"groupe #{inp.group_id} inconnu.")
            return "group", str(inp.group_id), f"groupe #{inp.group_id}"
        if strict and not roles.is_org_member(sub, int(g["org_id"])):
            raise AuthzDenied(403, "group_not_visible",
                              "un grant d'équipe cible un groupe d'une org dont tu es membre.")
        return "group", str(inp.group_id), g.get("name")
    if inp.org_id is not None:
        o = org_store.get_org(int(inp.org_id))
        if not o:
            raise AuthzDenied(404, "unknown_org", f"org #{inp.org_id} inconnue.")
        return "org", str(inp.org_id), o.get("name")
    # `getattr` : seule la surface v2 porte `sub` — l'héritée ne peut pas le
    # recevoir sans faire rougir son cliquet de schéma servi.
    u = _resolve_recipient(inp.email, inp.sub)
    return "user", u["sub"], u.get("email")


def _share_credentials(sub: str, inp: ResourceInput, rid: str) -> Optional[str]:
    """Les clés que CE partage d'un projet déclare (#480) — None s'il n'en dit rien.

    Omis : l'état existant est conservé tel quel, arête comprise — et avec elle le
    partageur qui l'a posée et qui la borne. Un re-partage qui ne fait que changer un
    rôle ne touche donc pas au prêt. `inherit` est borné aux droits de celui qui le
    déclare : il ne prête que ce qu'il atteint — être membre de l'org dont le projet
    résout les clés."""
    if inp.credentials == heritage.INHERIT and not heritage.peut_accorder(sub, int(rid)):
        raise AuthzDenied(
            403, "inherit_beyond_sharer_rights",
            "`credentials='inherit'` prêterait les clés de l'org propriétaire du projet, "
            "dont tu n'es pas membre : on ne prête que ce qu'on atteint soi-même. "
            "Partage avec `credentials='own'` (le bénéficiaire pose ses propres clés), "
            "ou demande à un membre de cette org de faire le partage.")
    return inp.credentials


def _share_pending(ctx: ResolvedCtx, inp: ResourceInput, rid: str, kind: str,
                   role: str, perm: str) -> dict:
    """Partage vers une adresse SANS COMPTE : un partage EN ATTENTE (29/09/2026).

    Il ne porte que CET objet et son rôle — un mail avec un lien d'invitation, et
    l'accès à l'inscription (`partage_en_attente`), jamais une adhésion à l'org.
    Refusé avec `cascade` ou un prêt de clés : ce sont des gestes sur un compte
    existant, à refaire une fois la personne inscrite. Un deuxième partage vers la
    même adresse rend le premier, sans second lien ni second mail."""
    from .orgs.invites import _INVITE_TTL_DAYS, _nominal_url
    if inp.cascade or inp.credentials not in (None, heritage.OWN):
        raise AuthzDenied(
            400, "pending_share_plain_only",
            f"{inp.email} n'a pas encore de compte oto : le partage reste en attente et ne "
            "porte que CET objet et son rôle. `cascade` et `credentials='inherit'` se "
            "posent une fois la personne inscrite — repartage alors.")
    nom = _resource_name(inp.resource_type, rid)
    r = partage_en_attente.creer(
        resource_type=inp.resource_type, kind=kind, resource_id=rid, email=inp.email,
        role=role, ttl_days=inp.ttl_days, invited_by=ctx.sub, nom=nom,
        jours=_INVITE_TTL_DAYS)
    adresse = inp.email.strip().lower()
    emailed = False
    if r["token"]:
        front_base, marque = config.front_for(ctx.sub)
        emailed = _notify_grant(ctx.sub, inp.resource_type, rid, adresse, event="share",
                                permission=perm,
                                lien=(_nominal_url(r["token"], adresse, front_base=front_base),
                                      marque))
    _portee.observer(ctx, ressource_type=inp.resource_type, ressource_id=rid,
                     vers="person", geste=f"oto_resource op=share role={role} (en attente)",
                     cible=adresse,
                     **(page.portee(rid) if inp.resource_type == "doc" else {}))
    note = (f"partage en attente : {adresse} n'a pas encore de compte oto. "
            + ("Un email d'invitation lui est envoyé" if emailed
               else "Le partage lui était déjà en attente" if not r["nouveau"]
               else "L'email d'invitation n'a pas pu partir")
            + " ; à son inscription, il aura accès à cet objet, et à rien d'autre.")
    return {"ok": True, "resource_id": rid, "shared_with": adresse,
            "principal_type": "user", "role": role, "permission": perm,
            "expires_at": None, "pending": True, "already_pending": not r["nouveau"],
            "invitation_expires_at": r["expires_at"], "pending_note": note,
            "notified": emailed}


# Le partage en attente, SERVI aux deux surfaces, écrit une fois.
PENDING_DESCRIPTION = (
    "PENDING SHARE (audience person, `email` with NO oto account yet): the share is "
    "NOT refused — it is kept pending: the address gets an email with a link, and on "
    "signup (or by opening the link) gets access to THIS resource with the given role "
    "and NOTHING else — they never join your org. op=share returns `pending: true` and "
    "`pending_note`; sharing again to the same address reuses it (`already_pending`). "
    "op=get lists it in `grants` with `pending: true`; op=unshare with the same `email` "
    "withdraws it. Refused with cascade=true or credentials='inherit' "
    "(`pending_share_plain_only`): share again once they have an account. To give "
    "someone ONE resource, share it — never invite them into your org, which opens "
    "everything the org owns.")


def _cascade_project(sub: str, project_id: int, op: str, *,
                     principal: Optional[tuple[str, str]] = None,
                     role: str = "viewer",
                     new_owner: Optional[tuple[str, str]] = None,
                     credentials: str = heritage.OWN,
                     ttl_days: Optional[int] = None) -> list[dict]:
    """Livraison d'un projet COMPLET (#52) : répercute le geste (share/transfer) sur
    les entités liées (`project_links`). Par entité gouvernée par l'acteur :
    - `tableau`  → même geste (grant au même principal — user/org/groupe, `can_access`
      honore les trois via `AccessorScope.principal_pairs` / transfert au même owner) ;
    - `procedure`→ share = grant READ sur le guide (lisible cross-org par id via
      oto_procedure op=get) ; transfer vers une org = COPIE du guide chez la cible
      + re-pointage du lien (l'originale reste chez la source — zéro casse des autres
      projets qui la référencent) ;
    - `connecteur` → rien à propager : le destinataire branche SON credential (la
      surcharge préfaite du lien — identité/instructions — voyage avec le projet),
      sauf si le partage lui prête les clés du propriétaire (`credentials=inherit`,
      #480) ;
    - `doc` → page Documents = contenu interne à SON projet propriétaire (partager CE
      projet la propage) ; hors périmètre du geste ici → `skipped`.
    L'échéance du partage (`ttl_days`, oto#39) voyage avec lui sur chaque grant posé.
    Les docs/fichiers du projet suivent d'office (ils héritent de son accès).
    Ne lève jamais : chaque entité rapporte `status` (le geste principal a réussi)."""
    report: list[dict] = []
    for link in db.list_project_links(project_id):
        t, ref = link.get("target_type"), str(link.get("target_ref") or "")
        entry = {"target_type": t, "target_ref": ref, "label": link.get("label")}
        try:
            if t == "tableau" and ref.isdigit():
                if not ownership.can_govern(sub, "datastore_namespace", ref):
                    entry["status"] = "skipped"
                    entry["reason"] = "not_governed"
                elif op == "share":
                    ownership.grant("datastore_namespace", ref, principal[0], principal[1],
                                    role=role, granted_by=sub, ttl_days=ttl_days)
                    entry["status"] = "shared"
                    entry["role"] = role
                    entry["permission"] = _PERMISSION_OF_ROLE.get(role, "write")
                else:
                    ownership.transfer("datastore_namespace", ref, new_owner[0], new_owner[1])
                    entry["status"] = "transferred"
            elif t == "procedure" and ref.isdigit():
                if not ownership.can_govern(sub, KIND_OF["procedure"], ref):
                    entry["status"] = "skipped"
                    entry["reason"] = "not_governed"
                elif op == "share":
                    # LECTEUR toujours : le partagé consomme la procédure, il n'édite pas
                    # le master (modèle licence — oto garde la main et pousse les màj).
                    ownership.grant(KIND_OF["procedure"], ref, principal[0], principal[1],
                                    role="viewer", granted_by=sub, ttl_days=ttl_days)
                    entry["status"] = "shared"
                    entry["role"] = "viewer"
                    entry["permission"] = "read"
                else:
                    # Les trois paliers d'un nouveau propriétaire (org, équipe, personne)
                    # sont ceux d'une procédure depuis l'ADR 0068 : la copie vaut pour
                    # tous. Un palier inconnu fait lever le store, et l'entrée ressort
                    # `failed` avec sa raison (le saut qui existait ici, nommé du mot
                    # retiré, était devenu inatteignable — retiré le 23/09/2026, oto#65).
                    copy = org_store.copy_instruction_to_owner(
                        int(ref), new_owner[0], new_owner[1], set_by=sub)
                    db.update_project_link_ref(project_id, "procedure", ref, str(copy["id"]))
                    entry["status"] = "copied"
                    entry["new_ref"] = str(copy["id"])
                    entry["slug"] = copy["slug"]
            elif t == "connecteur" and op == "share" and credentials == heritage.INHERIT:
                entry["status"] = "shared"
                entry["reason"] = "inherited_credential"   # les clés du propriétaire, prêtées (#480)
            elif t == "connecteur":
                entry["status"] = "action_required"
                entry["reason"] = "recipient_credential"   # le client branche SA clé (ADR 0022/0024)
            else:
                entry["status"] = "skipped"
                entry["reason"] = "external_or_unresolved"
        # noqa: SILENT — l'échec est rendu par entrée (status/reason) dans le rapport de cascade
        except Exception as e:   # une entité ratée ne casse pas la livraison
            entry["status"] = "failed"
            entry["reason"] = str(e)
        report.append(entry)
    return report


def _publish_audience(ctx: ResolvedCtx, inp: ResourceInput, rid: str,
                      access_mode: str) -> dict:
    """Audience public/secret → PUBLICATION MCP (ADR 0048 B3). Réservé aux projets
    (seule ressource dotée d'une mécanique de publication). Rôle forcé lecteur (le
    lien/annuaire se consomme, ne s'édite pas). L'autz est déjà gatée (`can_govern`) ;
    l'appartenance au propriétaire (#1176) l'est par `publish_project_mcp`."""
    if inp.resource_type != "project":
        raise AuthzDenied(400, "publication_unsupported",
                          "Le partage public/secret n'existe que pour les projets.")
    # #1176 — refusé ici, sur le chemin de cette capacité, avant toute lecture ;
    # `publish_project_mcp` le re-garde pour tout appelant.
    _publication.exiger_appartenance(ctx.sub, int(rid))
    row = db.get_project_by_id(int(rid))
    if not row:
        raise AuthzDenied(404, "not_found", "projet introuvable.")
    from . import projects as P
    tools = inp.mcp_tools or list(row.get("mcp_tools") or [])
    return P.publish_project_mcp(ctx.sub, row, access_mode=access_mode,
                                 mcp_slug=inp.mcp_slug, mcp_tools=tools)


def _unpublish_audience(ctx: ResolvedCtx, inp: ResourceInput, rid: str) -> dict:
    """Audience `private` → referme la diffusion par lien/annuaire (dépublie le projet)."""
    if inp.resource_type != "project":
        raise AuthzDenied(400, "publication_unsupported",
                          "La publication n'existe que pour les projets.")
    if not db.get_project_by_id(int(rid)):
        raise AuthzDenied(404, "not_found", "projet introuvable.")
    from . import projects as P
    return P.unpublish_project_mcp(ctx.sub, int(rid))


def _resources(ctx: ResolvedCtx, inp: ResourceInput) -> dict:
    """Handler PARTAGÉ par les deux surfaces. Une réponse servie par un alias daté
    porte l'avis (`deprecation_warning`) — seul endroit où il est ajouté, pour que
    les cinq verbes le portent sans qu'aucun ne l'oublie."""
    out = _gouverner(ctx, inp)
    if inp._avertissement:
        out = {**out, "deprecation_warning": inp._avertissement}
    return out


def _gouverner(ctx: ResolvedCtx, inp: ResourceInput) -> dict:
    # Le contrôle vaut pour les deux surfaces : par `resources_v2`, `resource_type`
    # est déjà un `Literal` et ce refus est inatteignable ; par la surface héritée,
    # c'est lui qui refuse. `tests/test_resources_output.py` épingle `_OPS` ==
    # l'énuméré publié — ajouter une famille au dispatch sans l'ajouter au contrat
    # fait rougir.
    _check_type(inp.resource_type)
    ops = _OPS[inp.resource_type]
    # Le kind d'`ownership` (valeur en base) de la famille publique demandée. Tout
    # appel à `ownership` passe par lui, jamais par `inp.resource_type`.
    kind = KIND_OF[inp.resource_type]

    if inp.ttl_days is not None and (inp.op != "share"
                                     or inp.audience in (*_PUBLICATION_AUDIENCE, "private")):
        raise AuthzDenied(
            400, "ttl_days_grant_only",
            "`ttl_days` ne se déclare que sur le partage d'une ressource à une personne, "
            "une équipe ou une org (op=share, audience person/team/org) : c'est "
            "l'échéance de CE partage.")

    if inp.op == "list":
        # PLATEFORME → tout ; sinon → ce que l'acteur gouverne (perso + orgs/groupes
        # qu'il administre). Plan gouvernance : métadonnées seulement, pas de contenu.
        if access.is_platform_operator(ctx.sub):
            rows = ops["list_all"]()
        else:
            scope = ownership.accessor_scope(ctx.sub)
            governed = [("user", ctx.sub)]
            governed += [("org", str(o)) for o in scope.org_ids if roles.is_org_admin(ctx.sub, o)]
            governed += [("group", str(g)) for g in scope.group_ids
                         if roles.can_admin_group(ctx.sub, g)]
            rows = ops["list_for_owners"](governed)
        return {"resource_type": inp.resource_type,
                "resources": [ops["enrich"](r) for r in rows]}

    # Filet pour les appels DIRECTS au handler (tests, réemploi interne) : par les
    # faces servies, `RESOURCE_GOVERN` a déjà refusé un `resource_id` absent avec son
    # propre `missing_resource`. D'où l'absence de ce code des refus DÉCLARÉS — le
    # document ne promet que ce qu'un client peut réellement recevoir.
    if inp.resource_id is None:
        raise AuthzDenied(400, "missing_resource_id", "`resource_id` requis.")
    rid = str(inp.resource_id)

    if inp.op == "get":
        row = ops["get_by_id"](int(rid))
        if not row:
            raise AuthzDenied(404, "not_found", "ressource introuvable.")
        out = ops["enrich"](row)
        out["grants"] = _grants_view(inp.resource_type, rid)
        return out

    if inp.op == "transfer":
        # Le TRANSFERT de propriété exclut le simple gérant (ADR 0048 §3) : owner ∪
        # escalade `roles.py` seulement. Le gate capacité (`RESOURCE_GOVERN`) laisse
        # passer un gérant (il gouverne) → on re-garde ici la structure.
        if not ownership.can_transfer(ctx.sub, kind, rid):
            raise AuthzDenied(403, "forbidden",
                              "Le transfert de propriété est réservé au propriétaire / admin.")
        # Cible : une de SES orgs (owner_type='org'), un de SES groupes (owner_type=
        # 'group', ADR 0049 — poser la ressource au scope du pôle EST la restriction)
        # OU un utilisateur (par email). Transférer VERS une org/un groupe exige d'en
        # être membre (on n'envoie pas une ressource dans un scope où on n'est pas).
        if inp.new_owner_group is not None:
            gid = int(inp.new_owner_group)
            if not roles.can_read_group(ctx.sub, gid):
                raise AuthzDenied(403, "not_group_member",
                                  "tu dois être membre (ou admin) de l'équipe cible pour "
                                  "lui transférer une ressource.")
            new_owner_type, new_owner_id = "group", str(gid)
            new_owner_label = _owner_label("group", str(gid))
        elif inp.new_owner_org is not None:
            org_id = int(inp.new_owner_org)
            if not roles.is_org_member(ctx.sub, org_id):
                raise AuthzDenied(403, "not_org_member",
                                  "tu dois être membre de l'org cible pour lui transférer une ressource.")
            new_owner_type, new_owner_id = "org", str(org_id)
            new_owner_label = _owner_label("org", str(org_id))
        else:
            recipient = _resolve_recipient(inp.new_owner_email)
            new_owner_type, new_owner_id = "user", recipient["sub"]
            new_owner_label = recipient.get("email")
        # Garde-fou anti-lockout (confirmation explicite, choix produit 24/07) : si ce
        # transfert retirerait à l'acteur tout moyen de RÉCUPÉRER la ressource (cession à
        # un tiers, ou à une org/équipe qu'il n'administre pas), on refuse tant qu'il n'a
        # pas confirmé consciemment. Le platform_admin n'est jamais gêné (il rattrape tout).
        if (not inp.confirm_transfer
                and not ownership.would_retain_control(ctx.sub, new_owner_type, new_owner_id)):
            raise AuthzDenied(
                409, "confirm_loss_of_control",
                f"Ce transfert vers « {new_owner_label} » te retirera tout contrôle : tu ne "
                f"pourras plus récupérer cette ressource toi-même (seul un admin Otomata le "
                f"pourra). Renvoie avec confirm_transfer=true pour confirmer.")
        try:
            ownership.transfer(kind, rid, new_owner_type, new_owner_id)
        except ownership.GroupOutsideResourceOrg as e:
            raise AuthzDenied(403, "group_outside_resource_org", str(e))
        except ValueError as e:
            raise AuthzDenied(409, "transfer_failed", str(e))
        out = {"ok": True, "resource_id": rid, "new_owner": new_owner_label}
        if inp.cascade and inp.resource_type == "project":
            out["cascade"] = _cascade_project(ctx.sub, int(rid), "transfer",
                                              new_owner=(new_owner_type, new_owner_id))
            db.log_project_activity(int(rid), ctx.sub, "project.deliver",
                                    f"transfer → {new_owner_label}")
        # Notifier le nouveau propriétaire user (best-effort). Cf. _notify_grant.
        if new_owner_type == "user" and new_owner_label:
            out["notified"] = _notify_grant(ctx.sub, inp.resource_type, rid,
                                            new_owner_label, dest_sub=new_owner_id,
                                            event="transfer")
        return out

    if inp.op == "share":
        # ADR 0048 — « Partager » unifié : l'AUDIENCE route vers le bon mécanisme.
        #   public/secret → publication MCP (projets) ; private → dépublication ;
        #   person/team/org (ou legacy sans audience) → grant au principal, avec RÔLE.
        if inp.credentials is not None and (inp.resource_type != "project"
                                            or inp.audience in (*_PUBLICATION_AUDIENCE,
                                                                "private")):
            raise AuthzDenied(
                400, "credentials_project_share_only",
                "`credentials` ne se déclare que sur le partage d'un PROJET à une "
                "personne, une équipe ou une org (audience person/team/org).")
        if inp.audience in _PUBLICATION_AUDIENCE:
            return _publish_audience(ctx, inp, rid, _PUBLICATION_AUDIENCE[inp.audience])
        if inp.audience == "private":
            return _unpublish_audience(ctx, inp, rid)
        # Rôle effectif : `role` prime ; à défaut rétro-compat depuis `permission`.
        role = inp.role or _ROLE_FROM_PERMISSION.get(inp.permission, "editor")
        perm = _PERMISSION_OF_ROLE.get(role, "write")
        if inp.resource_type == "doc":
            page.require_viewer(role)
        try:
            ptype, pid, plabel = _share_principal(ctx.sub, inp)
        except AuthzDenied as e:
            if e.code != "unknown_user" or not inp.email or inp.sub:
                raise
            return _share_pending(ctx, inp, rid, kind, role, perm)
        creds = _share_credentials(ctx.sub, inp, rid)
        expires_at = ownership.grant(kind, rid, ptype, pid, role=role, granted_by=ctx.sub,
                                     ttl_days=inp.ttl_days)
        out = {"ok": True, "resource_id": rid, "shared_with": plabel,
               "principal_type": ptype, "role": role, "permission": perm,
               "expires_at": expires_at}
        if creds is not None:
            heritage.declarer(int(rid), ptype, pid, creds, ctx.sub)
            out["credentials"] = creds
        # ADR 0068 §4 — observation : ce partage aurait prévenu quelqu'un.
        _portee.observer(ctx, ressource_type=inp.resource_type, ressource_id=rid,
                         vers={"org": "org", "group": "group"}.get(ptype, "person"),
                         geste=f"oto_resource op=share role={role}",
                         cible=str(plabel),
                         **(page.portee(rid) if inp.resource_type == "doc" else {}))
        if inp.cascade and inp.resource_type == "project":
            out["cascade"] = _cascade_project(
                ctx.sub, int(rid), "share", principal=(ptype, pid), role=role,
                credentials=creds or heritage.mode_de(int(rid), ptype, pid),
                ttl_days=inp.ttl_days)
            db.log_project_activity(int(rid), ctx.sub, "project.deliver",
                                    f"share → {plabel}")
        # Notifier le bénéficiaire (best-effort). UNE fois, au niveau capability —
        # jamais dans `ownership.grant` (un share en cascade y déclencherait N mails).
        if ptype == "user" and plabel:
            out["notified"] = _notify_grant(ctx.sub, inp.resource_type, rid, plabel,
                                            dest_sub=pid, event="share",
                                            permission=perm)
        return out

    # unshare
    try:
        ptype, pid, plabel = _share_principal(ctx.sub, inp, strict=False)
    except AuthzDenied as e:
        if e.code != "unknown_user" or not inp.email or inp.sub:
            raise
        # Pas de compte à cette adresse : c'est un partage EN ATTENTE qu'on retire.
        return {"ok": True, "resource_id": rid, "unshared_with": inp.email.strip().lower(),
                "removed": partage_en_attente.retirer(kind, rid, inp.email)}
    removed = ownership.revoke(kind, rid, ptype, pid)
    if inp.resource_type == "project":
        # Retirer l'accès retire le prêt des clés : une arête laissée vivante le
        # rendrait au premier re-partage, sans que personne l'ait redemandé (#480).
        heritage.declarer(int(rid), ptype, pid, heritage.OWN, ctx.sub)
    out = {"ok": True, "resource_id": rid, "unshared_with": plabel, "removed": removed}
    if inp.cascade and inp.resource_type == "project":
        revoked = []
        for link in db.list_project_links(int(rid)):
            t, ref = link.get("target_type"), str(link.get("target_ref") or "")
            rt = {"tableau": KIND_OF["datastore_namespace"],
                  "procedure": KIND_OF["procedure"]}.get(t)
            if rt and ref.isdigit() and ownership.can_govern(ctx.sub, rt, ref):
                if ownership.revoke(rt, ref, ptype, pid):
                    revoked.append({"target_type": t, "target_ref": ref})
        out["cascade"] = revoked
    return out


# Ce qu'un transfert fait à une procédure, SERVI dans la description des deux surfaces
# (otomata-tech/oto#65) : un agent qui déplace une procédure doit savoir qu'il emporte
# son historique, et qu'une cascade de projet, elle, copie. Une seule chaîne pour les
# deux surfaces — deux copies divergeraient au premier lot.
TRANSFER_PROCEDURE = (
    "PROCEDURES (resource_type=procedure): op=transfer MOVES the procedure with its "
    "history — same resource_id, every revision, its project links and its shares follow "
    "it (a slug already taken at the destination gets a suffix, nothing is overwritten). "
    "By contrast, transferring a PROJECT with cascade=true COPIES its linked procedures "
    "into the target (see DELIVER A FULL PROJECT below).")


# Une APP (`oto_apps`) se gouverne comme un tableau — servi aux deux surfaces.
APP_DESCRIPTION = (
    "APPS (resource_type=app): an app has its OWN owner and shares — viewer opens and "
    "uses it, editor changes it, manager re-shares it; op=transfer moves it to a person, "
    "a team or an org. The projects it is filed in give no access. No `cascade`, no "
    "`credentials`: an open app acts as its viewer, bounded by its author.")


# #480 — le texte servi aux deux surfaces (héritée et stricte), écrit une fois.
PUBLICATION_DESCRIPTION = (
    "PUBLISHING (`public`/`secret`) also takes being a member of the org that owns the "
    "project (the parent org of an owning team; the owner themself for a personal "
    "project): a manager from outside is refused 403 `publish_requires_membership` — "
    "`private` (unpublish) stays open to whoever governs.")

CREDENTIALS_DESCRIPTION = (
    "KEYS (`credentials`, project share only, same for person/team/org): a recipient "
    "works in the project with THEIR OWN keys (`own`, the default) — the owner org's "
    "and team's keys are NOT lent. `credentials='inherit'` lends them, bounded by your "
    "own rights (you must be a member of the owner org; refused with "
    "`inherit_beyond_sharer_rights` otherwise); re-share with `credentials='own'` or "
    "unshare to revoke. op=get shows each grant's `credentials`.")


# otomata-tech/oto#39 — l'échéance d'un partage, servie aux deux surfaces, écrite une fois.
EXPIRY_DESCRIPTION = (
    "EXPIRY (`ttl_days`, share to person/team/org only; refused with "
    "`ttl_days_grant_only` elsewhere): the share expires after N days — past it the "
    "recipient loses access (content, listings, lent keys), with NO reminder before. "
    "Omitted on a re-share: an active share keeps its expiry, an expired one is reopened "
    "without one; to remove an expiry, unshare then share. With cascade=true the linked "
    "tableaux/procedures get the same expiry. op=share returns `expires_at` (null = none); "
    "op=get lists each grant's `expires_at` and `expired` — an expired share stays "
    "listed, marked, until unshared.")


CAPABILITIES += [
    Capability(
        key="resources.govern",
        handler=_resources,
        Input=ResourceInput,
        authz=RESOURCE_GOVERN(),
        # Ce que cette surface DÉCLARE vit dans `resources_contract` : l'union
        # complète des cinq verbes, et les refus atteignables. L'enveloppe commune
        # avait été mesurée VIDE le 2026-08-11, d'où l'inscription à la dette de
        # sortie ; c'est l'union, pas l'enveloppe, qui décrit cette surface.
        Output=ResourceOut,
        # Les refus de l'union, PLUS celui que seule cette surface peut rendre : son
        # `resource_type` est un `str` libre, donc la famille inconnue arrive jusqu'au
        # handler et en ressort en 400 nommé. Sur la stricte, le `Literal` refuse
        # avant — déclarer ce code là-bas promettrait un refus jamais rendu.
        errors=REFUS + (REFUS_TYPE_INCONNU,),
        description=(
            "Govern an OWNED resource (ADR 0030) without reading its content. "
            "op=list: resources you govern (platform admins see all); op=get: owner + "
            "shares + metadata (each grant carries a `role`); op=transfer: hand ownership to a "
            "user (`new_owner_email`), to one of YOUR orgs (`new_owner_org`, you must be a "
            "member) OR to one of YOUR teams (`new_owner_group`, ADR 0049 — scoping a resource "
            "to a pôle IS the way to restrict it); a user-owned previous owner keeps editor "
            "access (transfer is owner/admin only, never a grantee). ANTI-LOCKOUT: if the "
            "transfer would leave YOU unable to ever get the resource back (handing to a third "
            "party, or to an org/team you don't administer), it is refused with code "
            "`confirm_loss_of_control` — resend with `confirm_transfer=true` to proceed "
            "consciously (a platform admin is never blocked and can always recover it). "
            "op=share/unshare — ONE unified « Share », two axes (ADR 0048): "
            "AUDIENCE (`audience`) = where it goes: `person` (`email`) / `team` (`group_id`, a "
            "group of an org you belong to) / `org` (`org_id`, a whole org, client delivery) → a "
            "grant; `public`/`secret` → PUBLISH the project (public = listed, secret = "
            "unguessable link) with `mcp_tools` (defaults to the already-published set); "
            "`private` → unpublish. ROLE (`role`) = what they can do: `viewer` (read), `editor` "
            "(write), `manager` (GOVERNANCE — re-share / delete / publish, grantable, but NOT "
            "ownership transfer); public/secret force viewer. " + PUBLICATION_DESCRIPTION
            + " Legacy `permission` read|write is "
            "still accepted (mapped to viewer/editor). " + CREDENTIALS_DESCRIPTION
            + " " + EXPIRY_DESCRIPTION
            + " " + PENDING_DESCRIPTION
            + " resource_type ∈ {datastore_namespace, "
            "project, procedure, doc} — it is the discriminant, and it also decides which shape "
            "comes back. " + TRANSFER_PROCEDURE + " " + page.DESCRIPTION + " " + APP_DESCRIPTION
            + " ⚠️ KNOWN DEFECT, kept for backward compatibility: resource_type "
            "DEFAULTS to `datastore_namespace`. Omitting it does NOT mean « any type » — the "
            "call silently targets a datastore namespace, so op=get/list answer about the "
            "wrong family, and op=transfer/share ACT ON A DIFFERENT RESOURCE than the one you "
            "meant (same numeric id, other family). ALWAYS pass resource_type explicitly. The "
            "fix is a separate surface, oto_resource_v2 / POST /api/resources/v2, which "
            "REQUIRES the field — migrate to it. DELIVER A FULL PROJECT (#52): "
            "share/transfer a project "
            "with cascade=true to carry its linked entities in one gesture — linked "
            "tableaux get the same share/transfer, linked procedures are share-granted "
            "read (readable cross-org via oto_procedure op=get guide_id) or COPIED into "
            "the target org on transfer (link re-pointed, source untouched), connector "
            "links report `recipient_credential` (the recipient plugs their own key; the "
            "project's pre-made identity/instructions overrides travel with it) or "
            "`inherited_credential` under credentials=inherit; docs & "
            "files follow automatically. Returns a per-entity cascade report. "
            "Owner OR org/platform admin governing it; never exposes row content."
        ),
        mcp="oto_resource",
        rest=RestBinding("POST", "/api/resources"),
    ),
]
