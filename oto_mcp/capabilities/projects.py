"""Projet — couche d'organisation (modèle produit 2026-06-27 ; owned resource ADR 0030).

Un **Projet** = un conteneur de travail POSSÉDÉ (owner_type/owner_id) : un nom + un
**brief** (le doc d'entrée, inline pour l'instant). CRUD co-déclaré MCP+REST (ADR 0009).
L'accès dérive du seam `ownership` : `can_access` (contenu, owner ∪ grants) pour
lire/écrire, `can_govern` (owner ∪ escalade `roles.py`) pour archiver.

Hors périmètre de cet incrément (suivants) : le **partage / transfert** (capacité
générique `oto_resource`, resource_type='project' déjà enregistré dans `ownership`),
les **liens** vers tableaux/procédures/connecteurs/bases, et le **Doc arborescent**
(le brief devient alors le Doc racine).
"""
from __future__ import annotations

import logging
import re
import secrets
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

from .. import (config, db, deprecations, group_store, org_store, output_projection,
                ownership, roles, session_org, url_perimeter)
from ._authz import SUB_ONLY, refus_hors_vue
from . import _portee, _publication
from ._types import AuthzDenied, Capability, DeclaredError, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

logger = logging.getLogger(__name__)

RTYPE = "project"


# « doc » RETIRÉ (lot 3 chantier 0.4) : relier des pages = les
# backlinks `[[…]]` (Ship 4), pas un pointeur manuel affiché en groupe de rail.
_LINK_TYPES = ("tableau", "procedure", "connecteur")


# Dossier d'affichage d'un projet : un libellé libre que les listes regroupent.
# Borné pour rester un libellé — une phrase n'est pas un nom de dossier.
_FOLDER_MAX = 80


class ProjectInput(BaseModel):
    op: Literal["create", "list", "list_templates", "get", "update", "archive",
                "unarchive", "copy", "handoff", "link", "unlink", "activity", "runs", "inventory",
                "lint", "publish_mcp", "unpublish_mcp"] = Field(description=(
        "To CHANGE THE OWNER of an existing project (e.g. personal → org) WITHOUT "
        "duplicating it or losing its history (docs, activity, runs stay on the SAME "
        "project_id): no op here does that — `op=copy` DUPLICATES into a new project. "
        "Use `oto_resource(op=\"transfer\", resource_type=\"project\", "
        "resource_id=<id>, new_owner_org=<org_id>, cascade=true)` (ADR 0030): same id, "
        "and a former personal owner keeps write access. `cascade` is NOT optional "
        "detail — it defaults to false, and without it the project moves ALONE while "
        "its linked entities stay behind, silently. With `cascade=true`: linked "
        "tableaux you govern are transferred too, a linked procedure is COPIED to the "
        "new owner org (the original stays with its owner) and connectors come back "
        "`action_required` — the recipient plugs in their own credential."))
    project_id: Optional[int] = Field(default=None, description=(
        "OMIT it on op=runs and you get YOUR OWN still-open runs instead, across every "
        "org, each with its `run_id` — that is how you find a run whose id you lost."))
    stale_days: Optional[int] = None   # lint : seuil « pas retouché depuis » (défaut 90)
    name: Optional[str] = None
    icon: Optional[str] = Field(default=None, description=(
        "update: emoji shown in lists and headers. `\"\"` clears it."))
    folder: Optional[str] = Field(default=None, description=(
        "update: the folder the project is shown under in lists — a free label "
        f"(≤ {_FOLDER_MAX} characters), shared by every project that carries the "
        "same one. Display only: no hierarchy, no access rule. `\"\"` clears it."))
    brief_md: Optional[str] = None
    is_template: Optional[bool] = None   # update : publier/retirer le projet comme MODÈLE (ADR 0032 §7 B5a)
    # publish_mcp : publier le projet en endpoint MCP dédié `<mcp_slug>.mcp.oto.cx` (ADR 0032, amende #44).
    mcp_slug: Optional[str] = None       # label de sous-domaine (^[a-z0-9-]{3,}$) ; en `secret`, sert de préfixe optionnel (un suffixe aléatoire est ajouté serveur)
    mcp_access: Optional[Literal["anonymous", "secret", "org"]] = None  # anonymous = sans login + listé ; secret = sans login, non listé, slug non devinable ; org = JWT + org épinglée
    mcp_tools: Optional[list[str]] = None  # allowlist figée du preset (les seuls tools exposés sur le sous-domaine)
    mcp_expose_datastore: Optional[bool] = None  # `secret` uniquement : exposer les tools data_* en LECTURE (tableaux liés au projet, sous l'autorité de l'org). None = DÉFAUT exposé au partage secret (#193) ; passer False pour refermer
    mcp_expose_datastore_write: Optional[bool] = None  # opt-in ADDITIONNEL (#193) : autoriser l'ÉCRITURE (data_write/data_set_schema) ; sans objet si la lecture n'est pas exposée — défaut False (lecture seule)
    mcp_expose_docs: Optional[bool] = None  # `secret` uniquement : exposer les PAGES du projet (oto_doc en LECTURE) au destinataire. Défaut False — les pages portent des notes internes, les exposer par défaut serait une fuite par surprise.
    mcp_instructions_md: Optional[str] = None  # publish_mcp ET update (#597, sans republier) : prose SERVIE AU DESTINATAIRE de l'endpoint (ce que son agent lit au branchement) — ≠ brief_md, qui reste interne. "" efface.
    # update : périmètre d'URL du projet (#605) — motifs `hôte/chemin/` (ex. `linkedin.com/in/`) que les outils de recherche ÉCARTENT (en le disant) et que les outils d'extraction REFUSENT ; un domaine entier s'écrit explicitement `hôte/*`, un hôte nu est refusé. `[]` retire. Porté aussi par l'endpoint publié du projet, sans republication.
    excluded_url_prefixes: Optional[list[str]] = Field(default=None, description=(
        "update: `host/path/` patterns search tools drop and extraction tools refuse "
        "under this project. A whole host must be written `host/*` — a bare host is "
        "refused. `[]` clears the list."))
    # create : SCOPE owner du projet (ADR 0049 — échelle platform/org/group/user).
    # 'user' (défaut) résout sur l'org ACTIVE ; 'org' = une org dont je suis membre ;
    # 'group' = un pôle/équipe (cloisonne le projet à ses membres + admins d'org) ;
    # 'platform' = projet bibliothèque (admin plateforme seulement).
    # Défaut None, et non 'user' : la PRÉSENCE du champ doit se lire, sinon op=update
    # l'ignorait sans rien dire et l'agent croyait avoir déplacé le projet (#1007).
    # Omis = 'user' pour create/copy (branche `else`).
    owner_type: Optional[Literal["user", "org", "group", "platform"]] = Field(
        default=None, description=(
            "create/copy only (default `user`). op=update refuses it: move a project "
            "with oto_resource op=transfer resource_type=project."))
    owner_id: Optional[str] = None   # org.id si owner_type='org' ; group.id si 'group' ; ignoré sinon
    # link / unlink : un pointeur typé vers une entité regroupée par le projet.
    target_type: Optional[Literal["tableau", "procedure", "connecteur"]] = None
    # Opt-ins d'op=get, cumulables : 'spine' (l'arbre des pages, Ship 2 lot 3, +
    # drill/bornage ci-dessous) · 'procedures' (le CORPS des procédures liées, #313).
    # Opt-ins d'op=list, cumulables : 'links' (les liens de chaque projet, ceux d'op=get
    # — un écran qui cherche quels projets lient une cible lit UNE liste, pas un op=get
    # par projet ; incident du 13/10/2026) · 'audit' (l'audit LÉGER de chaque projet,
    # mêmes clés que celui d'op=get plus `light: true`).
    # Une valeur inconnue est ignorée en silence — l'absence d'un champ optionnel se
    # lit dans la réponse, et refuser ferait d'un ajout futur une rupture de contrat.
    include: Optional[list[str]] = Field(default=None, description=(
        "get: `spine` (page tree) and/or `procedures` (bodies of linked procedures). "
        "list: `links` adds each project's links, as op=get serves them — to find which "
        "projects link a target in ONE call instead of one op=get per project; `audit` "
        "adds each project's LIGHT audit — op=get's keys plus `light: true`: dead "
        "tableaux and connectors missing from the registry only; dead procedures and "
        "unresolvable connectors are checked by op=get alone."))
    from_doc: Optional[int] = None     # enraciner l'épine sur un nœud (drill)
    depth: Optional[int] = None        # profondeur (défaut 2)
    target_ref: Optional[str] = None   # datastore.id | guide slug | connecteur name | doc.id (page Documents)
    label: Optional[str] = None        # nom d'affichage (link)
    role: Optional[str] = None         # pourquoi cette entité est ici / son rôle dans le projet (ADR 0032 §2)
    config: Optional[dict] = None      # surcharge contextuelle PRÉFAITE du lien (ADR 0032 §4) — connecteur : {identity_id?, instructions_md?} (legacy : identité dans config ; multi-binding : voir identity_ref) ; tableau : {provision?: "shared"|"empty"|"seeded"} = comment la COPIE de projet traite ce tableau (ADR 0032 §6)
    identity_ref: Optional[str] = None  # connecteur : identité (compte) du BINDING — clé de multiplicité (#57) ; N liens par connecteur, une identité par binding. link sans identity_ref = binding par défaut ; unlink sans identity_ref = TOUS les bindings du connecteur
    instance_ref: Optional[str] = None  # connecteur : ref d'INSTANCE (ADR 0038 B5, grammaire B4 via oto_instance op=list) — le binding désigne exactement CE credential ; la résolution le sert en dur (re-gardé pour l'appelant). Exclusif d'identity_ref (le ref porte déjà le compte). Stocké config.instance_ref.
    # list : la PORTÉE. Omis/`org` = l'org consultée (ses projets, ceux de ses équipes,
    # ce qui est partagé à elle ou à mes équipes en elle, et mes projets perso créés
    # en elle) — dans mon org PERSO, en plus, mes projets perso sans org de création et
    # ce qui est partagé à moi (28/09, 29/09 et 07/10/2026) ; `me` = ce qui est partagé à MOI
    # en personne, servi dans toute org (29/09/2026 : une org perso est une org comme
    # une autre).
    scope: Optional[Literal["org", "me"]] = Field(default=None, description=(
        "list only: `org` (default) = the projects of the organization you act in, of "
        "its teams, those shared with it or with your teams in it, and your personal "
        "projects created in it (visible to you only) — a personal project is listed "
        "ONLY in the organization where it was created; your PERSONAL org also lists "
        "everything shared with you as a person; `me` "
        "= only the projects shared with YOU as a person, whatever their "
        "organization — served in any organization."))
    # list : retrouver ce qu'on a RANGÉ (issue `oto`#38) — les projets archivés, et eux
    # seuls. Un archivage sans moyen de relire la liste de ce qu'on a archivé ne se
    # défait que si l'on se souvient de l'id.
    archived: Optional[bool] = Field(default=None, description=(
        "list only: `true` lists the ARCHIVED projects (yours and those of the orgs "
        "and teams you see) instead of the live ones — to find one and unarchive it."))
    # archive : le geste qui rend un brief ou une procédure inatteignable se CONFIRME.
    confirm: Optional[bool] = Field(default=None, description=(
        "archive only: required (`true`) when the project carries a non-empty brief or "
        "a linked procedure — without it the archive is refused (409 "
        "`confirm_required`) and the response says what would become unreachable."))
    # list / list_templates : projection — omis = vue de tri, ["*"] = la fiche entière
    fields: Optional[list[str]] = Field(default=None, description=(
        "list/list_templates: output projection. Omitted returns the INDEX (no "
        "briefs); `[\"*\"]` returns whole records; a list of names picks columns."))
    slot: Optional[str] = None         # ADR 0035 (B2) : nom de SLOT que ce lien binde — vocabulaire DU PROJET (unicité (projet, slot) → 409 slot_taken). Fait correspondre le lien aux slots déclarés par les procédures (<slot:name>). Binder un slot TABLEAU dont la procédure déclare un `schema` cible provisionne le namespace vierge avec ce schéma (ADR 0046)


# Les champs de la PUBLICATION que seul `publish_mcp` pose (#597) — `mcp_instructions_md`
# en est exclu : c'est le seul que `op=update` corrige.
_CHAMPS_DE_PUBLICATION = ("mcp_slug", "mcp_access", "mcp_tools", "mcp_expose_datastore",
                          "mcp_expose_datastore_write", "mcp_expose_docs")


def _require(cond, code: str, msg: str, status: int = 400) -> None:
    if not cond:
        raise AuthzDenied(status, code, msg)


def _handoff_md(row: dict) -> str:
    """Texte copier-coller « reprendre dans Claude » (ADR 0032 §7 B5b) : un blob
    universel (Claude/GPT/markdown) qui pré-écrit « charge ce projet ». Pur (entrée
    = dict projet, sortie = str), sans I/O — testable isolément.

    SÉCURITÉ — n'embarque PAS le `brief_md` : un projet partagé/modèle peut porter un
    brief à contenu hostile (injection de prompt) qui, collé dans Claude, s'exécuterait
    comme une consigne. Le blob ne porte que l'instruction de CHARGEMENT (id + nom) ;
    l'agent lit le brief via `oto_project(op=get)` — donnée d'outil, pas texte pré-collé."""
    pid, name = row["id"], row.get("name") or f"#{row['id']}"
    return (
        f"Charge le projet Oto #{pid} « {name} » : appelle `oto_project(op=get, "
        f"project_id={pid})` pour son brief, ses pages et ses entités liées, puis "
        f"passe `project={pid}` sur CHAQUE appel de travail fait pour ce projet "
        f"(ses connecteurs préconfigurés, ses slots et ses tableaux en découlent — "
        f"aucun état de session, ADR 0038)."
    )


def _mcp_url(slug: object, access: str) -> object:
    """URL du connecteur MCP d'un projet publié : `secret` → `<slug>.share.<D>/mcp`
    (partage navigable), `anonymous`/`org` → `<slug>.mcp.<D>/mcp`. `<D>` = domaine de projet
    (PROD `oto.cx` / PREPROD `oto.ninja`, cutover ADR 0040). None si non publié."""
    if not slug or access == "off":
        return None
    d = config.project_domain()
    dom = f"share.{d}" if access == "secret" else f"mcp.{d}"
    return f"https://{slug}.{dom}/mcp"


def _project_web_url(sub: Optional[str], project_id) -> Optional[str]:
    """L'adresse du projet chez ce lecteur, ou None (signal #599).

    Même geste que `docs._doc_url`, et même raison : sans elle, « où je le lis ? » n'a
    pas de réponse, et l'agent se rabat sur un patron d'URL deviné. `None` quand le
    produit du lecteur n'a pas cette vue — jamais notre domaine servi à quelqu'un qui
    n'a pas notre produit (`links`)."""
    from .. import links
    return links.link_for("project", sub=sub, id=project_id)


def _visible_to(row: dict, partages: Optional[list[str]] = None,
                nb_partages: int = 0) -> str:
    """QUI voit ce projet, en une phrase — le fait que la réponse taisait.

    Vécu le 04/09/2026 : une DG demande à un agent de travailler sa base de
    connaissance ; il crée un projet PERSO (`owner_type='user'`, donc visible d'elle
    seule) dans le contexte de son org. Elle le voit apparaître « à la racine de
    l'organisation », en conclut qu'il est **visible par tous**, et passe la matinée à
    vérifier. L'agent lui-même s'est accusé à tort. Le projet était privé.

    Rien dans la réponse ne permettait de trancher : elle rend `owner_type`,
    `owner_id` et `context_org_id` — les faits TECHNIQUES — et laisse dériver la
    conséquence. Personne ne la dérive, et devant un doute sur la confidentialité on
    suppose le pire, avec raison.

    ⚠️ Le contexte n'est PAS la visibilité : un projet perso travaille dans son org de
    contexte et n'y est vu que de son propriétaire — qui le LISTE dans cette org-là, et
    là seulement (décisions des 29/09 et 07/10/2026). C'est cette confusion qu'on paie.

    ⚠️ **La propriété n'est PAS la seule portée** (constaté le 04/09 en inventoriant
    les chemins d'élargissement). Deux faits que cette phrase taisait, et qui la
    rendaient fausse là où elle rassurait le plus :

    1. **Un projet PUBLIÉ échappe à son `owner_type`.** `mcp_access='anonymous'` le
       sert sans login ET le liste dans l'annuaire public ; `'secret'` le sert sur une
       URL non devinable, sans expiration ni rotation. La phrase annonçait « TOUS les
       membres de l'org » sur un projet lisible par n'importe qui.
    2. ~~Une proposition sur un projet perso notifiait les org_admin~~ — **corrigé le
       04/09 même (ADR 0068)**, puis la boucle de propositions et ses e-mails ont été
       retirés (oto#191) : plus aucun e-mail ne porte le corps d'une page proposée.
       La réserve que cette phrase avait dû porter quelques heures est
       retirée : un texte servi qui inquiète pour rien ment autant qu'un texte qui
       rassure à tort.

    ⚠️ **Le partage élargit aussi** (signal #1367, 06/10/2026) : un projet perso
    partagé en lecture à une org et en édition à une personne s'annonçait « toi seul »
    — c'est sur cette phrase qu'on décide de ce qui est confidentiel. `partages` = les
    bénéficiaires vivants, nommés (op=get) ; `nb_partages` = leur nombre seul, quand la
    liste ne les a pas lus un par un."""
    otype = str(row.get("owner_type") or "user")
    org = row.get("context_org_id")
    # La publication PRIME sur la propriété : dite en tête, avant tout le reste.
    pub = str(row.get("mcp_access") or "off")
    prefix = ""
    if pub == "anonymous":
        prefix = ("⚠️ N'IMPORTE QUI sur le web — ce projet est publié sans login et "
                  "listé dans l'annuaire public. Au-delà de ça : ")
    elif pub == "secret":
        prefix = ("⚠️ toute personne qui a l'URL de partage — publiée sans login, sans "
                  "expiration ni rotation"
                  + (", et les tableaux liés sont lisibles par la même URL"
                     if row.get("mcp_expose_datastore") else "")
                  + ". Au-delà de ça : ")
    elif pub == "org":
        prefix = "publié en accès MCP pour les membres de l'org. Au-delà de ça : "
    if partages:
        partage = "partagé avec " + ", ".join(partages)
    elif nb_partages:
        partage = (f"partagé avec {nb_partages} bénéficiaire(s) — le détail : op=get "
                   "ou oto_resource")
    else:
        partage = ""
    if otype == "user" and partage:
        return (prefix + f"son propriétaire, et les bénéficiaires du partage — {partage} "
                "(un partage à une org ou une équipe vaut pour tous ses membres) ; "
                "personne d'autre ne le voit, ni en liste, ni par recherche, ni en "
                "l'ouvrant par son id"
                + (f" ; il est rangé dans le contexte de l'org {org} : l'administrateur "
                   "de ton org peut le consulter, en lecture seule et de façon tracée "
                   "(« voir en tant que », oto#270)" if org is not None else "")
                + ". Seul un opérateur de la plateforme en voit le NOM, jamais le "
                  "contenu.")
    suite = f" ; aussi {partage}" if partage else ""
    if otype == "user":
        # Vérifié sur les CINQ chemins le 04/09 : liste (`mes_objets_ici` : le seul
        # appelant, dans l'org de création du projet — 29/09 et 07/10), recherche
        # (même seam, parité tenue par tripwire), ouverture
        # par id (`_owner_match_content` → `sub == owner_id`, « pas d'escalade
        # plateforme ici, privacy by default »), transfert (`sub == owner_id` ou admin
        # PLATEFORME), et console de gouvernance (un admin d'org n'y reçoit que
        # `("user", son_sub)` + ses orgs).
        # ⚠️ La seule exception est l'opérateur PLATEFORME, qui voit tous les projets en
        # MÉTADONNÉES (nom + propriétaire, jamais le contenu) via cette console. On le
        # dit : un nom de projet est parfois plus révélateur que son contenu.
        return (prefix
                + "toi seul — les autres membres ne le voient ni en liste, ni par "
                  "recherche, ni en l'ouvrant par son id"
                + (f" ; il est rangé dans le contexte de l'org {org}, ce qui n'est PAS "
                   "la même chose qu'y être partagé : l'administrateur de ton org peut "
                   "le consulter, en lecture seule et de façon tracée (« voir en tant "
                   "que », oto#270)" if org is not None else "")
                + ". Seul un opérateur de la plateforme en voit le NOM, jamais le "
                  "contenu.")
    if otype == "org":
        return (prefix + f"TOUS les membres de l'org {row.get('owner_id')} — ce projet "
                "n'est pas privé" + suite)
    if otype == "group":
        return (prefix + f"les membres de l'équipe {row.get('owner_id')}, et les "
                "administrateurs de l'org" + suite)
    return prefix + "tout le monde sur la plateforme (projet bibliothèque)"


_ROLES = {"viewer": "lecture", "editor": "édition", "manager": "gestion"}


def _beneficiaire(g: dict) -> str:
    """Un bénéficiaire vivant, nommé : l'adresse d'une personne, le nom d'une org ou
    d'une équipe (l'id si le nom manque), avec son rôle."""
    ptype, pid = str(g.get("principal_type") or ""), str(g.get("principal_id") or "")
    if ptype == "user":
        nom = g.get("email") or "une personne"
    elif ptype == "org":
        o = org_store.get_org(int(pid)) if pid.isdigit() else None
        nom = f"l'org {o['name'] if o else pid}"
    elif ptype == "group":
        gr = group_store.get_group(int(pid)) if pid.isdigit() else None
        nom = f"l'équipe {gr['name'] if gr else pid}"
    else:
        nom = f"{ptype} {pid}"
    role = _ROLES.get(g.get("role") or "") or (
        "édition" if g.get("permission") == "write" else "lecture")
    return f"{nom} ({role})"


def _partages_vivants(project_id) -> list[str]:
    """Les bénéficiaires vivants du projet (`resource_grants`, échus exclus — ils ne
    donnent plus accès), nommés : la même source que `oto_resource op=get`."""
    return [_beneficiaire(g) for g in ownership.list_grants(RTYPE, str(project_id))
            if not g.get("expired")]


def _view(row: dict, sub: Optional[str] = None, nb_partages: Optional[int] = None) -> dict:
    """`nb_partages` : le nombre de partages vivants, quand une LISTE l'a lu en une
    requête (`db.project_grant_counts`) — sinon le projet lit ses bénéficiaires, nommés."""
    partages = None if nb_partages is not None else _partages_vivants(row["id"])
    return {
        "id": row["id"], "name": row["name"], "icon": row.get("icon"),
        "folder": row.get("folder"),
        # L'adresse web du projet, à côté de son id (#599).
        "url": _project_web_url(sub, row["id"]),
        "brief_md": row.get("brief_md", ""),
        "owner_type": row["owner_type"], "owner_id": row["owner_id"],
        # Qui voit ce projet, en clair. `owner_type` seul oblige à dériver, et personne
        # ne dérive — surtout pas sur une question de confidentialité (04/09).
        "visible_to": _visible_to(row, partages, nb_partages or 0),
        # Org de CONTEXTE d'un projet perso (ADR 0030 amendé) — « moi, org ». NULL sinon.
        "context_org_id": (str(row["context_org_id"])
                           if row.get("context_org_id") is not None else None),
        "is_template": bool(row.get("is_template")),
        # Publication MCP (ADR 0032) : présence + URLs dérivées. `secret` = partage
        # navigable `<slug>.share.oto.cx` (UI + /mcp) ; `anonymous`/`org` = `<slug>.mcp.oto.cx`.
        "mcp_slug": row.get("mcp_slug"),
        "mcp_access": row.get("mcp_access") or "off",
        "mcp_tools": list(row.get("mcp_tools") or []),
        "mcp_expose_datastore": bool(row.get("mcp_expose_datastore")),
        "mcp_expose_datastore_write": bool(row.get("mcp_expose_datastore_write")),
        "mcp_expose_docs": bool(row.get("mcp_expose_docs")),
        # Prose servie au DESTINATAIRE de l'endpoint — ≠ `brief_md`, qui reste interne.
        "mcp_instructions_md": row.get("mcp_instructions_md") or "",
        # Périmètre d'URL (#605) — ce que les outils de recherche/extraction n'atteignent
        # pas dans ce projet. Liste canonique, vide = aucune exclusion.
        "excluded_url_prefixes": list(row.get("excluded_url_prefixes") or []),
        "mcp_url": _mcp_url(row.get("mcp_slug"), row.get("mcp_access") or "off"),
        # Base de PARTAGE navigable (lecture seule, humain) — mode `secret` uniquement.
        "share_url": (f"https://{row['mcp_slug']}.share.{config.project_domain()}"
                      if row.get("mcp_slug") and (row.get("mcp_access") or "off") == "secret" else None),
        "created_at": row.get("created_at"), "updated_at": row.get("updated_at"),
        "archived_at": row.get("archived_at"),
    }


def _projected(rows: list[dict], fields: Optional[list[str]]) -> dict:
    """Vue de LISTE d'un index de projets : les proses deviennent leur taille.

    L'index sert à choisir quel projet ouvrir — 26 projets avec tout leur `brief_md`
    pesaient 73 K caractères, au-delà du plafond d'un tool result. Le brief entier se
    lit par `op=get`, ou ici par `fields=["*"]`.

    `mcp_instructions_md` (prose servie au destinataire d'un endpoint publié) suit la
    même règle : c'est un corps, pas une métadonnée de tri."""
    _require(fields is None or bool(fields), "empty_fields",
             "`fields` est une liste vide : omets-le pour la vue de tri, passe "
             '`["*"]` pour les fiches entières, ou nomme les colonnes voulues.')
    rows, notice = output_projection.summarize(
        rows, body_fields=("brief_md", "mcp_instructions_md"), fields=fields,
        # `url` est gardée quoi qu'on projette (#599) : « lequel j'ouvre ? » est
        # exactement la question de l'index, et l'adresse en est la réponse.
        always=("id", "name", "owner_type", "owner_id", "url"))
    return {"projects": rows, **({"projection": notice} if notice else {})}


def _require_active_org_visible(ctx: ResolvedCtx, row: dict) -> None:
    """Gate de CONTEXTE (ADR 0023) des accès par-id. Sans lui, une URL directe
    `/projects/<id>` (ou `oto_use_project`) atteint un projet d'une AUTRE de mes orgs —
    fuite hors contexte. Délègue la visibilité à `ownership.visible_in_org` (primitive
    partagée) ; ajoute un message ACTIONNABLE (bascule d'org) si l'acteur y a accès par
    une autre org, 404 non-disclosant sinon (ne révèle pas l'existence)."""
    if ownership.visible_in_org(ctx.sub, ctx.org_id, RTYPE, str(row["id"])):
        return
    rid = str(row["id"])
    if ownership.can_access(ctx.sub, RTYPE, rid, "read"):
        owner = ownership.owner_of(RTYPE, rid)
        oname = (org_store.get_org(int(owner[1])) or {}).get("name") \
            if owner and owner[0] == "org" else None
        hint = (f" Il appartient à l'org « {oname} » — passe `org=<id>` sur cet "
                "appel pour l'ouvrir." if oname else "")
        raise AuthzDenied(403, "wrong_org_context",
                          f"Projet #{rid} hors de l'org active.{hint}")
    raise AuthzDenied(404, "unknown_project", f"Projet #{rid} inconnu.")


def _procedure_ref_to_id(org_id: Optional[int], ref: str) -> str:
    """Réf de procédure (ADR 0032) → l'ID stable du guide. Accepte déjà un id
    (chiffres) ou un slug (résolu dans l'org du projet) ; fallback = laisser tel quel
    (guide introuvable / hors org → pas de casse, résolu à la lecture côté front)."""
    if not ref or ref.isdigit() or org_id is None:
        return ref
    inst = org_store.get_instruction("org", org_id, ref)
    return str(inst["id"]) if inst and inst.get("id") is not None else ref


def _ref_canonizer(row: dict, org_id: Optional[int], target_type: str):
    """La fonction qui ramène une réf de lien à son écriture CANONIQUE — celle que `link`
    STOCKE aujourd'hui (l'id stable). Identité pour les types sans normalisation."""
    if target_type == "tableau":
        return lambda r: _resolve_tableau_id(row, r) or str(r or "").strip()
    if target_type == "procedure":
        return lambda r: _procedure_ref_to_id(org_id, str(r or "").strip())
    return lambda r: str(r or "").strip()


def _unlink_refs(links: list[dict], target_type: str, given: str, canon) -> list[str]:
    """Les `target_ref` STOCKÉS que la réf demandée désigne — la cible réelle d'un unlink.

    Le stockage est canonique (id) DEPUIS que `link` normalise nom/slug→id, mais les
    lignes d'avant portent encore le NOM du namespace (tableau) ou le SLUG du guide
    (procédure) — et le chemin de LECTURE les résout toujours (#117) : ce sont des liens
    bien vivants, avec leur `namespace` et leur slot. L'unlink, lui, canonisait la réf
    demandée puis supprimait CET id : zéro ligne touchée quand la ligne porte l'autre
    écriture, et un `ok: true` par-dessus (#699). On confronte donc les deux côtés
    canonisés, dans les deux sens.

    Renvoie les refs BRUTES, sans doublon : c'est la valeur STOCKÉE qui doit matcher la
    clause SQL, jamais sa forme canonique. Vide = rien à délier (le caller refuse).

    UNE règle, pas deux : `canon` est déterministe, donc deux refs identiques canonisent
    pareil — un `stored == given` en OU ne déciderait jamais seul, et masquerait une
    canonisation cassée derrière un vert."""
    want = canon(given)
    out: list[str] = []
    for l in links:
        if l.get("target_type") != target_type:
            continue
        stored = str(l.get("target_ref") or "")
        if not stored or stored in out:
            continue
        if canon(stored) == want:
            out.append(stored)
    return out


def _rien_a_delier(links: list[dict], target_type: str, given: str) -> str:
    """Le message d'un unlink qui n'a RIEN retiré : ce qu'on cherchait, et ce que le projet
    porte vraiment pour ce type. Sans le second, l'agent ne peut que réessayer à l'identique."""
    presents = [str(l.get("target_ref")) for l in links
                if l.get("target_type") == target_type and l.get("target_ref")]
    tete = (f"Aucun lien `{target_type}` « {given} » sur ce projet : RIEN n'a été retiré "
            "(déjà délié ?).")
    if not presents:
        return f"{tete} Ce projet ne porte aucun lien `{target_type}`."
    montre = presents[:12]
    return (f"{tete} Liens `{target_type}` de ce projet : "
            + ", ".join(f"« {r} »" for r in montre)
            + ("…" if len(presents) > len(montre) else "")
            + " — reprends la réf telle que `op=get` la rend.")


def _tableau_owner_candidates(row: dict) -> list[tuple[str, str]]:
    """Où CHERCHER un tableau nommé, pour un projet donné — du plus spécifique au plus large.

    Le détenteur d'un projet n'est PAS le détenteur de ses tableaux : un projet PERSO
    (scope membre, ADR 0030 §8) appartient à `(user, sub)` tandis que ses tableaux naissent
    dans l'ORG de travail ; un projet d'ÉQUIPE appartient au groupe, mais l'équipe range
    couramment ses tableaux au niveau de l'org parente. Chercher contre le seul owner brut
    rendait le lien par NOM impossible dans les deux cas — la doc promet « id/slug/name »,
    seul l'id marchait (signaux #272 · #286 · #287)."""
    otype, oid = str(row.get("owner_type") or ""), str(row.get("owner_id") or "")
    cands: list[tuple[str, str]] = []
    if otype and oid:
        cands.append((otype, oid))
    if otype == "user" and row.get("context_org_id"):
        cands.append(("org", str(row["context_org_id"])))   # l'org de travail du projet perso
    elif otype == "group" and oid.isdigit():
        g = group_store.get_group(int(oid))
        if g and g.get("org_id"):
            cands.append(("org", str(g["org_id"])))          # l'org parente de l'équipe
    return cands


def _resolve_tableau_id(row: dict, ref: str) -> Optional[str]:
    """Réf de tableau → l'ID NUMÉRIQUE stable du namespace (comme les procédures). Accepte
    un id (chiffres, renvoyé tel quel) OU un nom de namespace, résolu contre les scopes du
    projet (`_tableau_owner_candidates`). Renvoie None si un nom ne résout nulle part — le
    caller décide (erreur au link, ref brute conservée à l'unlink). Stocker l'id garde la
    résolution cohérente (audit, list_project_links, share_ui l'attendent numérique)."""
    ref = str(ref or "").strip()
    if not ref or ref.isdigit():
        return ref or None
    for otype, oid in _tableau_owner_candidates(row):
        ns = db.get_datastore(otype, oid, ref)
        if ns and ns.get("id") is not None:
            return str(ns["id"])
    return None


#: Le type de ressource (ownership) de chaque cible de lien qu'on désigne par numéro.
_RTYPE_CIBLE = {"tableau": ownership.TYPE_RESSOURCE_DATASTORE,
                "procedure": ownership.TYPE_RESSOURCE_PROCEDURE}


def _exiger_cible_dans_la_portee(sub: str, row: dict, target_type: str, ref: str) -> None:
    """Un lien `tableau`/`procedure` ne se POSE que vers une cible que le projet a le
    droit de désigner — la règle qui sert ensuite le lien (`db.refs_dans_la_portee`,
    celle de `list_project_links`) — ET que le lieur LIT : on ne range pas dans un projet
    ce qu'on ne voit pas (le tableau perso d'un autre membre, la procédure d'une équipe
    dont on n'est pas).

    ⚠️ **Aucune exemption pour une cible du propriétaire du projet.** Écrire un projet
    n'est pas en être membre : un bénéficiaire d'un partage en écriture, extérieur à
    l'org, liait n'importe quel tableau de cette org par son numéro, puis le recopiait
    chez lui (`op=copy`, gardé par la seule lecture du projet). Membres, objets à soi,
    bibliothèque plateforme et partages reçus par l'org passent tous `can_access`.

    ⚠️ **Le numéro ne prouvait rien** (avant ce correctif) : nom et slug se résolvaient
    dans la portée du projet, mais un identifiant était stocké tel quel — et servi tel
    quel : libellé et titre dans les liens, lignes et corps sur la page publiée, lignes
    recopiées par `op=copy`, schéma posé par un slot. N'importe quel tableau ou
    procédure de la plateforme, pour qui en devinait le numéro.

    Refus 404, MÊME message pour « n'existe pas » et « existe ailleurs » : distinguer
    les deux dirait qu'il existe. Seule exception : une cible que le lieur LIT déjà
    (typiquement, celle d'une autre org dont il est aussi membre) — son existence ne
    lui apprend rien ; on lui dit pourquoi le lien est refusé et quoi faire à la place
    (la copier dans l'org du projet, ou la partager à cette org), en 403
    `target_out_of_scope`."""
    if not str(ref).isdigit():
        return
    rtype = _RTYPE_CIBLE[target_type]
    dans_la_portee = int(ref) in db.refs_dans_la_portee(int(row["id"]), target_type, [ref])
    try:
        lisible = ownership.can_access(sub, rtype, str(ref), "read")
    except ValueError as e:
        # Une procédure sans propriétaire (`ownership._guide_owner`) est une
        # incohérence de données : elle se signale, et ne se lie pas — refus 404
        # comme une cible inconnue, pas une 500.
        logger.warning("link %s #%s : propriétaire illisible (%s)", target_type, ref, e)
        lisible = False
    if dans_la_portee and lisible:
        return
    if lisible:
        quoi = "Ce tableau" if target_type == "tableau" else "Cette procédure"
        _require(False, "target_out_of_scope",
                 f"{quoi} (#{ref}) n'appartient pas à l'org du projet : un lien le "
                 "montrerait à tout le projet, y compris à qui ne le lit pas. "
                 "Copie-le dans cette org, ou partage-le à l'org du projet "
                 "(`oto_resource op=share audience=org`), puis lie la copie ou le partage.",
                 403)
    if target_type == "tableau":
        _require(False, "unknown_tableau",
                 f"Aucun tableau « {ref} » dans le datastore du projet.", 404)
    _require(False, "unknown_procedure",
             f"Aucune procédure « {ref} » dans la portée du projet.", 404)


def _mcp_unresolvable_tools(row: dict, tools: list[str],
                            expose_datastore: bool = False) -> list[str]:
    """Sonde de publication SANS LOGIN (anonymous/secret, ADR 0032) : un endpoint sans
    login n'a pas d'identité user → un tool n'est servi que s'il est résoluble SANS `sub`.
    Renvoie la liste des tools **non résolubles** pour l'org propriétaire : tool spine/méta
    (`oto_*`, `data_*`… — sans connecteur, exige une identité) ou credential absent
    (`access.connector_resolvable_for_org`). **Non bloquant** (choix produit) : on publie
    quand même, ces tools sont exposés mais **échouent proprement à l'appel** (McpError, pas
    de fallback) ; la liste remonte en warning pour que l'humain configure une clé d'org ou
    retire les outils.

    `expose_datastore` (opt-in `secret`) : les tools `data_*` agissent alors SOUS l'org
    propriétaire (pas de connecteur, pas de `sub` requis) → considérés résolubles."""
    from .. import access, providers
    from ..tool_visibility import namespace_of
    if row.get("owner_type") != "org":
        return list(tools)  # pas d'org propriétaire → rien ne résout
    org_id = int(row["owner_id"])
    bad = []
    for t in tools:
        if expose_datastore and namespace_of(t) == "data":
            continue  # datastore de l'org, servi sous son autorité (opt-in)
        con = providers.connector_for_namespace(namespace_of(t))
        if con is None or not access.connector_resolvable_for_org(con.name, org_id):
            bad.append(t)
    return sorted(bad)


def _gen_secret_slug(base: Optional[str]) -> str:
    """Slug NON DEVINABLE pour un endpoint `secret` (URL secrète). Un préfixe optionnel
    lisible (issu du slug saisi, réduit à `[a-z0-9-]`) aide à identifier l'endpoint ; le
    suffixe aléatoire garantit l'imprévisibilité. C'est le SEUL secret d'accès du mode
    `secret` (URL-as-capability, endpoint servant sous les credentials de l'org) → 128 bits
    d'entropie, dimensionné contre le bruteforce en ligne (le préfixe, dérivé du nom, est
    devinable : l'entropie doit tenir dans le suffixe seul). `token_hex` reste dans
    `[a-f0-9]` ⊂ charset `_MCP_SLUG_RE` (`token_urlsafe` introduirait `A-Z_-`, rejeté)."""
    prefix = re.sub(r"[^a-z0-9]+", "-", (base or "").strip().lower()).strip("-")[:24]
    token = secrets.token_hex(16)  # 32 chars hex = 128 bits
    return f"{prefix}-{token}" if prefix else f"mcp-{token}"


def unpublish_project_mcp(sub: str, project_id: int) -> dict:
    """Retire la publication MCP d'un projet. AUCUN contrôle d'autz (le caller a déjà
    gaté `can_govern`) — réutilisé par oto_project ET le « Partager » unifié (ADR 0048)."""
    db.set_project_mcp_publication(project_id, slug=None, access="off", tools=[])
    db.log_project_activity(project_id, sub, "project.unpublish_mcp", None)
    return _view(db.get_project_by_id(project_id), sub)


def publish_project_mcp(sub: str, row: dict, *, access_mode: str,
                        mcp_slug: Optional[str], mcp_tools: Optional[list[str]],
                        expose_datastore: Optional[bool] = None,
                        expose_datastore_write: Optional[bool] = None,
                        expose_docs: Optional[bool] = None,
                        instructions_md: Optional[str] = None) -> dict:
    """Cœur de la publication MCP d'un projet (ADR 0032) — partagé par la capacité
    `oto_project` et par le « Partager » unifié (`oto_resource` audience public/secret,
    ADR 0048 B3). Le caller a déjà gaté `can_govern` ; s'y ajoute ICI, pour tous les
    chemins et tous les modes d'accès, l'appartenance au propriétaire (#1176,
    `_publication.exiger_appartenance`) : un gérant extérieur ne publie pas. Lève
    `AuthzDenied` sur entrée invalide (tools vide en public/org, slug manquant, slug pris)."""
    project_id = int(row["id"])
    _publication.exiger_appartenance(sub, project_id)
    tools = [t for t in (mcp_tools or []) if t and t.strip()]
    # Un endpoint `anonymous`/`org` EST un preset d'outils figé → liste requise. Un lien
    # `secret` (UI navigable lecture seule) peut tout exposer → liste vide autorisée (le
    # front applique déjà la même règle avant l'appel).
    _require(bool(tools) or access_mode == "secret", "missing_tools",
             "`mcp_tools` (liste non vide) requis pour un endpoint public ou org — "
             "seul un lien « secret » peut tout exposer en lecture seule.", 400)
    # Datastore exposé (LECTURE) : DÉFAUT au partage `secret` (#193), refermable ;
    # réservé à `secret`. L'ÉCRITURE est un opt-in additionnel séparé.
    expose_ds = ((access_mode == "secret") if expose_datastore is None
                 else bool(expose_datastore))
    _require(not (expose_ds and access_mode != "secret"), "datastore_secret_only",
             "mcp_expose_datastore est réservé à l'accès `secret` (un endpoint "
             "`anonymous` est public, un endpoint `org` résout déjà data_* via le "
             "membre authentifié).", 400)
    expose_ds_write = bool(expose_datastore_write) and expose_ds
    # Pages : opt-in EXPLICITE (jamais un défaut), `secret` seulement. Régime INVERSE du
    # datastore ci-dessus, et c'est voulu : le datastore d'un projet est le plus souvent
    # le livrable qu'on partage, les pages sont de la doc interne.
    expose_docs_eff = bool(expose_docs) and access_mode == "secret"
    _require(not (expose_docs and access_mode != "secret"), "docs_secret_only",
             "mcp_expose_docs est réservé à l'accès `secret` (un endpoint "
             "`anonymous` est public ; un endpoint `org` résout oto_doc via le "
             "membre authentifié).", 400)
    # Slug effectif : `secret` → non devinable (réutilise l'existant pour ne pas casser
    # l'URL déjà distribuée) ; anonymous/org → slug saisi requis.
    if access_mode == "secret":
        slug = (row.get("mcp_slug") if row.get("mcp_access") == "secret" and row.get("mcp_slug")
                else _gen_secret_slug(mcp_slug))
    else:
        _require(bool(mcp_slug), "missing_slug", "`mcp_slug` requis.", 400)
        slug = mcp_slug
    unresolvable = (_mcp_unresolvable_tools(row, tools, expose_ds)
                    if access_mode in ("anonymous", "secret") else [])
    try:
        db.set_project_mcp_publication(project_id, slug=slug, access=access_mode, tools=tools,
                                       expose_datastore=expose_ds,
                                       expose_datastore_write=expose_ds_write,
                                       expose_docs=expose_docs_eff)
    except ValueError as e:
        code = "slug_taken" if str(e).startswith("slug_taken") else "bad_slug"
        _require(False, code, str(e), 409 if code == "slug_taken" else 400)
    # Endpoint AUTHED (#44) : enregistre l'API resource Logto (audience JWT). Best-effort.
    resource_registered = None
    if access_mode == "org":
        try:
            from ..auth import facade as oauth_facade
            oauth_facade.ensure_api_resource(
                f"https://{slug}.mcp.{config.project_domain()}/mcp",
                name=f"oto MCP — {row.get('name') or slug}")
            resource_registered = True
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).exception("ensure_api_resource échoué pour %s", slug)
            resource_registered = False
    if instructions_md is not None:
        db.set_project_mcp_instructions(project_id, instructions_md)
    db.log_project_activity(project_id, sub, "project.publish_mcp", f"{access_mode}:{slug}")
    out = _view(db.get_project_by_id(project_id), sub)
    if resource_registered is not None:
        out["logto_resource_registered"] = resource_registered
    if unresolvable:
        out["mcp_unresolvable_tools"] = unresolvable
    warnings = []
    if not config.project_domain_is_production():
        # Les sous-domaines de projet hors prod n'ont PAS de certificat public (cert
        # interne Caddy) : l'URL est parfaitement fonctionnelle pour tester, et
        # REJETÉE par tout client MCP réel. Le dire ICI, pas au client à qui on
        # vient d'envoyer le lien (vécu, feedback #308).
        warnings.append(
            "cette instance ne se déclare pas comme la production (OTO_ENV) : le "
            f"certificat TLS de {config.project_domain()} n'y est pas reconnu "
            "publiquement, un client MCP refusera la connexion. Ne transmets pas cette "
            "URL à un tiers — republie depuis la production.")
    if not (db.get_project_by_id(project_id) or {}).get("mcp_instructions_md"):
        warnings.append(
            "aucune instruction publiée : l'agent du destinataire se branchera sans "
            "savoir ce que contient ce projet ni comment le lire. Passe "
            "`mcp_instructions_md` (≠ le brief interne).")
    if warnings:
        out["warnings"] = warnings
    return out


def _project(ctx: ResolvedCtx, inp: ProjectInput) -> dict:
    sub = ctx.sub
    _require(inp.scope is None or inp.op == "list", "unsupported_scope",
             "`scope` ne s'applique qu'à op=list. Retire-le.")
    _require(inp.archived is None or inp.op == "list", "unsupported_archived",
             "`archived` ne s'applique qu'à op=list. Retire-le.")
    _require(inp.confirm is None or inp.op == "archive", "unsupported_confirm",
             "`confirm` ne s'applique qu'à op=archive. Retire-le.")

    if inp.op == "create":
        _require(inp.name and inp.name.strip(), "missing_name", "`name` requis.")
        # `context_org` = l'org de CONTEXTE d'un projet PERSO (owner='user') : sépare la
        # propriété (la personne) du contexte de travail (ADR 0030 amendé). NULL pour un
        # projet non-perso (org/group/platform), dont le contexte se dérive de l'owner.
        context_org: Optional[int] = None
        if inp.owner_type == "org":
            _require(inp.owner_id, "missing_owner",
                     "`owner_id` (org) requis pour un projet d'org.")
            _require(roles.is_org_member(sub, int(inp.owner_id)), "forbidden",
                     "Tu n'es pas membre de cette org.", 403)
            owner_type, owner_id = "org", str(inp.owner_id)
        elif inp.owner_type == "group":
            # ADR 0049 : projet de PÔLE — cloisonné aux membres du groupe (+ escalade
            # org_admin, inaliénable). Créer = être du pôle ou l'administrer.
            _require(inp.owner_id, "missing_owner",
                     "`owner_id` (group) requis pour un projet d'équipe.")
            gid = int(inp.owner_id)
            _require(group_store.get_group(gid) is not None, "unknown_group",
                     f"Groupe #{gid} inconnu.", 404)
            _require(roles.can_read_group(sub, gid), "forbidden",
                     "Tu n'es pas membre de cette équipe.", 403)
            owner_type, owner_id = "group", str(gid)
        elif inp.owner_type == "platform":
            # ADR 0049 : projet BIBLIOTHÈQUE — gouverné par la plateforme, lisible de tous.
            _require(roles.is_platform_admin(sub), "forbidden",
                     "Un projet plateforme est réservé aux admins plateforme.", 403)
            owner_type, owner_id = "platform", "platform"
        else:
            # Défaut = PERSO (ADR 0030 amendé 2026-07-17) : le projet naît possédé par le
            # créateur `(user, sub)`, PRIVÉ, dans le CONTEXTE de son org active. L'org/team
            # deviennent des cibles de partage/transfert explicites, plus le défaut. Owner
            # = « moi » ; `context_org` = « mon org » — l'identité `(moi, org)`.
            _require(ctx.org_id is not None, "no_active_org", "Aucune org active.", 400)
            owner_type, owner_id, context_org = "user", sub, int(ctx.org_id)
        pid = db.create_project(owner_type, owner_id, inp.name.strip(),
                                inp.brief_md or "", created_by=sub,
                                context_org_id=context_org)
        db.log_project_activity(pid, sub, "project.create", inp.name.strip())
        if owner_type != "user":
            # ADR 0068 §4 — naître chez l'org est explicite, et reste observé : c'est
            # le geste, pas le défaut, qui rend un contenu lisible par d'autres.
            _portee.observer(ctx, ressource_type="project", ressource_id=pid,
                             ressource_nom=inp.name.strip(), vers=owner_type,
                             geste=f"oto_project op=create owner_type={owner_type}",
                             cible=str(owner_id))
        return _view(db.get_project_by_id(pid), sub)

    if inp.op == "list":
        from .. import project_audit
        # Pastilles d'ÉTAT de l'index (refonte UX, ADR 0032) : nb d'entités liées +
        # partagé + « à vérifier » (audit) + droit d'écriture. Tout se lit EN LOT, pour
        # tous les projets de la liste à la fois : le nombre de requêtes ne dépend pas
        # du nombre de projets (incident du 13/10/2026 — par projet, la liste d'une org
        # de vingt projets faisait plus de deux cents requêtes).
        # `include=["links"]` rend en plus les liens de chaque projet, ceux d'op=get :
        # un écran qui cherche quels projets lient une cible lit UNE liste, au lieu d'un
        # op=get par projet.
        avec_liens = bool(inp.include and "links" in inp.include)
        # `include=["audit"]` rend l'audit LÉGER de chaque projet, déjà calculé pour
        # `has_audit` : sans requête de plus. Mêmes clés que l'audit d'op=get, plus
        # `light: true` — ni procédure morte ni connecteur non résolvable n'y figurent
        # (leur vérification est une requête par lien : op=get seul).
        avec_audit = bool(inp.include and "audit" in inp.include)
        grant_counts: dict[int, int] = {}

        def _enrich(rows: list[dict], shared: bool) -> list[dict]:
            liens = db.list_project_links_many([r["id"] for r in rows])
            ecriture = ownership.can_access_many(
                sub, RTYPE, {str(r["id"]): (r["owner_type"], str(r["owner_id"])) for r in rows},
                "write")
            out = []
            for r in rows:
                links = liens[int(r["id"])]
                # LISTE : audit LÉGER (checks en mémoire seuls) — l'audit complet par
                # projet (résolution procédures + résolvabilité connecteur + run_stats)
                # produisait un N+1 → timeout 180 s (oto/#6 A7). Le badge « à vérifier »
                # reste sur les liens morts détectables sans requête ; le détail complet
                # est servi par op=get.
                aud = project_audit.audit_project(r["id"], links, light=True)
                has_audit = bool(aud.get("dead_links") or aud.get("unbound_slots")
                                 or aud.get("inert_procedures"))
                out.append({
                    **_view(r, sub, grant_counts.get(r["id"], 0)), "entity_count": len(links),
                    "has_audit": has_audit,
                    "shared": shared or grant_counts.get(r["id"], 0) > 0,
                    # `can_write` sur la LISTE (pastille « lecture ») — même règle qu'op=get :
                    # un projet partagé en lecture seule remonte false.
                    "can_write": ecriture[str(r["id"])],
                    **({"links": links} if avec_liens else {}),
                    **({"audit": {**aud, "light": True}} if avec_audit else {})})
            return out

        def _received(principals: list[tuple[str, str]], seen: set) -> list[dict]:
            # Vue bornée (oto#270) : un partage reçu ne passe que visible dans O.
            recus = ownership.borner_a_la_vue(sub, RTYPE,
                                              db.list_projects_granted_to(principals),
                                              rid=lambda r: r["id"])
            recus = [r for r in recus if r["id"] not in seen]
            grant_counts.update(db.project_grant_counts([r["id"] for r in recus]))
            return [{**v, "permission": r.get("permission")}
                    for r, v in zip(recus, _enrich(recus, True))]

        if inp.archived:
            _require(inp.scope != "me", "unsupported_scope",
                     "`archived=true` liste ce qui est rangé dans l'org consultée ; un "
                     "partage personnel n'y appartient pas. Retire `scope`.")
            return _projected(_archived_in_org(ctx), inp.fields)

        if inp.scope == "me":
            # « Partagés avec moi » : le partage PERSONNEL (principal ('user', sub)) —
            # une lentille « moi », servie dans toute org (29/09/2026 : une org perso
            # est une org comme une autre ; un partage à une personne n'appartient à
            # aucune org). L'ACCÈS ne dépend pas de la liste : `visible_in_org` honore
            # le partage dans toute org.
            return _projected(_received([("user", sub)], set()), inp.fields)

        # La LISTE de l'org consultée (décisions des 28 et 29/09/2026, amendent l'ADR
        # 0030 §8) : ses projets, ceux de ses pôles (ADR 0049 : mes équipes, ou toutes si
        # j'en suis admin), ce qui est partagé à elle ou à mes équipes en elle, marqué
        # `shared` — et MES projets personnels créés dans cette org (`mes_objets_ici`),
        # invisibles des autres membres sauf partage ; dans aucune autre org, même pas
        # mon org perso (07/10/2026). Aucun partage fait à MOI : il se liste dans mon
        # org PERSO. Deux seams, les mêmes que la recherche
        # (`accessible_project_ids`) : « cherchable ⇔ lisible »
        # (tripwire `test_search_scope_tripwire`).
        _require(bool(ownership.project_list_owners(sub, ctx.org_id)), "no_active_org",
                 "Aucune org active.", 400)
        own_rows = ownership.projets_possedes_ici(sub, ctx.org_id)
        grant_counts.update(db.project_grant_counts([r["id"] for r in own_rows]))

        own = _enrich(own_rows, False)
        shared = _received(ownership.principaux_de_liste(sub, ctx.org_id),
                           {p["id"] for p in own})
        return _projected(own + shared, inp.fields)

    if inp.op == "list_templates":
        # Modèles (is_template) lisibles DANS l'org consultée — la bibliothèque copiable
        # (B5a) : ceux de la liste de l'org (mêmes owners qu'`op=list` : l'org, ses
        # pôles, et mes modèles perso créés dans cette org) et (ADR 0049) les
        # modèles PLATFORM-owned, pour tous. Jamais l'union de toutes mes orgs : un
        # modèle d'une autre org se voit depuis cette org-là.
        rows = ownership.projets_possedes_ici(
            sub, ctx.org_id, extra=[("platform", "platform")], templates_only=True)
        comptes = db.project_grant_counts([r["id"] for r in rows])
        return _projected([_view(r, sub, comptes.get(r["id"], 0)) for r in rows],
                          inp.fields)

    if inp.op == "runs" and inp.project_id is None:
        # « Fermer un déroulé dont on a perdu l'identifiant » (#473). `run_finish`
        # n'accepte qu'un `run_id`, et rien ne le rendait à son propriétaire : le bloc
        # de contexte annonce les derniers déroulés par leur INTITULÉ, et un run ouvert
        # hors projet n'était énumérable nulle part. Il restait donc « en cours » pour
        # toujours — le régime dominant, pas le cas rare (15 des 16 runs ouverts
        # mesurés en prod, cf. `run_status`).
        #
        # Ce n'est PAS un repli silencieux du cas « projet manquant » : c'est une
        # portée DÉCLARÉE, que la réponse nomme (`scope`, `open_only`). Toutes les
        # autres ops par-id continuent de refuser en nommant leur manque, juste en
        # dessous.
        #
        # Ouverts SEULEMENT, parce que c'est la question posée — « qu'est-ce qu'il me
        # reste à refermer ? » — et qu'un historique complet noierait deux runs à clore
        # sous vingt runs déjà clos. Les runs d'un PROJET, eux, se demandent en nommant
        # le projet, et rendent tout (c'est la pastille ok/échec du viewer).
        from .. import run_status
        if ownership.vue_bornee() is not None:
            # Toutes orgs confondues par construction (cf. `db.my_runs`) : en vue bornée,
            # les runs se lisent par projet, ou dans la lentille de l'org.
            raise refus_hors_vue("la liste des runs ouverts du compte (toutes orgs)")
        runs = db.my_runs(sub, limit=20, open_only=True)
        return {"scope": "mine", "open_only": True,
                "runs": [{**r, "status": run_status.describe(r)} for r in runs]}

    # ops ciblées : project_id requis + existence
    _require(inp.project_id is not None, "missing_project", "`project_id` requis.")
    rid = str(inp.project_id)
    row = db.get_project_by_id(int(inp.project_id))
    _require(row is not None, "unknown_project", f"Projet #{inp.project_id} inconnu.", 404)

    # Gate de CONTEXTE d'org (ADR 0023) — UNE fois pour toutes les ops par-id : un projet
    # n'est atteignable (lecture comme mutation) que DANS l'org qui le possède, jamais
    # depuis une AUTRE de mes orgs. Le pendant par-id du scoping de `op=list`. SEUL `copy`
    # y échappe : copier un MODÈLE (ou un projet lisible) cross-org est une feature (B5a).
    if inp.op != "copy":
        _require_active_org_visible(ctx, row)

    if inp.op == "get":
        from .. import project_audit
        links = db.list_project_links(int(inp.project_id))
        out = {**_view(row, sub),
               "can_write": ownership.can_access(sub, RTYPE, rid, "write"),
               "links": links,
               # B5 : liens vérifiés comme des refs — le lien mort remonte à l'agent
               # qui LIT le projet (brief), pas seulement à op=inventory (curation).
               # `links` réutilisé : pas de double chargement.
               "audit": project_audit.audit_project(int(inp.project_id), links)}
        # ÉPINE (lot 3 Ship 2) — opt-in, bornée, enracinable : l'arbre des pages dans
        # l'ordre curé avec chapôs (fallback dérivé). C'est la CARTE que l'agent lit
        # pour se repérer, puis oto_doc(op=get) la page — jamais op=list de tout.
        if inp.include and "spine" in inp.include:
            out["spine"] = db.project_spine(
                int(inp.project_id), from_doc=inp.from_doc,
                depth=(inp.depth if inp.depth is not None else 2))
        # PROCÉDURES (#313) — opt-in : le CORPS des procédures liées, pour qu'on
        # puisse lire la règle qui a produit une fiche sans quitter l'écran.
        if inp.include and "procedures" in inp.include:
            out["procedures"] = _linked_procedures(sub, links)
        return out

    if inp.op == "lint":
        # B1 (#6) : santé des pages du projet (stale / vides / titres en double) — un
        # seul list_docs_for_project, checks en mémoire (pas de N+1). Accès = gate org
        # ci-dessus (comme op=get). L'agent/l'user curent ensuite.
        from datetime import datetime, timedelta
        from .. import doc_lint
        days = inp.stale_days if inp.stale_days is not None else 90
        cutoff = (datetime.now() - timedelta(days=max(1, int(days)))).strftime("%Y-%m-%d %H:%M:%S")
        docs = db.list_docs_for_project(int(inp.project_id))
        return {"project_id": int(inp.project_id), "stale_days": days,
                **doc_lint.lint_docs(docs, stale_before=cutoff)}

    if inp.op == "activity":
        # Chaque événement porte l'IDENTITÉ de son auteur (`actor`, résolue du sub loggé)
        # → l'Historique dashboard affiche « par X » réel (refonte UX, ADR 0032).
        rows = db.list_project_activity(int(inp.project_id))
        activity = [{
            "sub": r.get("sub"), "action": r["action"], "detail": r.get("detail"),
            "created_at": r.get("created_at"),
            "actor": ({"name": r.get("actor_name"), "email": r.get("actor_email")}
                      if r.get("actor_name") or r.get("actor_email") else None),
        } for r in rows]
        return {"id": inp.project_id, "activity": activity}

    if inp.op == "runs":
        # Derniers runs (ADR 0017) d'une procédure liée — pastille ok/échec du viewer.
        # `target_ref` = id stable du guide → résolu en slug (clé de `runs.doctrine`) ;
        # omis = tous les runs du projet. Read seul (gate de contexte d'org déjà passée).
        from .. import org_store  # local (org_store est shadowé en local par d'autres branches)
        slug: Optional[str] = None
        if inp.target_ref:
            ref = str(inp.target_ref)
            instr = org_store.get_instruction_by_id(int(ref)) if ref.isdigit() else None
            slug = (instr or {}).get("slug") or ref
        return {"id": inp.project_id, "target_ref": inp.target_ref,
                "runs": db.project_runs(int(inp.project_id), guide=slug)}

    if inp.op == "handoff":
        # « Reprendre dans Claude » (B5b) : blob copier-coller qui charge ce projet.
        return {"id": inp.project_id, "markdown": _handoff_md(row)}

    if inp.op == "inventory":
        # Inventaire DÉRIVÉ du projet (ADR 0035 B4) — jamais déclaré : surface d'outils
        # = refs <tool:> des procédures liées ∪ usage observé des runs (0017), plus les
        # connecteurs (liens ∪ slots connecteur des procédures). Sert le préremplissage
        # de publish_mcp (l'humain cure) + le manifeste dashboard.
        from .. import org_store, providers, tool_registry
        from ..tool_visibility import namespace_of
        links = db.list_project_links(int(inp.project_id))
        procedures, proc_tools, slot_connectors = [], [], set()
        for l in links:
            if l["target_type"] != "procedure":
                continue
            ref = str(l["target_ref"])
            # `procedure_id` : dans la portée du projet (`db.list_project_links`) ; une
            # procédure d'ailleurs ne livre ni son slug ni ses outils.
            instr = (org_store.get_instruction_by_id(int(l["procedure_id"]))
                     if l.get("procedure_id") is not None else None)
            if not instr:
                procedures.append({"ref": ref, "resolved": False})
                continue
            refs = tool_registry.ref_names(instr.get("body_md") or "")
            slots = instr.get("slots") or []
            procedures.append({"ref": ref, "slug": instr["slug"], "resolved": True,
                               "tools": refs, "slots": slots})
            proc_tools += refs
            slot_connectors |= {s.get("connector") or s["name"] for s in slots
                                if s.get("type") == "connecteur"}
        run_tools = db.project_run_tools(int(inp.project_id))
        # Union suggérée : refs des procédures d'abord (l'intention), puis l'usage ;
        # les tools spine/méta (sans connecteur au registre : oto_*, run_*, data_*…)
        # sont écartés de la suggestion (non publiables), les sources restent brutes.
        seen, tools, connectors = set(), [], set(slot_connectors)
        for t in proc_tools + run_tools:
            if t in seen:
                continue
            seen.add(t)
            con = providers.connector_for_namespace(namespace_of(t))
            if con is None:
                continue
            tools.append(t)
            connectors.add(con.name)
        connectors |= {l["target_ref"] for l in links if l["target_type"] == "connecteur"}
        # Source de CHAQUE connecteur (pour distinguer dans l'UI « déclaré au projet »
        # vs « requis par une procédure » vs « vu en run ») — additif, `connectors` reste
        # la liste plate rétro-compatible.
        csources: dict[str, set] = {}

        def _tag(con, src):
            if con:
                csources.setdefault(con, set()).add(src)
        for l in links:
            if l["target_type"] == "connecteur":
                _tag(l["target_ref"], "declared")
        for p in procedures:
            if not p.get("resolved"):
                continue
            slug = p.get("slug")
            for s in (p.get("slots") or []):
                if s.get("type") == "connecteur":
                    _tag(s.get("connector") or s.get("name"), f"procedure:{slug}")
            for t in (p.get("tools") or []):
                con = providers.connector_for_namespace(namespace_of(t))
                _tag(con.name if con else None, f"procedure:{slug}")
        for t in run_tools:
            con = providers.connector_for_namespace(namespace_of(t))
            _tag(con.name if con else None, "run")
        from .. import project_audit
        return {"id": inp.project_id, "tools": tools, "connectors": sorted(connectors),
                "connector_sources": {k: sorted(v) for k, v in csources.items()},
                "sources": {"procedures": procedures, "runs": run_tools,
                            "tableaux": [{"slot": l.get("slot"),
                                          "datastore": l.get("datastore"),
                                          "ref": l["target_ref"]}
                                         for l in links if l["target_type"] == "tableau"]},
                # B5 : liens vérifiés comme des refs — morts / slots non bindés / inertes.
                "audit": project_audit.audit_project(int(inp.project_id), links)}

    if inp.op == "update":
        # Changer de propriétaire n'est pas une mise à jour : c'est un TRANSFERT, avec
        # ses gardes (cession, anti-lockout, org de l'équipe cible). L'ignorer en
        # silence faisait croire à l'agent que le projet avait bougé (#1007).
        _require(inp.owner_type is None and inp.owner_id is None, "owner_change_unsupported",
                 "`op=update` ne change pas le propriétaire d'un projet, et rien n'a été "
                 "modifié. Déplace-le avec `oto_resource op=transfer resource_type=project "
                 "resource_id=<id>` (+ `new_owner_org`, `new_owner_group` ou "
                 "`new_owner_email`), puis renvoie l'update sans `owner_type`/`owner_id`.")
        _require(ownership.can_access(sub, RTYPE, rid, "write"), "forbidden", "Écriture refusée.", 403)
        # Publier/retirer comme MODÈLE est un acte de gouvernance (visible aux autres
        # membres de l'org comme bibliothèque) → can_govern, pas un simple write.
        if inp.is_template is not None:
            _require(ownership.can_govern(sub, RTYPE, rid), "forbidden",
                     "Publier un modèle est réservé au propriétaire / admin.", 403)
        # De la PUBLICATION, l'update ne prend que l'instruction servie (#597). Corriger
        # une phrase imposait de rejouer `publish_mcp` — donc de redéclarer accès et
        # outils — et une session prudente s'en abstenait : l'instruction périmée
        # restait servie. Le reste de la publication (slug, accès, outils, opt-ins)
        # se redéclare par `publish_mcp` ; ici il serait IGNORÉ, donc refusé (#1007).
        a_republier = [n for n in _CHAMPS_DE_PUBLICATION if getattr(inp, n) is not None]
        _require(not a_republier, "publication_field_on_update",
                 f"`op=update` ne touche pas à la publication ({', '.join(a_republier)}), "
                 "et rien n'a été modifié. Seule `mcp_instructions_md` s'y corrige ; "
                 "le reste se redéclare par `op=publish_mcp`.")
        # Avant toute écriture, comme les refus au-dessus : le périmètre d'URL plus bas
        # s'écrit seul, et « rien n'a été modifié » doit rester vrai.
        folder = inp.folder.strip() if inp.folder is not None else None
        _require(folder is None or len(folder) <= _FOLDER_MAX, "folder_too_long",
                 f"`folder` est un libellé d'au plus {_FOLDER_MAX} caractères, et rien "
                 "n'a été modifié.")
        # Ce que l'agent d'un TIERS lit au branchement : même autorité que la
        # publication elle-même, pas un simple write.
        if inp.mcp_instructions_md is not None:
            _require(ownership.can_govern(sub, RTYPE, rid), "forbidden",
                     "Corriger l'instruction servie par l'endpoint publié est réservé "
                     "au propriétaire / admin.", 403)
        if inp.excluded_url_prefixes is not None:
            # Périmètre d'URL (#605) : normalisé À LA POSE, refus nommé sur un motif
            # trop large (un hôte nu) ou malformé — rien n'est stocké d'un lot faux.
            try:
                prefixes = url_perimeter.normalize_prefixes(inp.excluded_url_prefixes)
            except url_perimeter.PerimeterError as e:
                raise AuthzDenied(400, "invalid_url_prefix",
                                  f"`excluded_url_prefixes` : {e.message}")
            db.set_project_excluded_url_prefixes(int(inp.project_id), prefixes)
        db.update_project(int(inp.project_id),
                          name=(inp.name.strip() if inp.name else None),
                          brief_md=inp.brief_md, is_template=inp.is_template,
                          icon=inp.icon, folder=folder)
        db.log_project_activity(int(inp.project_id), sub, "project.update", inp.name or None)
        if inp.mcp_instructions_md is not None:
            # L'instruction SEULE : slug, accès, outils et opt-ins ne bougent pas.
            db.set_project_mcp_instructions(int(inp.project_id), inp.mcp_instructions_md)
            db.log_project_activity(int(inp.project_id), sub,
                                    "project.update_mcp_instructions", None)
        return _view(db.get_project_by_id(int(inp.project_id)), sub)

    if inp.op == "copy":
        # Copier un projet qu'on peut LIRE (le sien ou un modèle) → nouveau projet
        # possédé par SOI (ADR 0068). L'original reste intact.
        # ⚠️ La copie était possédée par l'org active (ADR 0032 §7 B5a), y compris
        # depuis un projet PERSONNEL : dupliquer son propre travail le publiait à ses
        # collègues, sans qu'aucun paramètre ne l'ait demandé. Le cas que 0032 visait —
        # copier un MODÈLE pour l'équipe — se dit maintenant : `owner_type='org'`.
        _require(ownership.can_access(sub, RTYPE, rid, "read"), "forbidden", "Accès refusé.", 403)
        _require(inp.name and inp.name.strip(), "missing_name", "`name` (cible) requis.")
        _require(ctx.org_id is not None, "no_active_org", "Aucune org active.", 400)
        if inp.owner_type == "org":
            _require(inp.owner_id, "missing_owner",
                     "`owner_id` (org) requis pour copier vers une org.")
            _require(roles.is_org_member(sub, int(inp.owner_id)), "forbidden",
                     "Tu n'es pas membre de cette org.", 403)
            cible = ("org", str(inp.owner_id), None)
        elif inp.owner_type == "group":
            _require(inp.owner_id, "missing_owner",
                     "`owner_id` (group) requis pour copier vers une équipe.")
            _require(roles.can_read_group(sub, int(inp.owner_id)), "forbidden",
                     "Tu n'es pas membre de cette équipe.", 403)
            cible = ("group", str(inp.owner_id), None)
        else:
            cible = ("user", sub, int(ctx.org_id))
        new_id, warnings = db.duplicate_project(int(inp.project_id), inp.name.strip(),
                                                cible[0], cible[1], copied_by=sub,
                                                context_org_id=cible[2],
                                                lisible=lambda t, rid: ownership.can_access(
                                                    sub, t, rid, "read"))
        if cible[0] != "user":
            _portee.observer(ctx, ressource_type="project", ressource_id=new_id,
                             ressource_nom=inp.name.strip(), vers=cible[0],
                             geste=f"oto_project op=copy owner_type={cible[0]}",
                             cible=str(cible[1]))
        return {**_view(db.get_project_by_id(new_id), sub),
                "links": db.list_project_links(new_id), "copied_from": inp.project_id,
                "warnings": warnings}

    if inp.op in ("link", "unlink"):
        _require(ownership.can_access(sub, RTYPE, rid, "write"), "forbidden", "Écriture refusée.", 403)
        _require(inp.target_type and inp.target_ref, "missing_target",
                 "`target_type` et `target_ref` requis.")
        # ADR 0032 « stop using slug » : une procédure est référencée par l'ID STABLE de
        # le guide. On accepte un slug (naturel côté agent) OU un id et on stocke l'id
        # (idem à l'unlink pour matcher les lignes migrées).
        target_ref = inp.target_ref
        identity_ref = inp.identity_ref
        config = dict(inp.config) if inp.config else None
        proj_org = int(row["owner_id"]) if row.get("owner_type") == "org" else ctx.org_id
        if inp.target_type == "procedure":
            target_ref = _procedure_ref_to_id(proj_org, target_ref)
        elif inp.target_type == "tableau":
            # Normalise nom→id (le datastore du propriétaire du projet). Stocker l'id garde
            # la résolution cohérente ; un nom introuvable au LINK = erreur (pas de lien mort
            # silencieux), mais un unlink d'une réf legacy/supprimée passe avec la réf brute.
            resolved = _resolve_tableau_id(row, target_ref)
            if resolved is not None:
                if deprecations.adresse_par_nom(target_ref, resolved):
                    session_org.noter_avis(
                        deprecations.avis_nom_de_tableau(str(target_ref), int(resolved)))
                target_ref = resolved
            elif inp.op == "link":
                _require(False, "unknown_tableau",
                         f"Aucun tableau nommé « {target_ref} » dans le datastore du projet.", 404)
        elif inp.target_type == "connecteur":
            # L'identité est la clé du BINDING (#57). Fin du doublon : on la sort de
            # config.identity_id vers `identity_ref`. `identity_ref` explicite (front B4 /
            # agent) = multi-binding ; sinon on prend l'identité du config (chemin legacy).
            legacy_id = config.pop("identity_id", None) if config else None
            if identity_ref is None:
                identity_ref = legacy_id or None
            # Binding à INSTANCE (ADR 0038 B5) : le lien désigne exactement UN credential
            # (ref B4). Validé + gardé AU LINK (le lieur doit avoir accès à l'instance ;
            # la résolution RE-gardera l'appelant). Exclusif d'identity_ref.
            if inp.op == "link" and inp.instance_ref:
                _require(identity_ref is None, "conflicting_binding",
                         "Donne `instance_ref` OU `identity_ref`, pas les deux "
                         "(le ref d'instance porte déjà le compte).")
                from ..mcp_errors import McpError
                from .. import access as access_mod, instance_refs
                try:
                    iref = instance_refs.parse_ref(inp.instance_ref)
                except ValueError:
                    _require(False, "invalid_instance_ref",
                             f"`instance_ref` invalide : {inp.instance_ref!r} "
                             "(un ref s'obtient via oto_instance(op='list')).")
                # Lot L6 : `inst:{id}` se PARSE mais ne se résout pas encore (L7).
                # Le refuser nommément évite le message « une instance `None` » que
                # produirait la garde de connecteur juste en dessous.
                _require(iref.level != "inst", "instance_not_pinnable",
                         "L'identifiant d'instance `inst:` n'est pas encore bindable : "
                         "repasse le `ref` rendu par oto_instance(op='list').")
                _require(iref.connector == target_ref, "instance_mismatch",
                         f"Ce ref est une instance `{iref.connector}`, pas "
                         f"`{target_ref}` (le connecteur du lien).")
                try:
                    access_mod.guard_instance_access(sub, iref)
                except McpError as e:
                    _require(False, "instance_forbidden", e.error.message, 403)
                config = dict(config or {})
                config["instance_ref"] = inp.instance_ref
            # Édition legacy (front actuel, pas d'identity_ref explicite) : s'il existe UN
            # binding unique avec une AUTRE identité, on le DÉPLACE (delete+insert) au lieu
            # d'en créer un 2e — préserve la sémantique « éditer le connecteur du projet ».
            if inp.op == "link" and inp.identity_ref is None:
                existing = [l for l in db.list_project_links(int(inp.project_id))
                            if l["target_type"] == "connecteur" and l["target_ref"] == target_ref]
                if len(existing) == 1 and existing[0].get("identity_ref") != identity_ref:
                    db.remove_project_link(int(inp.project_id), "connecteur", target_ref,
                                           identity_ref=existing[0].get("identity_ref"))
        if inp.op == "link" and inp.target_type in _RTYPE_CIBLE:
            _exiger_cible_dans_la_portee(sub, row, inp.target_type, str(target_ref))
        # ADR 0035 (B2) : nom de slot bindé par ce lien — validé (hygiène de clé) puis
        # unicité (projet, slot) imposée par la DB (ValueError slot_taken → 409).
        slot = None
        if inp.op == "link" and inp.slot is not None:
            from .. import slots as slots_mod
            try:
                slot = slots_mod.normalize_name(inp.slot)
            except ValueError as e:
                _require(False, "invalid_slot", str(e), 400)
        # Signal #883 — un lien d'avant la normalisation porte encore le NOM du tableau (le
        # SLUG de la procédure) : l'upsert, qui cherche l'id, ne le voyait pas et en créait
        # un second, sans slot, que l'unlink retirait ensuite avec le premier. Les écritures
        # stockées de la même entité se reconnaissent par la règle de l'unlink ; l'ancienne
        # est RÉÉCRITE en id si l'id n'est pas encore lié. S'il l'est déjà, deux liens
        # coexistent (créés avant ce correctif) : les fondre perdrait le slot de l'un, on
        # le DIT au lieu de choisir.
        rewrite_from, doublons = None, []
        if inp.op == "link" and inp.target_type in ("tableau", "procedure"):
            meme_binding = [l for l in db.list_project_links(int(inp.project_id))
                            if l.get("identity_ref") == identity_ref]
            ecritures = _unlink_refs(meme_binding, inp.target_type, target_ref,
                                     _ref_canonizer(row, proj_org, inp.target_type))
            autres = [r for r in ecritures if r != target_ref]
            if autres and target_ref not in ecritures:
                rewrite_from, doublons = autres[0], autres[1:]
            else:
                doublons = autres
        effet = None
        if inp.op == "link":
            try:
                effet = db.add_project_link(int(inp.project_id), inp.target_type, target_ref,
                                            inp.label, role=inp.role, config=config,
                                            identity_ref=identity_ref, slot=slot,
                                            rewrite_from=rewrite_from)
            except ValueError as e:
                code = "slot_taken" if str(e).startswith("slot_taken") else "bad_link"
                _require(False, code, str(e), 409 if code == "slot_taken" else 400)
        else:
            # #699 — un unlink ne mime plus un succès. Il vise TOUTES les écritures que la
            # réf demandée désigne (id canonique / nom-slug d'avant la normalisation), COMPTE
            # ce qu'il a retiré, et refuse nommément si ce compte est nul : `ok: true` sur un
            # no-op est pire qu'un refus — le lien restait dans les `links` de la réponse.
            demande = str(inp.target_ref).strip()
            existants = db.list_project_links(int(inp.project_id))
            refs = _unlink_refs(existants, inp.target_type, demande,
                                _ref_canonizer(row, proj_org, inp.target_type))
            removed = sum(db.remove_project_link(int(inp.project_id), inp.target_type, r,
                                                 identity_ref=identity_ref)
                          for r in refs)
            _require(removed, "link_not_found",
                     _rien_a_delier(existants, inp.target_type, demande), 404)
        db.log_project_activity(int(inp.project_id), sub, f"project.{inp.op}",
                                f"{inp.target_type}:{inp.label or target_ref}")
        out = {"ok": True, "id": inp.project_id,
               "links": db.list_project_links(int(inp.project_id))}
        if inp.op == "unlink":
            out["removed"] = removed
        else:
            # oto#119 — le link disait qu'il avait RÉUSSI, jamais ce qu'il avait FAIT :
            # créer le lien et le reposer à l'identique rendaient la MÊME réponse (ok +
            # la liste complète des liens). Un appelant automatique — « s'assurer que la
            # ressource est rattachée, la rattacher sinon » — ne pouvait donc pas savoir
            # s'il avait agi, et rapportait avoir corrigé ce qui n'avait rien à corriger.
            # Le geste reste idempotent et rejouable ; c'est son compte rendu qui cesse
            # d'être indistinct. `changed_fields` n'apparaît QUE sur une vraie réécriture.
            out["link_status"] = effet["status"]
            if effet["changed"]:
                out["changed_fields"] = effet["changed"]
            if effet.get("rewritten_from"):
                out["rewritten_from"] = effet["rewritten_from"]
            if doublons:
                out["duplicate_refs"] = doublons
                out["warning"] = (
                    "ce projet porte AUSSI cette entité sous une autre écriture ("
                    + ", ".join(f"« {r} »" for r in doublons)
                    + ") — un lien d'avant la normalisation en id, avec ses propres slot, "
                    "rôle et libellé : deux liens pour une même entité, et `unlink` les "
                    "retire TOUS. Pour n'en garder qu'un : `unlink`, puis `link` par l'id "
                    "en repassant `slot`, `role` et `label`.")
        # 0035 × 0046 — schéma CIBLE au binding d'un slot tableau : si une procédure
        # liée déclare ce slot avec un `schema`, un namespace vierge est PROVISIONNÉ
        # (le tableau naît avec son contrat — validation/lifecycle/clé) ; un schéma
        # déjà posé différent = warning non bloquant. Best-effort : jamais un link raté.
        if inp.op == "link" and inp.target_type == "tableau" and slot:
            try:
                from .. import slots as slots_mod
                target = slots_mod.target_schema_for(slot, out["links"])
                if target is not None:
                    res = slots_mod.provision_tableau_schema(
                        int(target_ref), target, project_id=int(inp.project_id))
                    out["slot_schema"] = res["status"]
                    if res.get("warning"):
                        out["warning"] = res["warning"]
            # noqa: SILENT — dette déclarée : le lot d'avertissements tombe d'un bloc (#424, verdict C)
            except Exception:  # noqa: BLE001 — provisionnement opportuniste
                pass
        # #218/#219 — lier un connecteur « aveugle » à un projet ORG-owned : si le
        # credential n'existe qu'au niveau d'une équipe de l'org (donc irrésoluble en
        # contexte projet), WARNING immédiat pointant le remède (transfert à l'équipe /
        # instance_ref). Non bloquant : on lie, mais l'intention incohérente est dite.
        if (inp.op == "link" and inp.target_type == "connecteur"
                and row.get("owner_type") == "org"):
            try:
                from .. import project_audit
                new_link = next((l for l in out["links"]
                                 if l.get("target_type") == "connecteur"
                                 and l.get("target_ref") == target_ref
                                 and l.get("identity_ref") == identity_ref), None)
                why = (project_audit._unresolvable_connector_why(
                    str(target_ref), int(row["owner_id"]), new_link)
                    if new_link else None)
                if why:
                    out["unresolvable_connector"] = why
                    out["warning"] = why
            # noqa: SILENT — dette déclarée : le lot d'avertissements tombe d'un bloc (#424, verdict C)
            except Exception:  # noqa: BLE001 — warning best-effort, le link a réussi
                pass
        # B5 — complétude au link : lier une procédure dont des slots ne sont pas
        # bindés ⇒ WARNING immédiat (non bloquant), le pendant des refs mortes 0014.
        if inp.op == "link" and inp.target_type == "procedure":
            try:
                from .. import org_store, project_audit
                instr = (org_store.get_instruction_by_id(int(target_ref))
                         if any(l.get("procedure_id") == int(target_ref)
                                for l in out["links"] if str(target_ref).isdigit())
                         else None)
                missing = project_audit.unbound_slots_for(instr, out["links"]) if instr else []
                if missing:
                    out["unbound_slots"] = missing
                    out["warning"] = (
                        f"la procédure `{instr['slug']}` déclare des slots non bindés dans ce "
                        f"projet : {', '.join(missing)} — binde chacun "
                        f"(`oto_project op=link project_id={inp.project_id} target_type=… "
                        "target_ref=… slot='<name>'`) avant de l'exécuter ici.")
            # noqa: SILENT — dette déclarée : le lot d'avertissements tombe d'un bloc (#424, verdict C)
            except Exception:  # noqa: BLE001 — warning best-effort, le link a réussi
                pass
        return out

    if inp.op in ("publish_mcp", "unpublish_mcp"):
        # Publier un endpoint MCP = acte de gouvernance (URL publique au nom de l'org).
        _require(ownership.can_govern(sub, RTYPE, rid), "forbidden",
                 "Publier un endpoint MCP est réservé au propriétaire / admin.", 403)
        if inp.op == "unpublish_mcp":
            return unpublish_project_mcp(sub, int(inp.project_id))
        # ⚠️ Le défaut était `anonymous` : publier sans rien préciser ouvrait le projet
        # au web ET le listait dans l'annuaire public. Un mode d'accès se choisit.
        _require(inp.mcp_access, "missing_mcp_access",
                 "`mcp_access` requis : 'org' (les membres de l'org, avec leur compte), "
                 "'secret' (URL non devinable, sans login, sans expiration) ou "
                 "'anonymous' (le web entier, et listé dans l'annuaire public). Il n'y "
                 "a pas de défaut raisonnable ici — le plus ouvert l'était.")
        if inp.mcp_access in ("anonymous", "secret"):
            quoi = "ce projet"
            if inp.mcp_expose_datastore is not False:
                quoi = "ce projet et les tableaux qui y sont liés"
            _publication.refuser_si_agent(
                ctx, quoi,
                "La publication se fait depuis le dashboard, sur la page du projet.")
        return publish_project_mcp(
            sub, row, access_mode=inp.mcp_access, mcp_slug=inp.mcp_slug,
            mcp_tools=inp.mcp_tools, expose_datastore=inp.mcp_expose_datastore,
            expose_datastore_write=inp.mcp_expose_datastore_write,
            expose_docs=inp.mcp_expose_docs,
            instructions_md=inp.mcp_instructions_md)

    if inp.op == "unarchive":
        # L'inverse de l'archivage (issue `oto`#38), même garde. Il n'existait pas, et
        # c'était un choix ASSUMÉ (oto-backend#929) : rompu sur décision d'Alexis du
        # 23/09/2026, comme il l'avait été pour les procédures le 10/09. Le seul retour
        # était un UPDATE SQL par un admin.
        _require(ownership.can_govern(sub, RTYPE, rid), "forbidden",
                 "Désarchivage réservé au propriétaire / admin.", 403)
        avant = db.unarchive_project(int(inp.project_id))
        if avant is not None:
            db.log_project_activity(int(inp.project_id), sub, "project.unarchive",
                                    row.get("name"))
        # `unarchived: false` n'est pas une erreur : remettre en service ce qui l'est
        # déjà est un non-geste. `was_archived_at` dit ce que le geste a ANNULÉ.
        return {"ok": True, "id": inp.project_id, "unarchived": avant is not None,
                "was_archived_at": str(avant) if avant is not None else None}

    # archive
    _require(ownership.can_govern(sub, RTYPE, rid), "forbidden",
             "Archivage réservé au propriétaire / admin.", 403)
    perdu = _ce_que_l_archivage_range(row)
    if (perdu["brief"] or perdu["procedures"]) and not inp.confirm:
        # Le cas vécu (oto#38) : un projet pris pour un doublon, archivé, portait dans
        # son brief les règles opérationnelles de la mission. Rien ne l'a dit.
        raise AuthzDenied(
            409, "confirm_required",
            f"Archiver #{inp.project_id} le retire de toutes les listes, avec ce qu'il "
            f"porte : {perdu['pages']} page(s), {perdu['procedures']} procédure(s) "
            f"liée(s), {perdu['links']} lien(s)"
            + (", et un brief non vide" if perdu["brief"] else "") + ". Rien n'est "
            "détruit, et `op=unarchive` le remet en service. Repasse `confirm=true` "
            "si c'est bien voulu.", {"unreachable": perdu})
    db.archive_project(int(inp.project_id))
    db.log_project_activity(int(inp.project_id), sub, "project.archive", row.get("name"))
    return {"ok": True, "id": inp.project_id, "archived": True, "unreachable": perdu}


def _ce_que_l_archivage_range(row: dict) -> dict:
    """Ce qu'un archivage retire de la vue (issue `oto`#38) : le compte des pages, des
    procédures liées, des liens, et la présence d'un brief. Rien n'est détruit — les
    procédures liées restent chez leur propriétaire — mais tout cela ne se retrouve
    plus par la liste des projets."""
    pid = int(row["id"])
    links = db.list_project_links(pid)
    return {"pages": len(db.list_docs_for_project(pid)),
            "procedures": sum(1 for l in links if l["target_type"] == "procedure"),
            "links": len(links),
            "brief": bool((row.get("brief_md") or "").strip())}


def _archived_in_org(ctx: ResolvedCtx) -> list[dict]:
    """Les projets ARCHIVÉS de l'org consultée (`op=list archived=true`, oto#38) : ses
    projets, ceux de ses équipes que je vois, et mes projets perso créés dans cette org
    (dans mon org perso, aussi ceux sans org de création) : le même périmètre possédé
    que la liste vivante
    (`ownership.project_list_owners`), sans les partages reçus (celui qui reçoit ne
    gouverne pas, il ne désarchiverait rien)."""
    sub = ctx.sub
    _require(bool(ownership.project_list_owners(sub, ctx.org_id)), "no_active_org",
             "Aucune org active.", 400)
    rows = ownership.projets_possedes_ici(sub, ctx.org_id, include_archived=True)
    rows = [r for r in rows if r.get("archived_at") is not None]
    comptes = db.project_grant_counts([r["id"] for r in rows])
    return [_view(r, sub, comptes.get(r["id"], 0)) for r in rows]


# ── Procédures liées, servies sur demande (#313) ─────────────────────────────

# ⚠️ **L'ALLOWLIST EST LE MÉCANISME DE SÉCURITÉ, pas une commodité de sérialisation.**
# La contrainte est produit et non négociable : une procédure servie ne dit RIEN de
# son exécution — ni le modèle qui l'exécute, ni ceux qu'on essaie. Le rendu est donc
# CONSTRUIT champ par champ à partir de cette liste, jamais dérivé de la ligne
# (`{**instr}`) : une colonne ajoutée demain à `org_instructions` n'a aucun chemin
# pour atteindre le client. C'est ce que fige `test_procedure_payload_is_an_allowlist`.
#
# `slots` en est absent à dessein, bien qu'inoffensif : c'est de la mécanique
# d'exécution (quelle instance branchée où), pas la règle que le lecteur veut lire.
#
# `description` y est entrée le 07/09/2026 (oto#118) — une DÉCISION, prise en changeant
# ce test, comme cette allowlist l'exige. C'est la prose de l'auteur : le résumé qu'il
# a saisi, corrigeable sans toucher au corps, et qui ne dit rien de l'exécution. Elle
# manquait, et une application tierce affichait à sa lectrice un corps de quinze mille
# caractères faute de pouvoir servir deux lignes. Servie « » (jamais `null`) quand
# l'auteur n'en a pas écrit : l'absence de résumé n'est pas une anomalie.
_PROCEDURE_FIELDS = ("ref", "slug", "title", "description", "version", "body_md")


def _linked_procedures(sub: str, links: list[dict]) -> list[dict]:
    """Le corps des procédures liées au projet, dans l'ordre des liens.

    **Chaque procédure est regardée par le seam `ownership`, une par une**, et ce
    n'est pas une précaution redondante avec le gate du projet : un projet peut lier
    une procédure d'une AUTRE org (partage cross-org par grant, #52). Lire le projet
    n'emporte donc pas le droit de lire tout ce qu'il désigne — une procédure
    inaccessible est simplement ABSENTE du rendu, sans erreur : elle reste visible
    comme lien (titre, ref), c'est son corps qui ne suit pas.

    Un lien mort (procédure supprimée) est sauté de la même façon — `audit.dead_links`
    est l'endroit qui le SIGNALE ; ce rendu-ci n'a pas à le redire, et surtout pas à
    faire échouer la lecture du projet pour autant."""
    out = []
    for l in links:
        if l.get("target_type") != "procedure":
            continue
        ref = str(l.get("target_ref") or "")
        if not ref.isdigit():          # l'ADR 0032 a fixé l'id ; un slug résiduel n'est pas résolu ici
            continue
        if not ownership.can_access(sub, "doctrine", ref, "read"):
            continue
        instr = org_store.get_instruction_by_id(int(ref))
        if not instr:
            continue
        out.append({"ref": ref, "slug": instr["slug"], "title": instr["title"],
                    "description": instr.get("description") or "",
                    "version": instr["version"], "body_md": instr["body_md"]})
    return out


class ProjectReadInput(BaseModel):
    """Lire UN projet, désigné par l'URL."""
    project_id: int
    # ⚠️ `| str` n'est pas une facilité, c'est la FORME RÉELLE de cette entrée : cette
    # capacité n'a qu'une face REST en GET, donc son `include` arrive de la QUERY
    # STRING, où une URL ne sait pas dire « liste ». L'adaptateur verse
    # `{"include": "procedures"}` — une chaîne — et un champ `list[str]` nu la refuse.
    # Conséquence vécue (#367) : `GET /api/me/projects/12?include=procedures`, la
    # requête littérale du besoin partenaire ET celle que la description de cette
    # capacité annonce, répondait `400 invalid_input` depuis sa livraison du 13/08
    # (c46d81e) — livrée, testée, et inatteignable. Les tests d'alors vérifiaient que
    # le champ était DÉCLARÉ ; ils décrivaient l'intention, pas le montage.
    # Même patron que `node_rows.filter`, pour la même raison.
    include: Optional[list[str] | str] = None

    @field_validator("include", mode="after")
    @classmethod
    def _en_liste(cls, v):
        """La query string normalisée UNE fois, ici, pour que le handler ne voie
        jamais qu'une liste.

        La virgule sépare une valeur unique ; la forme répétée (`?include=a&include=b`)
        arrive déjà en liste depuis l'adaptateur (#418, 29/08 — avant, il ne gardait
        que la DERNIÈRE valeur, et ce docstring disait la forme inutilisable). Les
        deux se combinent : chaque entrée est découpée à son tour."""
        if v is None:
            return None
        brut = v if isinstance(v, list) else str(v).split(",")
        return [m for m in (str(x).strip() for x in brut) if m]


class ProjectAudit(BaseModel):
    """Santé des LIENS du projet — un contrôle, pas un blocage : un projet dont
    l'audit est plein reste parfaitement lisible et utilisable.

    **Les quatre listes sont toujours présentes, vides quand il n'y a rien.** Mais
    « vide » ne prouve pas « sain » : l'audit est best-effort, chaque vérification
    est enveloppée et un lien qui fait lever la sienne est simplement SAUTÉ (log
    serveur, pas d'erreur ici). Un audit tout vert peut donc être un audit partiel —
    ne pas en faire un feu vert automatique.

    ⚠️ Ce modèle décrit l'audit COMPLET, celui de la lecture par id. La LISTE de
    projets sert le même nom sous une forme allégée (checks en mémoire seuls, pour
    éviter un N+1) : `inert_procedures` y est toujours vide et les procédures
    cassées n'y remontent pas. Deux payloads, deux profondeurs : celui de la liste
    (`include=["audit"]`) porte en plus `light: true`."""
    # Lien dont la cible n'existe plus : namespace de tableau disparu (ou nom de tableau
    # AMBIGU dans la portée du projet, #365), procédure qui ne résout plus, connecteur
    # absent du registre. `{target_type, target_ref, why}`.
    dead_links: list[dict]
    # Procédure liée dont des slots ne sont bindés par aucun lien de CE projet —
    # elle est exécutable mais incomplète. `{procedure, ref, slots}`.
    unbound_slots: list[dict]
    # Connecteur lié qu'AUCUN credential de l'org ne résoudrait aujourd'hui.
    # Calculé seulement pour un projet possédé par une ORG (le seul cas où « l'org »
    # désigne quelqu'un) : toujours vide sur un projet perso ou d'équipe — ce n'est
    # donc pas un signal d'absence de problème. `{target_ref, why}`.
    unresolvable_connectors: list[dict]
    # Slugs de procédures liées que les runs du projet n'ont jamais empruntées.
    # ⚠️ Reste vide tant que le projet n'a AUCUN run : sans historique, « inerte »
    # ne veut rien dire (un projet jeune n'est pas un projet mort).
    inert_procedures: list[str]


class ProjectRead(BaseModel):
    """UN projet lu par son id — exactement le payload d'`oto_project op=get`, même
    handler (ADR 0009 : la lecture par URL ne doit pas être une seconde
    implémentation qui dérive de la première).

    Deux choses que la forme ne dit pas d'elle-même :

    - **`can_write` est l'accès de l'APPELANT, recalculé à chaque lecture**, pas une
      propriété du projet. Un projet partagé en lecture seule le rend `false` alors
      que tout le reste du payload est identique à ce que voit son propriétaire —
      c'est le seul champ qui répond « et moi, j'ai le droit ? ».
    - **Un projet n'est lisible que DEPUIS l'org qui le possède** (gate de contexte,
      ADR 0023). Y accéder depuis une autre de ses orgs donne 404, pas 403 : le
      message dit alors de basculer d'org quand l'appelant y a bien accès, et reste
      non-disclosant sinon. Un 404 n'est donc PAS une preuve d'inexistence.

    Champs `mcp_*` : ils décrivent la PUBLICATION du projet (endpoint MCP dédié,
    partage navigable). `mcp_access == "off"` ⟹ rien n'est publié et `mcp_url` /
    `share_url` sont `null`."""
    id: int
    name: str
    # L'adresse web du projet chez CE lecteur (#599) — `null` quand son produit n'a
    # pas cette vue. Servie par le serveur, jamais reconstruite par l'appelant : un
    # patron d'URL appris par cœur fabrique des liens plausibles et faux le jour où
    # la route bouge. À ne pas confondre avec `mcp_url`/`share_url`, qui décrivent la
    # PUBLICATION du projet vers l'extérieur ; `url`, c'est où on le lit chez soi.
    url: Optional[str] = None
    icon: Optional[str] = None
    # Dossier d'affichage : les projets au même libellé se regroupent dans les
    # listes. `null` = hors dossier. Aucune hiérarchie ni règle d'accès.
    folder: Optional[str] = None
    brief_md: str = ""
    owner_type: str                              # user | org | group | platform
    owner_id: str
    # Org de CONTEXTE d'un projet PERSO (« moi, dans cette org ») — `null` pour tout
    # projet non-perso, dont le contexte se dérive de l'owner. Servi en CHAÎNE.
    context_org_id: Optional[str] = None
    is_template: bool = False
    mcp_slug: Optional[str] = None
    mcp_access: str = "off"                      # off | anonymous | secret | org
    mcp_tools: list[str] = []
    mcp_expose_datastore: bool = False
    mcp_expose_datastore_write: bool = False
    mcp_expose_docs: bool = False
    # Prose servie au DESTINATAIRE de l'endpoint — ≠ `brief_md`, qui reste interne.
    mcp_instructions_md: str = ""
    # Périmètre d'URL (#605) : motifs canoniques `hôte/chemin/` (ou `hôte/*`) que les
    # outils de recherche écartent et que les outils d'extraction refusent sous ce projet.
    excluded_url_prefixes: list[str] = []
    mcp_url: Optional[str] = None                # dérivé du slug + du mode, jamais stocké
    share_url: Optional[str] = None              # mode `secret` uniquement
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    archived_at: Optional[str] = None            # non-null ⟹ projet archivé
    # Droit d'écriture de l'APPELANT sur ce projet (cf. docstring).
    can_write: bool
    # Liens typés (tableau | procedure | connecteur) enrichis à la lecture :
    # `datastore` + `datastore_id` (tableau) / `title` (procédure) résolus, et
    # `cross_project: true` quand la même cible est liée par un AUTRE projet — la
    # toucher retombe ailleurs. ⚠️ `datastore` est un LIBELLÉ, `datastore_id` l'ADRESSE :
    # `target_ref` est tantôt l'un tantôt l'autre (#117), `datastore_id` porte toujours
    # le même sens ou n'est pas là (oto#160) ; `datastore_ambigu: true` quand le NOM
    # porté désigne plusieurs tableaux dans la portée du propriétaire (#365).
    links: list[dict]
    audit: ProjectAudit
    # Présent SEULEMENT si `include=['spine']` a été demandé — l'arbre des pages dans
    # l'ordre curé : `{pages, root_doc, tree[]}` (+ `more_roots` si des racines ont
    # été coupées). Borné (profondeur, 200 nœuds) : un nœud porte `more` = nombre de
    # descendants NON rendus. Une branche sans `children` n'est donc pas forcément
    # une feuille — vérifier `more`.
    spine: Optional[dict] = None
    # Présent SEULEMENT si `include=['procedures']` a été demandé (#313) — le CORPS
    # des procédures liées : `{ref, slug, title, description, version, body_md}`, et rien d'autre
    # (cf. `_PROCEDURE_FIELDS` : aucune métadonnée d'exécution, jamais).
    # ⚠️ La liste peut être plus COURTE que les liens de type `procedure` : une
    # procédure liée mais inaccessible à l'appelant (partage cross-org) ou supprimée
    # est absente ici tout en restant présente dans `links`. Apparier par `ref`.
    procedures: Optional[list[dict]] = None


def _project_read(ctx: ResolvedCtx, inp: ProjectReadInput) -> dict:
    """`op=get`, servi par une URL qui NOMME son projet.

    Même handler, mêmes gates : la lecture par id ne doit pas être une seconde
    implémentation qui dérive de la première (c'est tout l'objet de l'ADR 0009).

    Pourquoi une route de plus, alors que `POST /api/me/projects {"op":"get"}` fait
    déjà le travail : parce qu'un jeton PORTÉ ne peut pas être borné sur ce POST —
    sa cible vit dans le corps, et `token_scopes` ne lit que la méthode et le
    chemin. Une intégration à qui l'on confie un projet (et lui seul) a besoin que
    le projet s'adresse. C'est le même geste que pour les tableaux, qui se nomment
    déjà dans l'URL.
    """
    return _project(ctx, ProjectInput(op="get", project_id=inp.project_id,
                                      include=inp.include))


CAPABILITIES += [
    Capability(
        key="me.project_read", handler=_project_read, Input=ProjectReadInput,
        authz=SUB_ONLY, Output=ProjectRead,
        description=(
            "Read ONE project by id (its brief, links and audit) — the same payload as "
            "oto_project op=get, on a URL that names its target. REST-only: agents "
            "already have oto_project. Exists so a SCOPED api token can be granted one "
            "project and nothing else ({\"projects\": {\"12\": \"read\"}}), which the "
            "POST form cannot express — its target sits in the body. "
            "Optional ?include=procedures adds the BODY of the linked procedures "
            "(title, description = the author's own short summary, version, body_md) so "
            "a reader can render the summary instead of the whole body, and see the rule that produced a "
            "record; omitted, the response is byte-for-byte unchanged. Ask for "
            "several with ONE comma-separated value (?include=spine,procedures) — "
            "repeating the parameter keeps only the last one."
        ),
        mcp=None,
        # `:int` et non `{project_id}` nu : le motif de portée (`token_scopes`)
        # ne reconnaît qu'un id numérique. Une route plus permissive que sa
        # portée se laisserait atteindre pour être refusée juste après.
        rest=RestBinding("GET", "/api/me/projects/{project_id:int}"),
    ),
    Capability(
        key="me.project", handler=_project, Input=ProjectInput, authz=SUB_ONLY,
        description=(
            # ⚠️ ORDRE FONCTIONNEL, pas rédactionnel. `tool_registry.blurb()` sert le
            # 1er PARAGRAPHE borné (100 c. pour le catalogue `oto_list_my_tools`, 140
            # pour le registre), coupé à la dernière phrase complète : ouvrir sur le
            # champ `url` (~275 c.) poussait `op=create` HORS du résumé SERVI.
            # oto_project était alors le seul conteneur de la plateforme dont le
            # résumé cachait sa création — une recherche `create` ne le rendait pas, et
            # une utilisatrice s'est vu proposer un tableau plutôt qu'un projet
            # (09/09/2026). Les ops passent donc en tête ; banc :
            # tests/test_resume_nomme_ses_ops.py.
            "Projects (organization layer). op=create (name, "
            "optional brief_md; owner_type user|org + owner_id for a team project) / list "
            "(ORG-SCOPED: the ACTIVE org's projects, its teams' + projects shared with it "
            "or with your teams in it + YOUR personal projects created in it (visible to "
            "you only) — a personal project is listed ONLY in the org where it was created; "
            "your PERSONAL org also lists those shared with YOU as a person; pass "
            "`org=<id>` to see another org's; "
            "`scope=\"me\"` lists only the projects shared with you as a person, in any "
            "org; every response echoes the effective org in `_org`. An INDEX: names and `brief_md_length`, NOT the briefs — "
            'read one with op=get, or pass `fields=["*"]` for whole records; '
            '`include=["links"]` adds every project\'s links in the same call, '
            '`include=["audit"]` its LIGHT audit, flagged `light: true`) / '
            "list_templates (published MODEL projects you can copy, from the ACTIVE org "
            "and the platform library) / "
            "get (project + its links + an `audit` of those links: dead_links / unbound_slots / "
            "unresolvable_connectors (a linked connector no org credential would resolve — "
            "org-owned projects only, always empty otherwise) / "
            "inert_procedures — a linked entity that no longer resolves surfaces HERE, act on it) / "
            "update (name, icon = an emoji shown in the lists and headers (\"\" clears it), folder = a display "
            "label the lists group projects under (\"\" clears it), brief_md, is_template = publish/unpublish "
            "as a copyable model, excluded_url_prefixes = URL prefixes such as `linkedin.com/in/` "
            "that search tools drop and extraction tools refuse under this project — a whole "
            "host must be written `host/*`, `[]` clears, mcp_instructions_md = the prose "
            "the published endpoint serves its recipients, fixed WITHOUT republishing — "
            "slug, access and tools stay as they are; owner/admin only) / copy (deep-copy a project you can read — its own or a model "
            "— into a NEW project in your active org: brief + doc tree + links + raw files; "
            "a tableau link stays a POINTER to the same namespace by default (config.provision "
            "absent/`shared`), but with config.provision=`empty`|`seeded` it is PROVISIONED — a "
            "FRESH namespace (same schema, rows only if `seeded`) so each copy gets its own "
            "isolated table (e.g. a campaign template's lead pool). A `shared` tableau owned by "
            "ANOTHER org is re-provisioned EMPTY (never a pointer to the source's private data), "
            "and links whose namespace no longer resolves are skipped — both surfaced in the "
            "response `warnings`. Pass project_id = source + name = target) / handoff (a copy-paste « resume in Claude » blob "
            "that pre-writes the per-call `_project=` token for this project) / archive (hides it "
            "from every list, destroys nothing; the response says what became unreachable — "
            "pages, linked procedures, links, brief — and a project with a non-empty brief or "
            "a linked procedure needs `confirm=true`, else 409 `confirm_required`) / "
            "unarchive (puts an archived project back; `was_archived_at` echoes what it "
            "undid; find archived ones with op=list `archived=true`) / link & unlink "
            "(attach an entity: "
            "target_type tableau|procedure|connecteur + target_ref = its id/slug/name, "
            "optional label + optional "
            "role = why this entity belongs to the project + optional config = the entity's "
            "PRE-MADE per-project override; for a connecteur: {identity_id?, instructions_md?} "
            "= which account to act as + prose instructions to apply (e.g. 'only filter "
            "agreements by the mutuelle theme'), or `instance_ref` (a ref from "
            "oto_instance op=list, ADR 0038 B5) to bind EXACTLY that credential — calls "
            "carrying this project's token then resolve it hard, no fallback; "
            "for a tableau: {provision?: shared|empty|seeded} "
            "= how a project copy treats it (empty/seeded = each copy gets its own fresh table). "
            "Optional `slot` = the SLOT NAME this link BINDS for the project (ADR 0035): "
            "procedures declare required entities as slots and reference them <slot:name> "
            "in their prose — the project maps each name to a concrete entity via its links. "
            "Slot names are a PROJECT-wide vocabulary (unique per project → 409 slot_taken; "
            "two linked procedures sharing `sortie` share the binding). "
            "Re-linking preserves every field you omit — label, role, config and slot; "
            "pass a value to change one. link says WHAT IT DID in `link_status`: `created` "
            "(the binding did not exist), `unchanged` (it was already there and this call "
            "rewrote nothing) or `updated` (it existed and this call changed it — "
            "`changed_fields` then lists which of label/role/slot/config/target_ref "
            "moved). Re-running a link is safe "
            "and idempotent, so a caller told to ENSURE a resource is attached must read "
            "`link_status` — not `ok` — to know whether it actually acted. "
            "unlink returns `removed` = how many bindings it actually took "
            "out, and REFUSES (`link_not_found`) when it matched none — it never answers ok "
            "on a link it did not find. Give the `target_ref` as op=get renders it: an older "
            "link may still carry the NAME of its tableau (or the SLUG of its procedure) "
            "instead of the id. link and unlink both recognize either spelling: link "
            "rewrites that older link to the id (`rewritten_from` = the old spelling) "
            "instead of adding a second one, unlink takes back every spelling. "
            "get/link return each link's role + slot + config + a derived "
            "`cross_project` flag (the same entity is linked by another project → avoid brutal "
            "edits / ask); a tableau link also returns its resolved `datastore` (the NAME, a "
            "label) and `datastore_id` (the IDENTIFIER, resolved server-side in the PROJECT "
            "OWNER's scope) — address THIS project's table with `datastore_id` in the data_* "
            "tools, never by hardcoding a name: several tables can carry one name, and at equal "
            "name resolution prefers the CALLER's own personal table. No `datastore_id` = this "
            "link does not resolve to a single table here (`datastore_ambigu: true` = its "
            "NAME designates several tables in the owner's scope) — say so instead of "
            "guessing. "
            "EVERY project carries `url` — the web address to OPEN it, in the reader's "
            "own product; hand it over as-is when asked \"where is it?\", never rebuild "
            "one from a pattern (`null` = that reader's product has no such view). "
            "Share & transfer go through oto_resource (resource_type='project', ADR 0030 "
            "owned resource) — this "
            "includes RE-PARENTING a project in place (same id, links, runs preserved): "
            "op=transfer new_owner_group=<id> hands it to a TEAM so the project and its "
            "connector credentials sit at the SAME level (the team's secrets then resolve "
            "when you open it), new_owner_org=<id> to an org, new_owner_email to a user. "
            "(op=update only changes name/icon/folder/brief_md/is_template — never the owner; op=copy "
            "makes a NEW id.) "
            "inventory = the project's DERIVED surface (union of the linked procedures' "
            "<tool:> refs + tools actually used by the project's runs, plus connectors "
            "from links & declared slots) — never retype a tool list: derive, then curate. "
            "runs (optional target_ref = a linked procedure's stable id) = the project's "
            "recent runs (label/guide/outcome), filtered to that procedure when given. "
            "OMIT project_id on op=runs and you get YOUR OWN still-open runs instead, "
            "each with its `run_id` — that is how you find a run you opened and lost "
            "the id of, so you can finally close it with run_finish. Across every org, "
            "since a run you cannot find is usually one you opened elsewhere. "
            "lint (optional stale_days, default 90) = health of this project's pages: "
            "stale (untouched since), empty (trivial body), duplicate_titles (likely merges). "
            "publish_mcp (mcp_slug + mcp_access anonymous|secret|org + mcp_tools = the fixed "
            "tool allowlist) publishes the project as a dedicated MCP endpoint "
            "`<mcp_slug>.mcp.oto.cx/mcp`, the toolset served under the OWNER ORG's credentials — "
            "`anonymous` = no login + LISTED in the public directory; `secret` = no login but "
            "UNLISTED, the slug is server-generated & unguessable (a secret URL; mcp_slug is an "
            "optional readable prefix); `org` = Logto JWT + pins the org. For anonymous/secret, "
            "tools that aren't credential-less or resolvable for the org are published anyway but "
            "FAIL cleanly at call time — they come back in `mcp_unresolvable_tools` (configure an "
            "org key or drop them). mcp_expose_datastore (SECRET only) opts the `data_*` tools "
            "in: they then act under the OWNER ORG's authority (read/write the org's namespaces) "
            "without a login — off by default (the datastore stays private); refused on "
            "anonymous/org. Publishing takes governing the project AND being a member of "
            "the org that owns it (the parent org of an owning team; the owner themself "
            "for a personal project) — a manager from outside is refused 403 "
            "`publish_requires_membership`. unpublish_mcp removes it, open to whoever "
            "governs. get returns "
            "mcp_slug/mcp_access/mcp_tools/mcp_expose_datastore/mcp_url."
        ),
        errors=(DeclaredError(409, "confirm_required",
                              "op=archive sur un projet qui porte un brief ou une "
                              "procédure liée, sans `confirm=true` — rien n'a été "
                              "archivé ; `details.unreachable` dit ce qui l'aurait été"),
                _publication.REFUS_HORS_ORG),
        mcp="oto_project",
        rest=RestBinding("POST", "/api/me/projects"),
    ),
]


# ── « Projet actif » = jeton d'appel (ADR 0038 B3b — le bracelet est retiré) ──
# `oto_use_project` ne pose PLUS d'état de session : le contexte projet est porté
# par le jeton `_project=` de CHAQUE appel de travail (l'axe co-pose l'org du projet,
# résout les slots et épingle les identités connecteur préfaites). Ce tool valide
# l'accès et renvoie le geste fiable + les surcharges préfaites (informatif).


class UseProjectInput(BaseModel):
    project_id: int   # id d'un projet auquel tu as accès (cf. oto_project op=list)


class NoInput(BaseModel):
    pass


def _use_project(ctx: ResolvedCtx, inp: UseProjectInput) -> dict:
    """Hint SANS ÉTAT (ADR 0038 B3b) : valide l'accès au projet et renvoie le geste
    fiable (`_project=` par appel) + ses surcharges connecteur préfaites."""
    row = db.get_project_by_id(inp.project_id)
    _require(row is not None, "unknown_project", f"Projet #{inp.project_id} inconnu.", 404)
    _require_active_org_visible(ctx, row)
    # Surcharges connecteur préfaites portées par ce projet (informatif pour l'agent).
    overrides = [{"connector": l["target_ref"], "config": l.get("config") or {}}
                 for l in db.list_project_links(inp.project_id)
                 if l.get("target_type") == "connecteur" and (l.get("config") or {})]
    return {
        "project": inp.project_id, "name": row.get("name"),
        "connector_overrides": overrides, "session_state": None,
        "how_to": (f"Aucun état de session (ADR 0038) : passe `project={inp.project_id}` "
                   "sur CHAQUE appel de travail fait pour ce projet (connecteurs et "
                   "data_* l'acceptent — l'org du projet, ses slots et ses identités "
                   f"préfaites en découlent). Puis recharge le contexte de l'org du "
                   f"projet (readme d'org+équipe, guides, procédures — figé à la "
                   f"connexion) via `oto_context(project={inp.project_id})`."),
    }


def _clear_project(ctx: ResolvedCtx, inp: NoInput) -> dict:
    """Hint sans état (ADR 0038 B3b) : hors projet = simplement ne pas passer `_project=`."""
    return {"session_state": None,
            "how_to": ("Aucun état de session à effacer (ADR 0038) : un appel sans "
                       "`_project=` est hors projet par construction.")}


CAPABILITIES += [
    Capability(
        key="me.use_project", handler=_use_project, Input=UseProjectInput, authz=SUB_ONLY,
        description=(
            "Resolve a project you can access (project_id from oto_project op=list) and "
            "get the RELIABLE way to work in it. NO session state (ADR 0038): pass "
            "`project=<id>` directly on each work call — the project's org, slot "
            "bindings and PRE-MADE connector identities all derive from that token. "
            "Returns the project's connector overrides."
        ),
        mcp="oto_use_project",
    ),
    Capability(
        key="me.clear_project", handler=_clear_project, Input=NoInput, authz=SUB_ONLY,
        description=("No-op hint (ADR 0038: no session state — a call without "
                     "`_project=` is out of any project by construction)."),
        mcp="oto_clear_project",
    ),
]
