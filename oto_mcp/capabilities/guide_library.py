"""Capacités « bibliothèque publique de guides » (marketplace de skills).

Un catalogue cherchable et partageable de guides PUBLIÉS, chaque entrée
portant un AUTEUR : **Otomata** (la plateforme) ou un **créateur privé** (une
org). Co-déclarées MCP + REST (ADR 0009) :

- lecture (`library.list`/`library.get`) = tout user authentifié (`SUB_ONLY`) ;
  la surface ANONYME pour la vitrine est servie à part par des routes écrites à
  la main dans `api.routes` (l'adaptateur REst des capacités authentifie toujours).
- publication = **org_admin de l'org active** (entrée signée du NOM de l'org) ou
  **super_admin plateforme** (signée **Otomata**), déclaré dans la règle d'autz
  `LIBRARY_PUBLISHER` (403 `publication_reservee_aux_admins` pour un membre). Le corps
  se lit dans l'**org active**, donc l'org reste REQUISE — la règle l'exige AVANT le
  rôle, qui s'y lit. Le contenu passe un **contrôle automatique** avant toute écriture
  (400 `contenu_refuse` : email, téléphone, clé d'API, URL à secret — `communaute.py`),
  puis l'entrée est listée tout de suite ; le personnel la retire après coup
  (`library.moderate`), et une republication ne lève pas un retrait.
- fork = tout membre de l'**org active** (injectée par `ORG_MEMBER`, jamais d'un
  param client → verrou IDOR ; l'org est REQUISE même pour un platform-operator).
  Le PALIER de la copie suit le rôle réel (décision d'Alexis, 06/10/2026) : un
  org_admin forke en procédure d'org, un simple membre en procédure PERSONNELLE
  (à lui seul, ADR 0068). `scope` peut le préciser ; `scope='org'` reste réservé à
  l'org_admin.
- publier est **borné à l'auteur** : le slug public est unique (c'est l'adresse de
  l'entrée, toute l'API adresse par slug) et POSSÉDÉ — republier le sien
  incrémente sa version, viser celui d'un autre auteur est refusé (409 `slug_taken`,
  message non-disclosant : un slug `unlisted` est un lien secret).
- dépublication = l'auteur (org_admin de l'org auteur) ou un admin plateforme.

Handlers SYNC (les adaptateurs n'awaitent pas). Le fork réutilise
`org_store.set_instruction` → le guide forké devient une procédure versionnée du
palier choisi, rattachable à un projet (`project_id`, lien `procedure` du projet).
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

from .. import access, db, deprecations, org_store, ownership, procedure_diagram, roles
from ..db import communaute as db_communaute
from . import communaute
from ._authz import (LIBRARY_PUBLISHER, ORG_MEMBER, SUB_ONLY, _refus_org_admin,
                     _refus_publication_bibliotheque, peut_publier_bibliotheque)
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES


class LibraryListInput(BaseModel):
    query: Optional[str] = None
    category: Optional[str] = None
    author_kind: Optional[str] = None    # 'otomata' | 'org'
    limit: int = 100
    # Absent = dernière publication d'abord (le tri d'avant, inchangé pour qui ne
    # demande rien) ; `trending` = installs_30d, `most_added` = installs_total, `new`.
    sort: Optional[Literal["trending", "most_added", "new"]] = None
    # Les entrées RETIRÉES que l'appelant peut voir (celles de son org s'il en est
    # admin ; toutes pour un opérateur plateforme). Ignoré pour tout autre compte.
    include_removed: bool = False

    @field_validator("limit")
    @classmethod
    def _cap(cls, v):
        # Patron `SearchInput._cap` : la valeur part telle quelle en `LIMIT %s`. Sans
        # borne, un `limit` énorme rend toute la bibliothèque d'un coup (avec un ILIKE
        # sur `body_md` quand il y a une requête) et un NÉGATIF fait échouer Postgres
        # (« LIMIT must not be negative ») en 500 opaque.
        return max(1, min(int(v), 200))


class LibraryGetInput(BaseModel):
    slug: str


class PublishInput(BaseModel):
    slug: str                            # le slug du skill d'org à publier
    public_slug: Optional[str] = None    # slug public (défaut = slug source)
    title: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    tags: Optional[list] = None
    visibility: str = "public"           # 'public' | 'unlisted'


class ForkInput(BaseModel):
    slug: str                            # slug public de l'entrée à forker
    new_slug: Optional[str] = None
    # Palier de la copie. Absent = selon le rôle réel : `org` pour un org_admin,
    # `user` pour un membre. `org` demandé par un membre = refus nommé.
    scope: Optional[Literal["user", "org"]] = None
    # Projet où rattacher la copie (lien `procedure` du projet) ; l'appelant doit
    # pouvoir y écrire, vérifié AVANT le fork (pas de copie orpheline sur un refus).
    project_id: Optional[int] = None


class UnpublishInput(BaseModel):
    id: int


# ── formes de réponse (ADR 0009 : `Output` DÉCRIT la 200, ne la valide pas) ───
# Ce qui n'était écrit nulle part et qu'un intégrateur découvrait autrement en
# production : le catalogue ne porte PAS le corps, `snippet` dépend de la requête,
# un slug rendu peut différer de celui demandé, et un `version` ne dit pas la même
# chose selon qu'on publie ou qu'on forke.

# Validée par l'UNIQUE écriture (`org_store.library.publish_guide`) depuis la création
# de la table, colonne `NOT NULL DEFAULT 'public'` (#437).
LibraryVisibility = Literal["public", "unlisted"]


class LibraryEntrySummary(communaute.ChampsCommunaute):
    """Une entrée du CATALOGUE : métadonnées seules. Le corps markdown n'y est
    jamais — c'est library.get qui le sert, par slug."""
    id: int = Field(description="Identifiant de l'entrée publiée — la clé qu'attend "
                                "library.unpublish (≠ le slug).")
    slug: str = Field(description="Slug PUBLIC de l'entrée (normalisé à la "
                                  "publication) : la clé de library.get et library.fork. "
                                  "Il peut différer du slug du skill d'org d'origine.")
    title: str = Field(description="Titre affiché ; chaîne vide si l'auteur n'en a "
                                   "jamais posé (jamais null).")
    description: str = Field(description="Résumé d'une ligne ; chaîne vide par défaut.")
    author_kind: str = Field(description="'otomata' (publiée par la plateforme) | 'org' "
                                         "(créateur privé). C'est le seul axe de "
                                         "confiance affichable.")
    author_org_id: Optional[int] = Field(
        default=None,
        description="Org autrice quand author_kind='org' ; `null` pour une entrée "
                    "Otomata. Une entrée 'org' sans author_org_id n'est plus "
                    "dépubliable par son auteur (seul un admin plateforme le peut).")
    author_display: str = Field(description="Nom d'auteur affichable ('Otomata' ou le "
                                            "nom de l'org). Figé à la publication : il "
                                            "ne suit pas un renommage d'org ultérieur.")
    category: str = Field(description="Catégorie de rangement, libre ; chaîne vide si "
                                      "non classée (et non filtrable par category alors).")
    tags: list = Field(default_factory=list, description="Étiquettes libres, [] par défaut.")
    visibility: LibraryVisibility = Field(
        description="'public' (listée) | 'unlisted' (partage par LIEN : servie par slug "
                    "exact à tout compte authentifié, mais jamais listée ici). Sur cette "
                    "liste la valeur vaut donc toujours 'public'.")
    version: int = Field(description="Numéro de publication : incrémenté à chaque "
                                     "re-publication du même slug. 1 = jamais republiée.")
    created_at: str = Field(description="Première publication ('YYYY-MM-DD HH:MM:SS' UTC).")
    updated_at: str = Field(description="Dernière re-publication — la clé de tri du "
                                        "catalogue quand `sort` est absent (plus "
                                        "récentes d'abord).")
    slots: list = Field(default_factory=list,
                        description="Entités requises déclarées par la procédure : ce "
                                    "qu'il faudra brancher après le fork.")
    snippet: Optional[str] = Field(
        default=None,
        description="Extrait du corps autour de la 1ʳᵉ occurrence de `query`. Présent "
                    "UNIQUEMENT quand une `query` a été passée : son absence signifie "
                    "« pas de recherche », jamais « corps vide ».")


class LibraryList(BaseModel):
    """Catalogue filtré, trié par `sort` (absent = plus récemment publiées d'abord),
    borné par `limit`. Les entrées retirées par le personnel n'y sont pas, sauf avec
    `include_removed` pour qui peut les voir.

    ⚠️ Ne contient QUE les entrées `public` : une entrée `unlisted` existe et reste
    lisible par son slug exact (library.get), mais n'apparaît jamais ici. Une liste
    vide ne prouve donc pas qu'un guide n'existe pas."""
    guides: list[LibraryEntrySummary]


class LibraryEntry(communaute.ChampsCommunaute):
    """Une entrée COMPLÈTE, corps inclus. ⚠️ Rendue à plat, sans enveloppe : les
    champs sont à la racine de la réponse (pas de clé d'enveloppe)."""
    id: int = Field(description="Identifiant de l'entrée publiée.")
    slug: str = Field(description="Slug public.")
    title: str = Field(description="Titre affiché ('' si absent).")
    description: str = Field(description="Résumé ('' si absent).")
    body_md: str = Field(description="Le corps markdown intégral du guide — la "
                                     "matière à lire avant de forker. Jamais vide : une "
                                     "publication au corps vide est refusée.")
    slots: list = Field(default_factory=list,
                        description="Entités requises déclarées par la procédure (ADR "
                                    "0035) : ce qu'il faudra brancher APRÈS le fork pour "
                                    "qu'elle tourne. [] = rien à brancher.")
    author_kind: str = Field(description="'otomata' | 'org'.")
    author_org_id: Optional[int] = Field(default=None, description="Org autrice, `null` "
                                                                   "pour Otomata.")
    author_display: str = Field(description="Nom d'auteur figé à la publication.")
    category: str = Field(description="Catégorie ('' si non classée).")
    tags: list = Field(default_factory=list, description="Étiquettes libres.")
    visibility: LibraryVisibility = Field(
        description="'public' | 'unlisted'. Ici la valeur PEUT être 'unlisted' : cette "
                    "lecture sert le partage par lien. « Non listé » ≠ secret d'org — "
                    "tout compte authentifié qui connaît le slug lit l'entrée.")
    source_org_id: Optional[int] = Field(
        default=None,
        description="Org d'où le skill a été publié (traçabilité interne). `null` pour "
                    "une publication au nom d'Otomata.")
    source_slug: Optional[str] = Field(default=None, description="Slug du skill d'org "
                                                                 "d'origine, quand il "
                                                                 "diffère du slug public.")
    forked_from: Optional[int] = Field(
        default=None,
        description="Entrée de bibliothèque dont celle-ci descend, si elle a elle-même "
                    "été forkée avant d'être republiée. `null` = publication d'origine.")
    version: int = Field(description="Numéro de publication (incrémenté à chaque "
                                     "re-publication du même slug).")
    published_by: Optional[str] = Field(default=None, description="Compte (sub) auteur de "
                                                                  "la dernière publication.")
    created_at: str = Field(description="Première publication ('YYYY-MM-DD HH:MM:SS' UTC).")
    updated_at: str = Field(description="Dernière re-publication.")


class PublishResult(BaseModel):
    """Accusé de publication. Une entrée déjà publiée sous le même slug public par
    le MÊME auteur (la même org, ou la plateforme) est REMPLACÉE (corps, titre) et sa
    version incrémentée — publier n'est donc pas toujours une création. Le slug d'une
    entrée d'un autre auteur est refusé, jamais repris : 409 `slug_taken`."""
    published: bool = Field(description="Toujours `true` : un échec ne rend pas "
                                        "`published:false`, il lève (403 "
                                        "`publication_reservee_aux_admins` hors admin de "
                                        "l'org, 400 `contenu_refuse` au contrôle "
                                        "automatique, 404 guide absente, 409 nom déjà "
                                        "pris par un autre auteur). Ne pas le tester "
                                        "comme un booléen d'issue.")
    author_kind: str = Field(description="'org' (signée du nom de l'org active) | "
                                         "'otomata' (super_admin plateforme).")
    author_display: str = Field(description="Le nom qui signe l'entrée au catalogue.")
    moderation_state: communaute.ModerationState = Field(
        default="listed",
        description="'listed' : l'entrée est visible de tous dès maintenant. 'removed' : "
                    "le personnel l'avait retirée, et la republier ne lève pas le retrait.")
    id: int = Field(description="Identifiant de l'entrée publiée — à conserver, c'est "
                                "ce qu'attend library.unpublish.")
    slug: str = Field(description="Slug public RÉELLEMENT retenu, après normalisation : "
                                  "il peut différer du `public_slug` demandé.")
    version: int = Field(description="Version de publication après l'opération. `1` = "
                                     "création ; ≥2 = le slug existait et vient d'être "
                                     "écrasé.")
    visibility: LibraryVisibility = Field(description="'public' | 'unlisted' tel qu'enregistré.")
    diagram_warning: Optional[str] = Field(
        default=None,
        description="Le SCHÉMA manquant du corps publié (cf. `procedure_diagram`). "
                    "`null` = rien à signaler. Non bloquant : la publication a eu lieu.")


class ForkResult(BaseModel):
    """Accusé de fork : le guide public a été COPIÉ en procédure versionnée, au
    palier `scope`. Copie ponctuelle, sans lien vivant — republier la source ne
    mettra jamais à jour le fork."""
    forked: bool = Field(description="Toujours `true` (un échec lève : 404 entrée "
                                     "inconnue, 403 `scope='org'` sans org_admin, 403 "
                                     "projet non modifiable).")
    org_id: int = Field(description="Org ACTIVE de l'appelant, jamais un paramètre — "
                                    "c'est le verrou anti-IDOR. Propriétaire de la copie "
                                    "quand `scope='org'`.")
    scope: Literal["user", "org"] = Field(
        description="Palier de la copie : `org` (procédure d'org, vue de tous les "
                    "membres) ou `user` (procédure personnelle de l'appelant). Sans "
                    "`scope` demandé : `org` pour un org_admin, `user` pour un membre.")
    slug: str = Field(description="Slug de la procédure créée. Peut différer du "
                                  "`new_slug` demandé : en cas de collision dans le "
                                  "palier, il est suffixé -2, -3… plutôt que d'écraser.")
    version: int = Field(description="Version de la procédure créée — vaut toujours 1, "
                                     "le slug étant dédoublonné avant écriture (un fork "
                                     "n'écrase jamais une procédure existante).")
    guide_id: Optional[int] = Field(default=None,
                                    description="Identifiant STABLE de la procédure créée.")
    forked_from: int = Field(description="Identifiant de l'entrée de bibliothèque "
                                         "source, conservé pour la traçabilité.")
    source_title: str = Field(description="Titre de l'entrée source au moment du fork "
                                          "('' si elle n'en portait pas).")
    project_id: Optional[int] = Field(
        default=None,
        description="Projet auquel la copie a été rattachée (lien `procedure`), si "
                    "demandé.")
    diagram_warning: Optional[str] = Field(
        default=None,
        description="Le SCHÉMA manquant du corps forké (cf. `procedure_diagram`). "
                    "`null` = rien à signaler. Non bloquant : le fork a eu lieu.")


class UnpublishResult(BaseModel):
    """Retrait du catalogue. Le retrait supprime l'entrée PUBLIÉE, jamais le skill
    d'org d'origine ni les forks déjà faits par d'autres."""
    unpublished: bool = Field(
        description="`false` n'est ni un refus (403) ni un identifiant inconnu (404) — "
                    "les deux lèvent avant. C'est le cas de course : l'entrée a disparu "
                    "entre la vérification d'auteur et la suppression, donc le résultat "
                    "voulu est déjà atteint.")


#: Les slugs publics qui ne désignent pas une entrée : des adresses de la communauté
#: elle-même (`/community/apps` sur le site et dans l'application).
SLUGS_RESERVES = frozenset({"apps"})


def _author_for(ctx: ResolvedCtx) -> tuple[str, Optional[int], str]:
    """Auteur affiché : super_admin plateforme → Otomata ; sinon l'org active (un
    opérateur `admin` publie comme l'org dont il est admin, pas au nom d'Otomata).

    Lève plutôt que de publier un auteur vide : `author_display` est le seul axe
    de confiance affiché au catalogue, une entrée anonyme n'y a rien à faire."""
    if access.is_super_admin(ctx.sub):
        return "otomata", None, "Otomata"
    o = org_store.get_org(ctx.org_id)
    if not o:
        raise AuthzDenied(404, "unknown_org", f"Org #{ctx.org_id} introuvable.")
    name = (o.get("name") or "").strip()
    if not name:
        raise AuthzDenied(400, "unnamed_org",
                          "Ton org n'a pas de nom : nomme-la avant de publier "
                          "(elle signe l'entrée au catalogue).")
    return "org", ctx.org_id, name


def _list(ctx: ResolvedCtx, inp: LibraryListInput) -> dict:
    operateur = access.is_platform_operator(ctx.sub)
    org_retirees = None
    if inp.include_removed and not operateur and ctx.org_id is not None \
            and roles.is_org_admin(ctx.sub, ctx.org_id):
        org_retirees = ctx.org_id
    rows = org_store.list_library(
        query=inp.query, category=inp.category, author_kind=inp.author_kind,
        include_unlisted=False, limit=inp.limit, sort=inp.sort,
        removed_for_org=org_retirees,
        include_all_removed=bool(inp.include_removed and operateur))
    return {"guides": [communaute.projeter(ctx.sub, r, privilegie=operateur or (
        org_retirees is not None and r.get("author_org_id") == org_retirees))
        for r in rows]}


def _entree_visible(ctx: ResolvedCtx, slug: str) -> dict:
    """L'entrée par slug, ou 404 — une entrée RETIRÉE n'existe que pour qui peut la voir
    (même 404 qu'une entrée inconnue pour les autres)."""
    entry = org_store.get_library_entry(slug=slug, include_unlisted=True,
                                        include_removed=True)
    if not entry or (entry.get("moderation_state") == "removed"
                     and not communaute.peut_voir_retiree(ctx.sub, entry)):
        raise AuthzDenied(404, "unknown_entry", f"Guide public `{slug}` inconnu.")
    return communaute.projeter(ctx.sub, entry)


def _get(ctx: ResolvedCtx, inp: LibraryGetInput) -> dict:
    # Sémantique `unlisted` = **lien non listé** (style YouTube), choix assumé :
    # une entrée `unlisted` est servie par SLUG EXACT à tout user authentifié
    # (`include_unlisted=True`), mais n'apparaît JAMAIS dans le catalogue
    # (`_list` force `include_unlisted=False`) ni sur la surface anonyme. C'est un
    # partage par lien — pas un secret d'org. Un guide vraiment sensible ne se
    # publie pas (reste un skill d'org privé). Cf. CLAUDE.md §Bibliothèque.
    return _entree_visible(ctx, inp.slug)


def _publish(ctx: ResolvedCtx, inp: PublishInput) -> dict:
    # Qui publie se DÉCLARE dans la règle d'autz des deux capacités qui mènent ici
    # (`LIBRARY_PUBLISHER` : `library.publish`, `oto_procedure op=publish`) : c'est
    # elle qui refuse sur les faces servies, avant ce handler. Ce contrôle n'est qu'un
    # filet pour un appelant qui câblerait `_publish` sous une autre règle — même
    # refus, même source, jamais une seconde phrase. L'entrée est signée de l'org, ou
    # d'Otomata pour un super_admin (`_author_for`).
    if ctx.org_id is None:
        raise AuthzDenied(400, "no_active_org",
                          "Publier demande une org active — choisis-en une avec oto_use_org.")
    if not peut_publier_bibliotheque(ctx.sub, ctx.org_id):
        raise _refus_publication_bibliotheque()
    org_id = ctx.org_id
    src = org_store.get_instruction("org", org_id, inp.slug)
    if not src:
        # Code d'aujourd'hui + code d'hier dans `details.legacy_code` : un code
        # d'erreur ne se double pas, il n'y a qu'un champ `error` (#519).
        raise AuthzDenied(404, "unknown_guide",
                          f"Guide `{inp.slug}` absent de ton org active.",
                          deprecations.details_avec_code_dhier("unknown_guide"))
    kind, author_org_id, display = _author_for(ctx)
    public = org_store.normalize_slug(inp.public_slug or inp.slug)
    if public in SLUGS_RESERVES:
        # Même refus qu'un nom pris : le slug est l'adresse de l'entrée, et `apps` est
        # celle de la page des apps de la communauté (site et application).
        raise AuthzDenied(409, "slug_taken", f"Le nom `{public}` n'est pas disponible — "
                          "publie sous un autre `public_slug`.")
    title = inp.title if inp.title is not None else (src.get("title") or "")
    description = (inp.description if inp.description is not None
                   else (src.get("description") or ""))
    # Le contrôle automatique, AVANT toute écriture : ce qui part ici est lu par tous
    # les comptes, et ses métadonnées par le web entier (`/api/public/community`).
    communaute.refuser_si_contenu({
        "title": title, "description": description, "body_md": src["body_md"],
        "category": inp.category or "", "tags": " ".join(map(str, inp.tags or [])),
        "slots": " ".join(str(x.get("description") or "")
                          for x in (src.get("slots") or []) if isinstance(x, dict))})
    try:
        row = org_store.publish_guide(
            slug=inp.public_slug or inp.slug,
            title=title, description=description, body_md=src["body_md"], author_kind=kind, author_org_id=author_org_id,
            author_display=display, category=inp.category or "", tags=inp.tags or [],
            visibility=inp.visibility, source_org_id=org_id, source_slug=inp.slug,
            published_by=ctx.sub, slots=src.get("slots") or [],
        )
    except org_store.LibrarySlotsInvalid as e:
        # Le schéma cible d'un slot se juge à la publication comme à l'écriture d'une
        # procédure (oto#34) : seul ce que ce geste ajoute ou change est refusé.
        raise AuthzDenied(400, "invalid_slots", f"Publication refusée — {e}")
    except org_store.LibrarySlugTaken:
        # NON-DISCLOSANT, à dessein : ne jamais dire À QUI est l'entrée, ni même
        # qu'elle existe sous cette forme — le slug d'une entrée `unlisted` est un
        # lien secret (cf. `_get`), un refus précis le confirmerait à qui devine.
        raise AuthzDenied(409, "slug_taken",
                          f"Le nom `{org_store.normalize_slug(inp.public_slug or inp.slug)}` "
                          "n'est pas disponible — publie sous un autre `public_slug`.")
    # Publier une procédure sans schéma propage le manque à tous ses forks : le
    # signal part ici aussi, au même régime non bloquant (front tiers, issue #108).
    etat = db_communaute.etat(org_store.KIND, row["id"])["state"]
    return {"published": True, "id": row["id"], "slug": row["slug"],
            "version": row["version"], "visibility": row["visibility"],
            "author_kind": kind, "author_display": display, "moderation_state": etat,
            **procedure_diagram.diagram_check(src.get("body_md") or "")}


# Ce qui reste ouvert au membre qui demande `scope='org'` sans être admin.
_FORK_PERSO = ("Sans `scope`, ou avec `scope='user'`, le fork crée une procédure qui "
               "n'appartient qu'à TOI, que tu peux modifier puis partager.")


def _fork(ctx: ResolvedCtx, inp: ForkInput) -> dict:
    # Ouvert à tout membre de l'org active (retour client 06/10). L'org reste exigée
    # ici, le handler pouvant être servi hors de la règle ORG_MEMBER.
    if ctx.org_id is None:
        raise AuthzDenied(400, "no_active_org",
                          "Forker demande une org active — choisis-en une avec oto_use_org.")
    org_id = ctx.org_id
    # Le palier suit le rôle RÉEL dans l'org active, jamais un paramètre seul : une
    # procédure d'org est vue de tous les membres et fait autorité pour leurs agents,
    # l'écrire reste un geste d'org_admin (même garde que `oto_procedure set scope=org`).
    admin = roles.is_org_admin(ctx.sub, org_id)
    scope = inp.scope or ("org" if admin else "user")
    if scope == "org" and not admin:
        raise _refus_org_admin(org_id, _FORK_PERSO, sub=ctx.sub)
    if inp.project_id is not None:
        projet = db.get_project_by_id(int(inp.project_id))
        if not projet or not ownership.can_access(ctx.sub, "project",
                                                  str(inp.project_id), "write"):
            raise AuthzDenied(403, "project_not_writable",
                              f"Projet #{inp.project_id} introuvable ou non modifiable par "
                              "toi : rien n'a été forké. Choisis un projet où tu peux "
                              "écrire, ou forke sans `project_id`.")
    entry = _entree_visible(ctx, inp.slug)
    owner = ("org", org_id) if scope == "org" else ("user", ctx.sub)
    res = org_store.fork_library_entry(entry_id=entry["id"], owner_type=owner[0],
                                       owner_id=owner[1], new_slug=inp.new_slug,
                                       set_by=ctx.sub, install_org_id=org_id)
    out = {"forked": True, "org_id": org_id, "scope": scope, "slug": res["slug"],
           "version": res["version"], "guide_id": res["guide_id"],
           "forked_from": res["forked_from"], "source_title": res["source_title"]}
    if inp.project_id is not None:
        db.add_project_link(int(inp.project_id), "procedure", str(res["guide_id"]))
        db.log_project_activity(int(inp.project_id), ctx.sub, "project.link",
                                f"procedure:{res['slug']}")
        out["project_id"] = int(inp.project_id)
    # Le fork est une écriture de procédure comme une autre : on repart avec un
    # corps qu'on n'a pas écrit, et c'est à nous de lui dessiner son schéma.
    return {**out, **procedure_diagram.diagram_check(entry.get("body_md") or "")}


def _unpublish(ctx: ResolvedCtx, inp: UnpublishInput) -> dict:
    entry = org_store.get_library_entry(entry_id=inp.id, include_unlisted=True,
                                        include_removed=True)
    if not entry or (entry.get("moderation_state") == "removed"
                     and not communaute.peut_voir_retiree(ctx.sub, entry)):
        raise AuthzDenied(404, "unknown_entry", "Entrée inconnue.")
    is_author = (entry["author_kind"] == "org" and entry.get("author_org_id") is not None
                 and roles.is_org_admin(ctx.sub, entry["author_org_id"]))
    if not (is_author or access.is_platform_operator(ctx.sub)):
        raise AuthzDenied(403, "forbidden", "Réservé à l'auteur ou à un admin plateforme.")
    communaute.refuser_si_retiree(ctx.sub, entry)
    return {"unpublished": org_store.unpublish_guide(inp.id)}


CAPABILITIES += [
    Capability(
        key="library.list", handler=_list, Input=LibraryListInput, authz=SUB_ONLY,
        description="Browse/search the COMMUNITY (public guide library: a marketplace of "
                    "processes). Each entry has an author (Otomata or the org that "
                    "published it) and install counters (installs_total, installs_30d = "
                    "distinct orgs). Filter by query / category / author_kind "
                    "(otomata|org); sort = trending | most_added | new (default: latest "
                    "published first). Entries removed by platform staff are hidden "
                    "(include_removed=true shows them to the author org's admins and to "
                    "staff). Returns metadata + snippet, not the full body — use "
                    "oto_procedure op=library_get for that.",
        Output=LibraryList,
        rest=RestBinding("GET", "/api/me/guide-library"),
    ),
    Capability(
        key="library.get", handler=_get, Input=LibraryGetInput, authz=SUB_ONLY,
        description="Read one public-library guide in full (markdown body) by its public "
                    "slug — preview before forking it into your org with oto_procedure op=fork. "
                    "Also serves `unlisted` entries by exact slug (unlisted = shared by link, "
                    "never in the catalog), not a private-org secret.",
        Output=LibraryEntry,
        rest=RestBinding("GET", "/api/me/guide-library/{slug}"),
    ),
    Capability(
        key="library.publish", handler=_publish, Input=PublishInput,
        authz=LIBRARY_PUBLISHER,
        description="Publish a procedure of your active org to the COMMUNITY (public "
                    "library) so other orgs can find and fork it. Org admins only (403 "
                    "publication_reservee_aux_admins): the entry is signed with your org's "
                    "name (a platform super_admin publishes as Otomata). Needs an active "
                    "org (the body is read from it). An automatic check refuses content "
                    "carrying email addresses, phone numbers, API keys/tokens or URLs with "
                    "secrets (400 contenu_refuse, `details.found` = field + kind, never the "
                    "value). Listed immediately; platform staff may remove it afterwards. "
                    "slug = the org procedure to publish ; visibility = public | unlisted. "
                    "Public names are unique and OWNED: re-publishing your own entry bumps "
                    "its version, a name held by another author is refused (409) — pick "
                    "another public_slug.",
        Output=PublishResult,
        rest=RestBinding("POST", "/api/me/guide-library/publish"),
    ),
    Capability(
        key="library.fork", handler=_fork, Input=ForkInput, authz=ORG_MEMBER,
        description="Fork (copy) a community (public-library) entry as a new versioned "
                    "procedure, counted as one install for your active org. Open "
                    "to any member of your active org: an org_admin gets an org procedure, a "
                    "member a personal one — `scope` user|org overrides, org "
                    "needs org_admin. slug = the public entry ; new_slug optional (defaults "
                    "to source slug, de-duplicated) ; project_id optional (links the copy "
                    "to that project, which you must be able to edit).",
        Output=ForkResult,
        rest=RestBinding("POST", "/api/me/guide-library/fork"),
    ),
    Capability(
        key="library.unpublish", handler=_unpublish, Input=UnpublishInput, authz=SUB_ONLY,
        description="Remove a guide you published from the public library (author org_admin "
                    "or platform admin). id = the library entry id. An entry removed by "
                    "platform staff can't be unpublished by its author (403 "
                    "entree_retiree): unpublishing then re-publishing would lift the "
                    "removal.",
        Output=UnpublishResult,
        rest=RestBinding("DELETE", "/api/me/guide-library/{id}"),
    ),
]
