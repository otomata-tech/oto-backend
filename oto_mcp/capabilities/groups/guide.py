"""Guide & skills d'un GROUPE (ADR 0012) — éditeur self-service de l'équipe.

Miroir du guide d'org au grain groupe. La garde suit le VERBE, pas la surface (#681) :
lecture, écriture et restauration = **membre** du groupe (`GROUP_MEMBER_OF`) ;
suppression = **chef d'équipe** (`GROUP_ADMIN_OF`, escalade org_admin/platform).
Éditer une procédure, c'est faire son travail, et le geste est réversible — chaque
écriture ajoute une version, `revert` en restaure une. Supprimer emporte l'historique
sans corbeille : c'est le seul geste que rien ne défait, et le seul réservé au chef.

Deux gardes ⟹ deux droits SERVIS : `GET /api/groups/{id}/instructions` rend
`can_write_instructions` et `can_delete_instructions`, chacun calculé par la règle
d'autz de sa capacité. Un drapeau unique remettrait « qui peut annoter » dans les mains
d'un écran, et c'est exactement ce que #695 vient de retirer.

⚠️ Ces routes et `oto_procedure(op='set', scope='group')` écrivent la MÊME procédure
(même store, même clé `(owner_type, owner_id, slug)`) : leurs gardes se déplacent
ENSEMBLE, sinon « qui peut annoter » devient une propriété du transport.

Modèle versionné (slug réservé `claude_md` = guide de base servi en complément de celle
de l'org par `oto_procedure(op='get')`). Édité par le dashboard via REST
`/api/groups/{id}/instructions*`.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from ... import (deprecations, guide_store, org_store, procedure_diagram, roles,
                 tool_alias)
from .._authz import GROUP_ADMIN_OF, GROUP_MEMBER_OF, capacite_autorise
from .._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from ..registry import CAPABILITIES

_GID = {"id": "group_id"}
_GID_SLUG = {"id": "group_id", "slug": "slug"}
_BASE = org_store.BASE_SLUG

# ── Les droits ANNONCÉS par le bundle ────────────────────────────────────────
#
# Chaque drapeau NOMME la capacité dont il rend la règle, et c'est cette règle-là
# qu'on exécute (`capacite_autorise`) — jamais une seconde condition écrite ici. Le
# défaut réparé : `can_edit` recopiait le critère de l'administration, la garde
# d'écriture est descendue au membre (#695), et l'écran s'est mis à cacher un geste
# permis sans qu'une ligne de ce fichier ait bougé. Déplacer une garde déplace
# désormais son drapeau avec elle.
#
# ⚠️ Deux entrées et non une : `set` et `delete` n'ont pas la même garde. Un drapeau
# unique redeviendrait soit une porte fermée à tort (l'écriture), soit une porte
# ouverte à tort (le bouton de suppression, que le serveur refuse).
_DROITS_SERVIS = {
    "can_write_instructions": "group.instruction.set",
    "can_delete_instructions": "group.instruction.delete",
}


class GroupIdInput(BaseModel):
    group_id: int


class InstrGetInput(BaseModel):
    group_id: int
    slug: str
    version: Optional[int] = None


class InstrSetInput(BaseModel):
    group_id: int
    slug: str
    body_md: str
    title: Optional[str] = None
    description: Optional[str] = None


class InstrSlugInput(BaseModel):
    group_id: int
    slug: str


class InstrRevertInput(BaseModel):
    group_id: int
    slug: str
    version: int


# --- Sorties ----------------------------------------------------------------
#
# ⚠️ Ce palier est le MIROIR du guide d'org (`orgs_instructions`), mais le
# miroir est INFIDÈLE sur trois points qu'un front factorisé prendra de plein fouet :
#   · `list` rend `doctrine` en **chaîne brute** ici, en **objet** `{exists, version,
#     updated_at}` côté org — même nom de clé, deux types ;
#   · les écritures d'org portent un `ok`, celles d'équipe **n'en portent pas** ;
#   · `delete` d'un slug inconnu rend `deleted: false` en 200 ici, un **404** côté org.
# Les modèles ci-dessous décrivent le palier ÉQUIPE ; ne pas déduire l'autre de l'un.

class GroupInstructionIndexEntry(BaseModel):
    """Métadonnées d'une procédure d'équipe — **sans le corps** (`body_md` s'obtient par
    `GET /api/groups/{id}/instructions/{slug}`)."""
    id: int
    slug: str
    title: Optional[str] = None
    description: Optional[str] = None
    version: int
    updated_at: Optional[str] = None


class GroupInstructionsBundle(BaseModel):
    """Readme d'équipe + index de ses procédures.

    ⚠️ **Chaque clé est servie sous DEUX noms** le temps du préavis (#519) :
    `guide`/`guide_version` (aujourd'hui) et `doctrine`/`doctrine_version`, qui s'en
    vont le 29/10/2026 — cf. `docs/alias-deprecies.md`.

    ⚠️ **`guide` est le corps MARKDOWN BRUT, pas un objet de métadonnées** — c'est
    l'écart de forme avec le bundle d'org, qui rend là un `{exists, version,
    updated_at}`. Chaîne vide = pas de readme.

    ⚠️ **`guide_version` est un faux compteur** : il vaut `1` s'il existe un readme,
    `null` sinon, et n'atteint JAMAIS 2. Le readme d'équipe est de la prose plate sans
    historique (ADR 0042) ; l'afficher comme un numéro de révision promet un versionnage
    qui n'existe pas — et `POST …/revert` sur ce slug ne le rembobinera pas.

    ⚠️ **Un readme vide peut être un incident, pas une absence** : sa lecture est
    **fail-open** — une erreur de base rend `null`, donc exactement `guide: ""` +
    `guide_version: null`, indiscernable d'une équipe qui n'a rien écrit. Ne pas
    proposer « créer le readme » sur cette seule foi si l'action écrase.

    ⚠️ `instructions` **exclut le readme** (slug réservé `claude_md`). L'asymétrie va
    plus loin : le readme annoncé par `guide` est **introuvable** par
    `GET …/instructions/claude_md` (404) — il vit sur la surface guide, pas ici.

    ⚠️ **`can_edit` est le seul champ de tout le domaine `group.*` qui dit la vérité sur
    les droits** : il intègre l'escalade (chef d'équipe, org_admin parent,
    platform_admin), là où `GroupBrief.my_role` ne rend que l'appartenance explicite.
    `can_edit: true` avec `my_role: null` n'est pas une incohérence — c'est un org_admin.

    ⚠️ **`can_edit` ne dit RIEN du droit d'écrire une procédure** — c'est le droit
    d'ADMINISTRER l'équipe : le readme (`guide`, écrit sur la surface guide), les
    membres, les secrets partagés, et la suppression d'une procédure. Depuis #695
    l'écriture n'en fait plus partie. **Les boutons d'une procédure se conditionnent à
    `can_write_instructions` et `can_delete_instructions`, jamais à `can_edit`** : un
    écran qui masque l'édition sur ce champ masque un geste permis à tout membre.
    `can_edit` n'est pas déprécié pour autant — il reste la réponse juste aux gestes
    d'administration de l'équipe."""
    group_id: int
    guide: str
    guide_version: Optional[int] = None
    doctrine: str                          # ALIAS déprécié (retrait 29/10/2026)
    doctrine_version: Optional[int] = None  # ALIAS déprécié (retrait 29/10/2026)
    instructions: list[GroupInstructionIndexEntry]
    can_edit: bool
    # Les deux droits sur les procédures ci-dessus, chacun rendu par la règle d'autz
    # de SA capacité (cf. `_DROITS_SERVIS`) — pas par une condition recopiée ici.
    can_write_instructions: bool = Field(description=(
        "Peut créer, modifier et restaurer une procédure de cette équipe "
        "(`PUT`/`revert`) : vrai pour tout MEMBRE. Le geste est réversible — chaque "
        "écriture ajoute une version."))
    can_delete_instructions: bool = Field(description=(
        "Peut supprimer une procédure de cette équipe ET tout son historique "
        "(irréversible) : vrai pour le CHEF d'équipe seulement. Toujours servi à côté "
        "de `can_write_instructions`, dont il diffère."))


class GroupInstructionView(BaseModel):
    """Une procédure d'équipe, corps compris. `slug` est le slug NORMALISÉ (l'entrée est
    tolérante).

    ⚠️ **Demander `?version=N` rend un objet plus PETIT** : la version archivée est
    servie depuis la table des révisions, qui ne porte ni `id` ni `updated_at`. Ces deux
    champs absents ne signalent donc ni une procédure sans identité ni une procédure
    jamais modifiée — seulement qu'on lit un instantané.

    ⚠️ Le slug réservé `claude_md` rend **404** ici, même quand
    `GET /api/groups/{id}/instructions` vient d'annoncer un readme : les deux surfaces ne
    lisent pas le même stockage.

    ⚠️ **Aucun contrôle de références au niveau équipe** : contrairement à la procédure
    d'org, le corps n'est pas confronté au registre d'outils vivants. Un `<tool:slug>`
    mort y reste, silencieusement — rien dans cette réponse ne le signale."""
    group_id: int
    # Absent quand on lit une version archivée (cf. ci-dessus).
    id: Optional[int] = None
    slug: str
    title: Optional[str] = None
    description: Optional[str] = None
    version: int
    body_md: str
    # ADR 0035 : entités requises déclarées. Servi depuis #681 — jusque-là le store
    # d'équipe écrivait `[]` en dur et ne les relisait pas ; une procédure d'équipe peut
    # désormais en porter (écriture par `oto_procedure op=set scope=group`), et une vue
    # qui les tairait laisserait croire qu'elle n'en a pas.
    slots: list = []
    set_by: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class GroupInstructionWritten(BaseModel):
    """Écriture d'une procédure d'équipe. Chaque écriture **incrémente la version** et
    archive un instantané ; il n'y a pas de mise à jour en place — `version` est donc le
    NOUVEAU numéro, jamais celui qu'on croit poser.

    ⚠️ **Pas de champ `ok` ici** (l'équivalent d'org en a un) : le témoin de succès est
    `set`, une constante qui vaut toujours `true` quand la réponse existe. Un client qui
    teste `response.ok` lira `undefined` et conclura à un échec.

    ⚠️ **Écrire le slug réservé `claude_md` n'est pas refusé proprement** : le store lève,
    et la capacité ne traduit pas — l'appelant reçoit une erreur serveur au lieu d'un 400
    actionnable. Le readme d'équipe s'écrit sur la surface guide
    (`scope='group', delivery='init'`), pas ici.

    `title`/`description` omis **conservent** l'existant (ce n'est pas un effacement) ;
    ils ne sont pas réécho, donc relire l'index pour confirmer."""
    group_id: int
    # Slug NORMALISÉ (minuscules, [a-z0-9_-]) — peut différer de l'entrée.
    slug: str
    # Le NOUVEAU numéro de version.
    version: int
    # Constante d'écho : vaut toujours `true`.
    set: bool
    # Le SCHÉMA manquant. `None` = rien à signaler.
    diagram_warning: Optional[str] = None


class GroupInstructionDeleted(BaseModel):
    """Suppression d'une procédure d'équipe **et de tout son historique** — irréversible,
    aucune corbeille.

    ⚠️ **`deleted: false` est un 200, pas un 404** : supprimer un slug inexistant réussit
    et le dit par ce booléen. C'est l'inverse exact du palier org, où l'absence lève un
    404 et où `deleted` ne vaut jamais `false`. Le même nom de champ n'a donc pas le même
    statut aux deux étages : ici il faut le lire, là-bas il ne dit rien.

    ⚠️ Pas de champ `ok` (asymétrie avec l'org, cf. `GroupInstructionWritten`)."""
    group_id: int
    slug: str
    deleted: bool


class GroupInstructionVersion(BaseModel):
    """Une révision archivée — métadonnées seules, sans le corps (`?version=N` sur la
    lecture pour l'obtenir)."""
    version: int
    title: Optional[str] = None
    set_by: Optional[str] = None
    created_at: Optional[str] = None


class GroupInstructionVersions(BaseModel):
    """Historique d'une procédure d'équipe, plus récente d'abord.

    ⚠️ **Une liste vide recouvre trois situations distinctes**, toutes en 200 : le slug
    n'existe pas (aucun 404 n'est levé ici), c'est le readme (`claude_md`, retourné vide
    par construction — il n'a pas d'historique), ou la procédure n'a encore aucune
    révision archivée. Il faut `GET …/instructions/{slug}` pour trancher.

    `slug` est renvoyé normalisé, y compris quand la liste est vide — ce n'est pas une
    confirmation d'existence."""
    group_id: int
    slug: str
    versions: list[GroupInstructionVersion]


class GroupInstructionReverted(BaseModel):
    """Restauration d'une version passée.

    ⚠️ **`version` est un numéro NEUF, pas celui qu'on restaure** : revenir à la v2 d'une
    procédure en v6 produit une v7 dont le contenu est celui de la v2. L'historique n'est
    jamais rembobiné — `reverted_from` est la seule trace de l'intention.

    ⚠️ Pas de champ `ok` (asymétrie avec l'org).

    ⚠️ Un revert sur le readme (`claude_md`) rend **404 `unknown_version`** quelle que
    soit la version demandée — un code trompeur : ce n'est pas la version qui manque,
    c'est que ce slug n'a jamais d'historique ici."""
    group_id: int
    slug: str
    version: int
    reverted_from: int


def _list(ctx: ResolvedCtx, inp: GroupIdInput) -> dict:
    # Readme d'équipe = guide `delivery='init'` (ADR 0042), lu sur sa surface.
    base_body = guide_store.init_guide_body("group", inp.group_id) or ""
    return deprecations.avec_les_deux_noms({
        "group_id": inp.group_id,
        "guide": base_body,
        "guide_version": 1 if base_body else None,
        "instructions": org_store.list_instructions("group", inp.group_id),
        # Droit d'ADMINISTRER l'équipe (readme, membres, secrets) — inchangé.
        "can_edit": roles.can_admin_group(ctx.sub, inp.group_id),
        # Droits sur les PROCÉDURES : rendus par les règles d'autz déclarées, pas
        # recalculés — ce qu'on annonce ici est ce qui refuse là-bas.
        **{nom: capacite_autorise(cle, ctx.sub, group_id=inp.group_id)
           for nom, cle in _DROITS_SERVIS.items()},
    })


def _get(ctx: ResolvedCtx, inp: InstrGetInput) -> dict:
    instr = org_store.get_instruction("group", inp.group_id, inp.slug, inp.version)
    if not instr:
        raise AuthzDenied(404, "unknown_instruction",
                          f"Instruction `{org_store.normalize_slug(inp.slug)}` absente.")
    # Projection EXPLICITE : le store unifié rend aussi le propriétaire et l'org parente
    # (`owner_type`/`owner_id`/`org_id`), qui sont de la plomberie — cette face-ci ne
    # publie que ce que `GroupInstructionView` déclare.
    return {"group_id": inp.group_id, "id": instr.get("id"), "slug": instr["slug"],
            "title": instr.get("title"), "description": instr.get("description"),
            "version": instr["version"], "body_md": instr["body_md"],
            "slots": instr.get("slots") or [], "set_by": instr.get("set_by"),
            "created_at": instr.get("created_at"), "updated_at": instr.get("updated_at")}


def _set(ctx: ResolvedCtx, inp: InstrSetInput) -> dict:
    if not (inp.body_md or "").strip():
        raise AuthzDenied(400, "empty_body", "body_md vide.")
    slug = org_store.normalize_slug(inp.slug)
    if not slug:
        raise AuthzDenied(400, "invalid_slug", "slug vide ou invalide ([a-z0-9_-]).")
    # Le readme d'équipe est réservé (ADR 0042) : le store le refuse déjà, mais par un
    # `ValueError` que rien ne traduit — l'appelant recevait un 500 pour une entrée
    # refusée. On déclare le refus ici plutôt que d'élargir l'adaptateur à `ValueError` :
    # un refus métier se déclare, il ne se déduit pas d'un type d'exception (#281).
    if slug == org_store.BASE_SLUG:
        raise AuthzDenied(
            400, "reserved_slug",
            f"`{org_store.BASE_SLUG}` est le readme d'équipe, pas une procédure — "
            "écris-le via la surface guide (`oto_guide` scope='group', delivery='init').")
    # Outils cités au canonique, comme au grain org (`orgs.instructions`).
    body_md = tool_alias.canonical_prose(inp.body_md, ctx.sub)
    version = org_store.set_instruction(
        "group", inp.group_id, slug, body_md, title=inp.title,
        description=inp.description, set_by=ctx.sub)
    # Une procédure d'équipe est une procédure : même exigence de schéma qu'au grain org
    # (front tiers, issue #108), même régime — un warning, jamais un refus.
    return {"group_id": inp.group_id, "slug": slug, "version": version, "set": True,
            **procedure_diagram.diagram_check(body_md)}


def _delete(ctx: ResolvedCtx, inp: InstrSlugInput) -> dict:
    deleted = org_store.delete_instruction("group", inp.group_id, inp.slug)
    return {"group_id": inp.group_id, "slug": org_store.normalize_slug(inp.slug),
            "deleted": deleted}


def _versions(ctx: ResolvedCtx, inp: InstrSlugInput) -> dict:
    return {"group_id": inp.group_id, "slug": org_store.normalize_slug(inp.slug),
            "versions": org_store.list_instruction_versions("group", inp.group_id, inp.slug)}


def _revert(ctx: ResolvedCtx, inp: InstrRevertInput) -> dict:
    old = org_store.get_instruction("group", inp.group_id, inp.slug, inp.version)
    if not old:
        raise AuthzDenied(404, "unknown_version",
                          f"Pas de version {inp.version} pour `{org_store.normalize_slug(inp.slug)}`.")
    new_version = org_store.set_instruction(
        "group", inp.group_id, inp.slug, old["body_md"], title=old["title"],
        description=old["description"], set_by=ctx.sub, slots=old.get("slots") or [])
    return {"group_id": inp.group_id, "slug": org_store.normalize_slug(inp.slug),
            "version": new_version, "reverted_from": inp.version}


CAPABILITIES += [
    Capability(
        key="group.instruction.list", handler=_list, Input=GroupIdInput,
        authz=GROUP_MEMBER_OF("group_id"), Output=GroupInstructionsBundle,
        description=("Group base doctrine + skills index, plus the caller's rights: "
                     "can_write_instructions (any member) and "
                     "can_delete_instructions (team lead) — can_edit is the "
                     "right to ADMINISTER the team, not to edit a procedure."),
        rest=RestBinding("GET", "/api/groups/{id}/instructions", _GID),
    ),
    Capability(
        key="group.instruction.get", handler=_get, Input=InstrGetInput,
        authz=GROUP_MEMBER_OF("group_id"), Output=GroupInstructionView,
        description="Full markdown of one group instruction (slug `claude_md` = base doctrine).",
        rest=RestBinding("GET", "/api/groups/{id}/instructions/{slug}", _GID_SLUG),
    ),
    # ⚠️ Écrire et supprimer ne partagent PAS une garde (#681) : voir l'en-tête du
    # module. Le tableau de bord et `oto_procedure op=set scope='group'` écrivent la
    # MÊME procédure — les laisser sur deux gardes ferait de « qui peut annoter » une
    # propriété du TRANSPORT.
    Capability(
        key="group.instruction.set", handler=_set, Input=InstrSetInput,
        authz=GROUP_MEMBER_OF("group_id"), Output=GroupInstructionWritten,
        description=("Create/update a group instruction (any team MEMBER — whoever runs "
                     "a procedure may improve it; a bad edit is undone by restoring a "
                     "past version). slug `claude_md` "
                     "= the group base doctrine; any other slug = a named skill."),
        rest=RestBinding("PUT", "/api/groups/{id}/instructions/{slug}", _GID_SLUG),
    ),
    Capability(
        key="group.instruction.delete", handler=_delete, Input=InstrSlugInput,
        authz=GROUP_ADMIN_OF("group_id"), Output=GroupInstructionDeleted,
        description=("Delete a group instruction and its history (team LEAD) — "
                     "destructive and irreversible, unlike writing."),
        rest=RestBinding("DELETE", "/api/groups/{id}/instructions/{slug}", _GID_SLUG),
    ),
    Capability(
        key="group.instruction.versions", handler=_versions, Input=InstrSlugInput,
        authz=GROUP_MEMBER_OF("group_id"), Output=GroupInstructionVersions,
        description="Version history of one group instruction (metadata, latest first).",
        rest=RestBinding("GET", "/api/groups/{id}/instructions/{slug}/versions", _GID_SLUG),
    ),
    Capability(
        key="group.instruction.revert", handler=_revert, Input=InstrRevertInput,
        authz=GROUP_MEMBER_OF("group_id"), Output=GroupInstructionReverted,
        description=("Restore an older version of a group instruction as a new version "
                     "(any team MEMBER — it is the undo of `set`, and it destroys "
                     "nothing)."),
        rest=RestBinding("POST", "/api/groups/{id}/instructions/{slug}/revert", _GID_SLUG),
    ),
]
