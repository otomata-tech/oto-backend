"""Apps : écrans bâtis par les utilisateurs (`oto_apps`, `POST /api/me/apps`).

**Une app n'est pas une page de mémoire.** Jusqu'ici une app vivait dans une page
(un bloc de code dans un `oto_doc`) : elle polluait la mémoire du projet et la
recherche, la liste des apps devait les DEVINER par recherche plein texte, et le
serveur ne savait ni la valider ni dire qui l'avait écrite. Elle a désormais sa table
(`db/schema/apps.py`), ses révisions et cette capacité — `docs/apps.md`.

**Une app a son propriétaire et ses partages, comme un tableau** (kind `app`,
`ownership`) : une personne, une équipe ou une org la possède ; `oto_resource`
(`resource_type=app`) la partage en lecteur, éditeur ou gérant, la transfère, et
l'écran « Partager » est le même que celui d'un projet. Une app NEUVE appartient à qui
la crée, dans l'org active, privée — sauf si l'org a réglé un partage par défaut
(`partage_par_defaut`, type `app`). Lire = `can_access` lecture, la modifier =
écriture. Une app inconnue OU illisible rend le même 404 : rien ne dit qu'elle existe.

**Un projet RANGE une app, il ne l'ouvre pas** (`app_projects`, 0..n projets) :
`op=file` / `op=unfile` l'affichent sur la page d'un projet ; ranger ne donne accès ni
à l'app ni au projet. `op=list` sans projet rend les apps de l'org active — possédées
par elle, ses équipes, moi ici, ou partagées à l'un de ces principals.

**Le manifeste se juge à l'écriture** (`apps_manifeste.py`) — création, mise à jour
ET restauration d'une révision : sa forme, que chaque tableau, champ et agent déclarés
sont à la portée de l'AUTEUR, et qu'ils vivent dans l'org de l'app (`org_de_l_app`). Un
manifeste invalide est refusé en 400 `invalid_manifest` avec la liste de ce qui ne va
pas ; `op=validate` rend cette liste sans rien écrire. Le HTML, lui, se LIT à
l'écriture sans jamais refuser (`apps_html.py`) : ce qu'il demande hors du manifeste
revient en `warnings`.

**Écrire une app est ouvert à toute org** depuis le 2026-11-04 : la porte `beta` sur
les écritures (403 `apps_beta_only`) et `oto_apps` dans `BETA_TOOLS` sont tombées. Ce qui
borne une app n'a jamais été l'option : c'est le manifeste jugé contre la portée de
l'AUTEUR, et à l'appel l'intersection auteur ∩ lecteur.

**Face REST : session interactive seule** (`allow_api_token=False`) — aucun jeton
`oto_`, délégation d'agent hébergé comprise : une app s'écrit par un humain ou par son
agent en MCP, pas par un script porteur de clé.
"""
from __future__ import annotations

import os
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from .. import db, interrupteurs, org_suspension, ownership
from ._authz import SUB_ONLY
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .apps_html import MAX_WARNINGS, avertissements
from . import apps_versions as V
from .apps_manifeste import auteur, forme, org_de_l_app
from .orgs import partage_par_defaut
from .remplacements import RemplacementRefuse, appliquer
from .registry import CAPABILITIES


PROJECT_RTYPE = "project"
APP_RTYPE = "app"
MAX_HTML = 1_000_000
MAX_TITLE = 200
MAX_PROJECTS = 200
_THUMB_KEYS = {"image", "color", "emoji", "design"}
_ECRITURES = frozenset({"create", "update", "patch", "archive", "unarchive", "restore",
                        "file", "unfile", "publish", "delete_draft", "set_drafts",
                        "set_role", "set_scope", "grant_through_app", "revoke_through_app"})


class AppInput(BaseModel):
    op: Literal["list", "get", "create", "update", "patch", "archive", "unarchive",
                "revisions", "revision", "restore", "validate", "file", "unfile",
                "read_file", "put_file", "publish", "delete_draft", "set_drafts",
                "roles", "set_role", "set_scope", "grant_through_app", "revoke_through_app",
                "reads"]
    project_id: Optional[int] = Field(
        default=None, description="create: also file the new app in this project. "
        "file / unfile: the project. list: only the apps filed in it.")
    project_ids: Optional[list[int]] = Field(
        default=None, description="list: only the apps filed in these projects (at most "
        "200). Without any project: the apps of your active organization.")
    app_id: Optional[int] = None
    title: Optional[str] = None
    manifest: Optional[dict] = None
    html: Optional[str] = None
    thumbnail: Optional[dict] = None
    clear_thumbnail: Optional[bool] = None
    expected_rev: Optional[str] = None
    edits: Optional[list[dict]] = Field(default=None, description=(
        "patch: exact replacements in the app's HTML, applied in order, all or nothing — "
        "`[{\"find\": \"…\", \"replace\": \"…\"}]`; `find` must appear exactly once "
        "(or pass `\"all\": true`)."))
    revision_id: Optional[int] = None
    archived: Optional[bool] = None
    draft: Optional[bool] = Field(
        default=None, description="get: read the app's DRAFT (its manifest, HTML and "
        "rev) instead of its published version — editors only.")
    note: Optional[str] = Field(
        default=None, description="publish / restore: what this version changes, shown "
        "in the app's history (500 characters).")
    to_draft: Optional[bool] = Field(
        default=None, description="restore, on an app with drafts on: put the version "
        "in the DRAFT, to preview it before publishing, instead of making it live.")
    enabled: Optional[bool] = Field(
        default=None, description="set_drafts: true = edits go to a draft that "
        "op=publish makes live; false = every edit is live at once.")
    file_id: Optional[int] = Field(
        default=None, description="read_file: a file a person dropped in the app (its id "
        "comes with the run's input).")
    name: Optional[str] = Field(
        default=None, description="put_file: the file's name, with its extension "
        "(`summary.csv`, `report.docx`).")
    content: Optional[str] = Field(
        default=None, description="put_file: the file's TEXT (CSV, Markdown, text, JSON). "
        "Exactly one of `content` or `content_base64`.")
    content_base64: Optional[str] = Field(
        default=None, description="put_file: the file's bytes in base64 (PDF, Word, Excel).")
    media_type: Optional[str] = Field(
        default=None, description="put_file: its type (`text/csv`, `application/pdf`…); "
        "judged on the bytes anyway.")
    principal_type: Optional[Literal["user", "group", "org"]] = Field(
        default=None, description="set_role: who the app is shared with — a person "
        "(`user`), a team (`group`) or an organization (`org`), as op=roles lists them.")
    principal_id: Optional[str] = Field(
        default=None, description="set_role: their id, as op=roles lists it.")
    as_role: Optional[str] = Field(
        default=None, description="get: see the app AS one of the roles its manifest "
        "declares — that role's narrowed manifest, HTML without the sections it doesn't "
        "open, and its `access` (editors only, to preview a role). Rows scoped by `$share` "
        "come back empty in such a preview: it is no one's share.")
    tables: Optional[list[str]] = Field(
        default=None, description="grant_through_app: the app's table names to open "
        "(default: every table it declares). reads: only these tables — the app's table "
        "names, or table NUMBERS; without `app_id`, the numbers of tables you manage.")
    days: Optional[int] = Field(
        default=None, ge=1, le=180, description="reads: the last N days (UTC, today "
        "included), 30 by default, at most 180 (what the journal keeps).")
    role: Optional[str] = Field(
        default=None, description="set_role: one of the roles the app's manifest declares "
        "(`manifest.roles`); omit it to give them back the app's `default_role`.")
    scope: Optional[dict] = Field(
        default=None, description="set_scope: the rows they see in the app, per table — "
        "`{table: {field: value}}`; omit it to remove their scope.")


class AppCard(BaseModel):
    id: int
    owner_type: Optional[str] = None
    owner_id: Optional[str] = None
    # Les projets où l'app est rangée — affichage seulement, aucun accès.
    project_ids: list[int] = Field(default_factory=list)
    # L'appelant peut-il la modifier (éditeur, gérant, propriétaire) ?
    can_edit: bool = False
    # Qui la voit au-delà de son propriétaire (`db.apps.visibilites`) : `{"org":
    # "editor"|"contributor"|"viewer"|None, "people": n}` — « Vous seul » quand personne.
    visibility: Optional[dict] = None
    # Peut-il en cliquer les boutons (Use, ou la modifier) ? Servi par `op=get` seul ;
    # absent d'une liste. Faux = View : le front désactive les gestes.
    can_use: Optional[bool] = None
    title: str
    thumbnail: Optional[dict] = None
    created_by: Optional[str] = None
    created_at: Optional[Any] = None
    updated_by: Optional[str] = None
    updated_at: Optional[Any] = None
    archived_at: Optional[Any] = None


class AppView(AppCard):
    manifest: dict
    html: str
    rev: str
    # Brouillon et versions (`apps_versions.py`) : l'app passe-t-elle par un
    # brouillon, et l'état servi est-il une version publiée, avec sa note.
    drafts_enabled: bool = False
    published_at: Optional[Any] = None
    version_note: Optional[str] = None
    # Le rôle d'app qui borne l'appelant (`apps_roles.py`) : `role`, `role_label`,
    # `sections` (None = tout) — ce que l'app lit par `oto.access()`. Le manifeste
    # servi est déjà resserré à ce rôle.
    access: Optional[dict] = None


class AppDraft(BaseModel):
    """Le brouillon d'une app, sans son contenu — servi à ses seuls éditeurs."""
    rev: str
    updated_by: Optional[str] = None
    updated_at: Optional[Any] = None


class AppRevision(BaseModel):
    id: int
    title: str
    edited_by: Optional[str] = None
    # `created_at` : quand cet état a été REMPLACÉ (la révision s'écrit alors).
    created_at: Optional[Any] = None
    # Quand cet état a été ÉCRIT ; absent pour les révisions d'avant la colonne.
    saved_at: Optional[Any] = None
    # Cet état était une version publiée (`op=publish`, `op=restore`), et sa note.
    published_at: Optional[Any] = None
    note: Optional[str] = None


class AppOut(BaseModel):
    app: Optional[AppView] = None
    apps: Optional[list[AppCard]] = None
    revisions: Optional[list[AppRevision]] = None
    # get (éditeurs) : le brouillon en cours, s'il y en a un ; jamais servi à un lecteur.
    draft: Optional[AppDraft] = None
    # revision : une version entière (manifeste, HTML), pour la lire avant de la restaurer.
    revision: Optional[dict] = None
    # update, patch, restore, publish : où l'écriture est allée — `draft` (les
    # lecteurs n'en voient rien) ou `live`.
    target: Optional[Literal["draft", "live"]] = None
    # validate : vide = le manifeste passe.
    problems: Optional[list[str]] = None
    ok: Optional[bool] = None
    # read_file : le fichier rendu pour un agent (`file_content.render_for_agent`).
    file: Optional[dict] = None
    # create, update, restore, validate, patch : ce que le HTML demande hors du
    # manifeste (`apps_html.py`) — un avertissement, l'app est écrite quand même.
    warnings: Optional[list[str]] = None
    # patch : un reçu, jamais le HTML — l'op existe pour ne pas le faire transiter.
    rev: Optional[str] = None
    replaced: Optional[int] = None
    # roles / set_role : les rôles que l'app déclare (`[{key, label}]`, `default_role`)
    # et chacun de ses partages avec son rôle d'app (None = le rôle par défaut).
    roles: Optional[list[dict]] = None
    default_role: Optional[str] = None
    # roles : chaque nom de tableau du manifeste → son numéro.
    tables: Optional[dict] = None
    grants: Optional[list[dict]] = None
    # grant_through_app : les tableaux de l'app qu'on n'a pas pu ouvrir (pas gérant).
    skipped: Optional[list[dict]] = None
    # reads : le journal des lectures (`app_reads`), une ligne par app, tableau,
    # lecteur, accès et jour UTC — `_lectures`.
    reads: Optional[list[dict]] = None


def _require(cond, code: str, msg: str, status: int = 400) -> None:
    if not cond:
        raise AuthzDenied(status, code, msg)


def _can_project(sub: str, project_id: int, want: str) -> bool:
    return ownership.can_access(sub, PROJECT_RTYPE, str(project_id), want)


def _can(sub: str, app_id: int, want: str) -> bool:
    return ownership.can_access(sub, APP_RTYPE, str(app_id), want)


def _app_or_404(sub: str, app_id: Optional[int], want: str) -> dict:
    _require(app_id is not None, "missing_app_id", "`app_id` is required for this op.")
    row = db.apps.get(int(app_id))
    # Inconnue ou illisible : la même réponse, rien ne dit qu'elle existe.
    _require(row is not None and _can(sub, row["id"], "read"),
             "unknown_app", f"App {app_id} doesn't exist or isn't shared with you.", 404)
    if want == "write":
        _require(_can(sub, row["id"], "write"), "forbidden",
                 "You can read this app but not change it.", 403)
    return row


def _thumbnail(raw: Optional[dict]) -> Optional[dict]:
    if raw is None:
        return None
    _require(isinstance(raw, dict) and set(raw) <= _THUMB_KEYS, "invalid_thumbnail",
             f"A thumbnail takes only: {', '.join(sorted(_THUMB_KEYS))}.")
    image = raw.get("image")
    # La liste des apps affiche la vignette HORS du cadre isolé : une adresse libre
    # ferait appeler un hôte tiers par chaque lecteur. Seul le stockage média de
    # CETTE instance est permis ; base non déclarée = aucune image (fail-closed).
    _require(image is None or (isinstance(image, str) and len(image) <= 2000
                               and _media_base() is not None
                               and image.startswith(_media_base() + "/")),
             "invalid_thumbnail",
             "`thumbnail.image` must be a file published on this instance's media store.")
    for k, cap in (("color", 24), ("emoji", 16)):
        v = raw.get(k)
        _require(v is None or (isinstance(v, str) and len(v) <= cap), "invalid_thumbnail",
                 f"`thumbnail.{k}` is too long.")
    _require(raw.get("design") is None or isinstance(raw.get("design"), dict),
             "invalid_thumbnail", "`thumbnail.design` must be an object.")
    return {k: v for k, v in raw.items() if v is not None}


def _media_base() -> Optional[str]:
    from .. import media_store
    try:
        return media_store.public_base()
    except RuntimeError:  # noqa: SILENT — instance sans stockage média : aucune image permise
        return None


def _problemes(sub: str, org_id: Optional[int], raw: Optional[dict]) -> tuple[dict, list[str]]:
    clean, problems = forme(raw if raw is not None else {})
    if not problems:
        problems = auteur(sub, org_id, clean)
    return clean, problems


def _checked_manifest(ctx: ResolvedCtx, org_id: Optional[int], raw: Optional[dict]) -> dict:
    clean, problems = _problemes(ctx.sub, org_id, raw)
    if problems:
        raise AuthzDenied(400, "invalid_manifest",
                          "The manifest isn't valid: " + " ".join(problems))
    return clean


def _content(inp: AppInput, *, creating: bool) -> None:
    if creating or inp.title is not None:
        _require(isinstance(inp.title, str) and inp.title.strip() and len(inp.title) <= MAX_TITLE,
                 "invalid_title", f"A title is 1 to {MAX_TITLE} characters.")
    if inp.html is not None:
        _require(len(inp.html) <= MAX_HTML, "html_too_large",
                 f"An app's HTML is at most {MAX_HTML} characters.")


def _projets_lisibles(sub: str, filed: dict[int, list[int]]) -> set[int]:
    """Parmi les projets où ces apps sont rangées, ceux que `sub` LIT — en une passe
    (`can_access_many`). Ranger une app dans un projet ne le rend pas visible : une
    carte ne nomme que les projets que son lecteur voit déjà."""
    ids = {p for pids in filed.values() for p in pids}
    if not ids:
        return set()
    owners = db.apps.project_owners(ids)
    lus = ownership.can_access_many(sub, PROJECT_RTYPE, owners, "read") if owners else {}
    return {int(pid) for pid, ok in lus.items() if ok}


def _orgs_des_apps(rows: list[dict]) -> dict[int, Optional[int]]:
    """L'org où vit chaque app (`org_de_l_app`), lue une fois par propriétaire : une
    liste d'apps d'une même équipe ne relit pas l'équipe à chaque carte."""
    vues: dict[tuple, Optional[int]] = {}
    out: dict[int, Optional[int]] = {}
    for r in rows:
        cle = (r.get("owner_type"), r.get("owner_id"), r.get("context_org_id"))
        if cle not in vues:
            vues[cle] = org_de_l_app(r)
        out[int(r["id"])] = vues[cle]
    return out


def _cards(sub: str, rows: list[dict], writable: Optional[set[int]] = None) -> list[dict]:
    """Les cartes servies : de quoi dessiner la tuile, où l'app est rangée (parmi les
    projets que l'appelant lit), et s'il peut la modifier. `writable` : les apps que la
    liste SAIT modifiables (possédées, ou partagées en écriture) — sans lui, chaque
    carte se juge."""
    rangees = db.apps.projects_of(r["id"] for r in rows)
    lus = _projets_lisibles(sub, rangees)
    filed = {a: [p for p in pids if p in lus] for a, pids in rangees.items()}
    vis = db.apps.visibilites(_orgs_des_apps(rows))
    return [{**r, "project_ids": filed.get(int(r["id"]), []),
             "visibility": vis[int(r["id"])],
             "can_edit": (int(r["id"]) in writable if writable is not None
                          else _can(sub, r["id"], "write"))} for r in rows]


def _view(sub: str, row: dict, as_role: Optional[str] = None) -> dict:
    from . import apps_appel, apps_roles, apps_sections
    row = _cards(sub, [row])[0]
    row["can_use"] = bool(row.get("can_edit")) or apps_appel.peut_cliquer(sub, int(row["id"]))
    if "manifest" in row:
        # Un lecteur reçoit le manifeste de SON rôle, sans la définition des rôles ;
        # qui peut modifier l'app le reçoit entier — sauf s'il l'ESSAIE sous un rôle
        # (`as_role`) : il reçoit alors exactement ce que ce rôle reçoit.
        entier = row["manifest"] or {}
        essai = bool(as_role) and bool(row.get("can_edit"))
        if as_role and not row.get("can_edit"):
            raise AuthzDenied(403, "as_role_editors_only",
                              "Only the app's editors can see it as another role.")
        if essai and as_role not in (entier.get("roles") or {}):
            raise AuthzDenied(400, "unknown_role", f"This app declares no role “{as_role}”.")
        role = as_role if essai else (None if row.get("can_edit")
                                      else apps_roles.role_du_lecteur(sub, row))
        row["access"] = apps_roles.acces(entier, role)
        if not row.get("can_edit") or essai:
            row["manifest"] = apps_roles.resserrer(entier, role)
            if "html" in row:
                # Les éléments des sections que ce rôle n'ouvre pas ne partent pas
                # (`apps_sections.py`) ; sans rôle ou sans `sections`, le HTML entier.
                # Dernier recours du mode strict : une app écrite avant lui, ou dont le
                # manifeste a changé sans le HTML, n'est pas servie à un lecteur borné
                # tant que tout n'est pas classé — refus neutre, sans nom de section.
                if apps_sections.strict(entier) and apps_sections.non_classes(row["html"]):
                    raise AuthzDenied(403, "app_unavailable",
                                      "This app can't be shown to you right now: ask its "
                                      "editors.")
                row["html"] = apps_sections.retirer(row["html"], row["access"]["sections"])
    return {k: row[k] for k in AppView.model_fields if k in row}


def _avertir(manifest: Optional[dict], html: Optional[str]) -> dict:
    """`{"warnings": […]}` quand le HTML demande hors du manifeste, sinon `{}`.
    Appelé AVANT l'écriture : un défaut du lecteur de HTML refuse sans rien écrire,
    plutôt qu'un 500 après une création commitée qu'un agent rejouerait en doublon."""
    from . import apps_sections
    m = manifest or {}
    # Le mode strict des sections REFUSE (la seule exception à « le HTML avertit, ne
    # refuse jamais ») : un script, une feuille ou un gestionnaire non classé partirait
    # chez chaque lecteur borné. Jugé ici, avant toute écriture, sur le manifeste et le
    # HTML qu'elle laissera — create, update, patch, restore, publish.
    if apps_sections.strict(m):
        nc = apps_sections.non_classes(html)
        if nc:
            raise AuthzDenied(400, "sections_not_classified",
                              "Roles declare `sections`, so every <script>, <style> and on… "
                              "handler must be marked with a section, or with "
                              "data-oto-section=\"*\" when every role may read it (or set "
                              "`strict_sections: false`). " + " ".join(nc))
    w = (avertissements(m, html) + apps_sections.avertissements(m, html)
         + apps_sections.noms_dans_le_commun(m, html))[:MAX_WARNINGS]
    return {"warnings": w} if w else {}


def _devenir_auteur_ou_refus(ctx: ResolvedCtx, row: dict) -> None:
    """Toute écriture fait de son auteur celui de l'app (`updated_by`), dont les droits
    bornent chaque lecteur. Écrire sans pouvoir lire ce que l'app déclare la casserait
    pour tous : rejugé, refusé nommé."""
    if row.get("updated_by") != ctx.sub:
        _, problems = _problemes(ctx.sub, org_de_l_app(row), row["manifest"])
        if problems:
            raise AuthzDenied(
                403, "would_break_app",
                "Saving would make you this app's author, and you can't reach "
                "everything it uses: " + " ".join(problems))


def _org_list(ctx: ResolvedCtx, archived: bool) -> tuple[list[dict], set[int]]:
    """Les apps de l'org active : possédées par l'org ou ses équipes, mes apps perso
    créées ici, et celles partagées à l'un des principals de la liste — les mêmes
    seams que la liste des projets (« listé ⇔ lisible »). Rend aussi celles que
    l'appelant peut modifier, SANS une lecture par carte : une app possédée par ma
    portée l'est (comme un tableau de l'org pour ses membres), une partagée l'est si
    le meilleur partage est en écriture."""
    sub, org = ctx.sub, ctx.org_id
    if org is None:
        return [], set()
    owned = db.apps.list_for_owners(ownership.project_list_owners(sub, org), archived=archived,
                                    createur=ownership.mes_objets_ici(sub, org))
    writable = {int(r["id"]) for r in owned}
    granted, vus = [], {int(o["id"]) for o in owned}
    # Partagées à l'org ou à mes équipes, puis à moi en personne — rangées dans l'org de
    # l'app si j'en suis membre, sinon dans ma maison (06/11/2026).
    recues = (db.apps.list_granted_to(ownership.proprietaires_collectifs_de_liste(sub, org),
                                      archived=archived)
              + ownership.partages_a_moi_ici(
                  sub, org, db.apps.list_granted_to([("user", sub)], archived=archived)))
    for r in recues:
        if r.get("permission") == "write":
            writable.add(int(r["id"]))
        if int(r["id"]) not in vus:
            vus.add(int(r["id"]))
            granted.append({k: v for k, v in r.items() if k != "permission"})
    rows = sorted(owned + granted, key=lambda r: (r["updated_at"], r["id"]), reverse=True)
    return ownership.borner_a_la_vue(sub, APP_RTYPE, rows, lambda r: r["id"]), writable


def _app(ctx: ResolvedCtx, inp: AppInput) -> dict:
    sub = ctx.sub

    if inp.op == "list":
        ids = list(inp.project_ids or ([] if inp.project_id is None else [inp.project_id]))
        if not ids:
            rows, writable = _org_list(ctx, inp.archived is True)
            return {"apps": _cards(sub, rows, writable)}
        _require(len(ids) <= MAX_PROJECTS, "too_many_projects",
                 f"At most {MAX_PROJECTS} projects per list.")
        owners = db.apps.project_owners(ids)
        readable = ownership.can_access_many(sub, PROJECT_RTYPE, owners, "read") if owners else {}
        allowed = [int(pid) for pid, ok in readable.items() if ok]
        # Ranger n'ouvre rien : une app rangée dans un projet que je lis reste
        # invisible si elle ne m'est pas partagée.
        rows = [r for r in db.apps.list_for_projects(allowed, archived=inp.archived is True)
                if _can(sub, r["id"], "read")]
        return {"apps": _cards(sub, rows)}

    if inp.op == "validate":
        # Jugé contre l'org où l'app vit : la sienne, ou l'org active pour une app à
        # créer. Le HTML : celui passé, sinon celui de l'app ; jamais contre un
        # manifeste à corriger (chaque appel y paraîtrait hors manifeste).
        app = _app_or_404(sub, inp.app_id, "read") if inp.app_id is not None else None
        org = org_de_l_app(app) if app is not None else ctx.org_id
        if app is not None:
            org_suspension.exiger(org)
        clean, problems = _problemes(sub, org, inp.manifest)
        out: dict = {"ok": not problems, "problems": problems}
        # Les avertissements lisent le manifeste ENTIER (rôles, sections) et le HTML
        # entier : à qui peut modifier l'app seulement, jamais à un lecteur.
        if not problems and (app is None or _can(sub, app["id"], "write")):
            manifeste = app["manifest"] if inp.manifest is None and app is not None else clean
            html = inp.html if inp.html is not None else (app or {}).get("html")
            try:
                out.update(_avertir(manifeste, html))
            except AuthzDenied as e:
                if e.code != "sections_not_classified":
                    raise
                out = {"ok": False, "problems": [e.message]}
        return out

    if inp.op == "create":
        _require(ctx.org_id is not None, "no_active_org",
                 "An app is created in your active organization: pick one first.")
        if inp.project_id is not None:
            _require(_can_project(sub, int(inp.project_id), "write"), "forbidden",
                     "You can't file an app in this project.", 403)
        _content(inp, creating=True)
        manifest = _checked_manifest(ctx, int(ctx.org_id), inp.manifest)
        extra = _avertir(manifest, inp.html)
        # À qui la crée, dans l'org active : privée, comme un agent — l'org l'ouvre
        # seulement si un admin l'a réglé (`partage_par_defaut`, type `app`).
        row = db.apps.create(("user", sub), int(ctx.org_id), title=inp.title.strip(),
                             manifest=manifest, html=inp.html or "",
                             thumbnail=_thumbnail(inp.thumbnail), by=sub,
                             project_id=inp.project_id)
        role = partage_par_defaut.role_a_la_naissance(int(ctx.org_id), "app")
        if role:
            ownership.grant(APP_RTYPE, str(row["id"]), "org", str(ctx.org_id), role=role,
                            granted_by=db.MARQUE_PARTAGE_PAR_DEFAUT)
        return {"app": _view(sub, row), **extra}

    if inp.op == "reads":
        return _lectures(sub, inp)

    if inp.op == "revoke_through_app" and inp.app_id is not None and not _can(
            sub, inp.app_id, "read"):
        # Le GÉRANT d'un tableau retire ce qu'une app en ouvre, sans lire l'app.
        return _retirer_par_le_tableau(sub, inp)

    row = _app_or_404(sub, inp.app_id,
                      "read" if inp.op in ("get", "revisions", "read_file", "put_file",
                                           "roles", "set_role", "set_scope",
                                           "grant_through_app", "revoke_through_app")
                      else "write")
    # L'org de l'APP, en plus de celle de l'appel (gardée par l'adaptateur) : l'app d'une
    # org suspendue se ferme à tous, invités d'une autre org compris.
    org_suspension.exiger(org_de_l_app(row))

    if inp.op in ("roles", "set_role", "set_scope", "grant_through_app", "revoke_through_app"):
        return _roles(sub, row, inp)

    if inp.op == "get":
        # L'aperçu d'un rôle se fait sur l'app publiée : le brouillon remplacerait après
        # coup le manifeste et le HTML resserrés par les siens, entiers.
        _require(not (inp.draft and inp.as_role), "as_role_live_only",
                 "A role preview opens the published app: publish the draft first.")
        view = _view(sub, row, inp.as_role)
        if inp.draft:
            lu, courant = V.lire(row, view["can_edit"])
            return {"app": {**view, **{k: lu[k] for k in ("manifest", "html", "rev")}},
                    "draft": V.resume(courant), "target": "draft"}
        # Le brouillon n'est NOMMÉ qu'à qui peut modifier l'app : un lecteur n'en sait rien.
        draft = V.resume(db.app_versions.draft(row["id"])) if view["can_edit"] else None
        return {"app": view, **({"draft": draft} if draft else {})}

    if inp.op == "revision":
        return {"revision": V.version(row, inp.revision_id)}

    if inp.op == "publish":
        out, extra = V.publier(ctx, row, inp.expected_rev, inp.note, _checked_manifest, _avertir)
        return {"app": _view(sub, out), "target": "live", **extra}

    if inp.op == "delete_draft":
        return {"ok": V.supprimer(row, inp.expected_rev)}

    if inp.op == "set_drafts":
        return {"app": _view(sub, V.regler(row, inp.enabled))}

    if inp.op == "revisions":
        return {"revisions": db.apps.revisions(row["id"])}

    if inp.op == "read_file":
        # Le lecteur qui l'a déposé, ou un run qu'il a lancé (qui tourne en son nom).
        _require(inp.file_id is not None, "missing_file_id", "`file_id` is required.")
        f = db.apps.file_of(int(row["id"]), sub, int(inp.file_id))
        _require(f is not None, "unknown_file",
                 f"No file {inp.file_id} of yours in app {row['id']}.", 404)
        from .. import file_content, media_store
        try:
            data = media_store.fetch_object(f["s3_key"])
            rendu = file_content.render_for_agent(data, f["name"], f["media_type"],
                                                  sub=sub, prefix="app-files")
        except (media_store.MediaError, file_content.MediaUnavailable) as e:
            raise AuthzDenied(503, "file_unavailable",
                              f"The file can't be read right now: {e}") from None
        return {"file": {"id": int(f["id"]), **rendu}}

    if inp.op == "put_file":
        # Un AGENT rend un fichier à l'app, pour le lecteur au nom duquel il tourne (un
        # run hébergé tourne au nom de qui l'a lancé) : le lecteur le retrouve dans
        # l'app (`oto.files()`, `oto.download`). Pas une écriture de l'app
        # (`_ECRITURES`) : c'est le geste d'un lecteur, borné par sa lecture,
        # `files: true` et son plafond.
        _require((inp.content is None) != (inp.content_base64 is None), "invalid_file",
                 "Give exactly one of `content` (text) or `content_base64` (bytes).")
        _require(row.get("archived_at") is None, "unknown_app",
                 f"App {row['id']} doesn't exist or isn't shared with you.", 404)
        if inp.content is not None:
            data = inp.content.encode("utf-8")
        else:
            import base64
            import binascii
            try:
                data = base64.b64decode(inp.content_base64 or "", validate=True)
            except (binascii.Error, ValueError):
                raise AuthzDenied(400, "invalid_file", "`content_base64` isn't valid base64.") from None
        from . import apps_appel
        return {"file": apps_appel.rendre(sub, row, inp.name or "", inp.media_type, data)}

    if inp.op in ("file", "unfile"):
        _require(inp.project_id is not None, "missing_project", "`project_id` is required.")
        _require(_can_project(sub, int(inp.project_id), "write"), "forbidden",
                 "You can't file apps in this project.", 403)
        if inp.op == "file":
            db.apps.file(row["id"], int(inp.project_id), sub)
        else:
            db.apps.unfile(row["id"], int(inp.project_id))
        return {"app": _view(sub, row)}

    if inp.op in ("archive", "unarchive"):
        out = db.apps.set_archived(row["id"], inp.op == "archive")
        return {"app": _view(sub, out)}

    if inp.op == "restore":
        _require(inp.revision_id is not None, "missing_revision", "`revision_id` is required.")
        old = db.apps.revision(row["id"], int(inp.revision_id))
        _require(old is not None, "unknown_revision", "No such revision of this app.", 404)
        # Restaurer, c'est RÉÉCRIRE l'app sous son nom : le manifeste d'hier est rejugé
        # contre celui qui restaure (il devient l'auteur) et l'org de l'app d'aujourd'hui.
        manifest = _checked_manifest(ctx, org_de_l_app(row), old["manifest"])
        extra = _avertir(manifest, old["html"])
        if inp.to_draft:
            # Vers le brouillon : l'essayer avant de la rendre vivante. Le titre et la
            # vignette, la carte, n'en font pas partie.
            V.ecrire(ctx, row, expected_rev=inp.expected_rev, manifest=manifest,
                     html=old["html"], auteur_ou_refus=_devenir_auteur_ou_refus)
            return {"app": _view(sub, row), "target": "draft",
                    "draft": V.resume(db.app_versions.draft(row["id"])), **extra}
        # Une restauration est une version PUBLIÉE : un retour en arrière délibéré, gardé
        # dans l'historique hors de la purge des révisions ordinaires.
        changed = _update(sub, row, inp.expected_rev, title=old["title"], manifest=manifest,
                          html=old["html"], thumbnail=old["thumbnail"], published=True,
                          note=V._note(inp.note))
        return {"app": _view(sub, changed), "target": "live", **extra}

    if inp.op == "patch" and row.get("drafts_enabled"):
        # Une app à brouillon : le patch s'applique au BROUILLON (ou, sans lui, à l'état
        # vivant dont il naît), contre SON rev — jamais un `find` lu dans une version
        # et appliqué à une autre.
        base_html, base_manifest, base_rev = V.base_du_patch(row)
        try:
            html, n = appliquer(base_html, inp.edits, max_len=MAX_HTML)
        except RemplacementRefuse as e:
            raise AuthzDenied(400, "invalid_edits", str(e)) from None
        extra = _avertir(base_manifest, html)
        out = V.ecrire(ctx, row, expected_rev=inp.expected_rev or base_rev, html=html,
                       auteur_ou_refus=_devenir_auteur_ou_refus)
        return {"rev": out["rev"], "replaced": n, "target": "draft", **extra}

    if inp.op == "patch":
        # Changer une ligne sans renvoyer 40 000 caractères : un agent qui réécrit tout
        # le HTML pour un tri y laisse un tour de modèle entier, ou s'y fait couper.
        # Les gardes d'update : écriture sur l'app, auteur qui lit
        # ce que l'app déclare, taille (bornée AVANT de remplacer), révision.
        try:
            html, n = appliquer(row.get("html") or "", inp.edits, max_len=MAX_HTML)
        except RemplacementRefuse as e:
            raise AuthzDenied(400, "invalid_edits", str(e)) from None
        _devenir_auteur_ou_refus(ctx, row)
        # Le manifeste, un patch n'y touche pas : celui de l'app lue.
        extra = _avertir(row.get("manifest"), html)
        # Le HTML est calculé sur la révision LUE : sans `expected_rev`, on écrit contre
        # elle, sinon une écriture concurrente serait écrasée sans un mot.
        changed = _update(sub, row, inp.expected_rev or row["rev"], html=html)
        return {"rev": changed["rev"], "replaced": n, "target": "live", **extra}

    # update
    _content(inp, creating=False)
    if inp.manifest is not None:
        manifest = _checked_manifest(ctx, org_de_l_app(row), inp.manifest)
    else:
        manifest = None
    if row.get("drafts_enabled") and (manifest is not None or inp.html is not None):
        return _update_brouillon(ctx, row, inp, manifest)
    if manifest is None:
        # Changer le titre ou la vignette fait aussi de moi l'auteur.
        _devenir_auteur_ou_refus(ctx, row)
    thumb: Any = ...
    if inp.clear_thumbnail:
        thumb = None
    elif inp.thumbnail is not None:
        thumb = _thumbnail(inp.thumbnail)
    extra = _avertir(manifest if manifest is not None else row["manifest"],
                     inp.html if inp.html is not None else row["html"])
    changed = _update(sub, row, inp.expected_rev, title=inp.title.strip() if inp.title else None,
                      manifest=manifest, html=inp.html, thumbnail=thumb)
    return {"app": _view(sub, changed), "target": "live", **extra}


def _lectures(sub: str, inp: AppInput) -> dict:
    """Le journal des lectures d'app (`app_reads`, `docs/apps.md` § Journal des
    lectures), à qui gouverne ce qu'il concerne — et rien d'autre :
    - avec `app_id`, à qui GOUVERNE l'app (propriétaire, admin, Manage) : toutes ses
      lectures (`tables` : celles-là) ; à qui ne la gouverne pas, les seules lectures
      des tableaux qu'il gouverne — sans même lire l'app, comme `revoke_through_app` ;
    - sans `app_id`, `tables` = des NUMÉROS de tableaux qu'on gouverne : leurs lectures,
      par toutes les apps."""
    jours = int(inp.days or 30)
    gere = lambda ns: ownership.can_govern(sub, ownership.TYPE_RESSOURCE_DATASTORE, str(ns))
    if inp.app_id is None:
        _require(bool(inp.tables) and all(str(t).isdigit() for t in inp.tables),
                 "missing_tables", "Name an app (`app_id`), or tables by number in `tables`.")
        ns_ids = sorted({int(t) for t in inp.tables})
        _require(all(gere(n) for n in ns_ids), "forbidden",
                 "Only who can share these tables reads who read them.", 403)
        return {"reads": [_ligne_lue(r) for r in db.apps.reads_of_tables(ns_ids, jours)]}
    app = db.apps.get(int(inp.app_id))
    if app is not None and ownership.can_govern(sub, APP_RTYPE, str(app["id"])):
        org_suspension.exiger(org_de_l_app(app))
        decl = app.get("manifest", {}).get("tables") or {}
        ns_ids = None
        if inp.tables:
            ns_ids = sorted({int(decl[t]["table"]) if t in decl else int(t)
                             for t in inp.tables if t in decl or str(t).isdigit()})
        alias = {str(t.get("table")): a for a, t in decl.items() if isinstance(t, dict)}
        return {"reads": [_ligne_lue(r, alias) for r in
                          db.apps.reads_of_app(int(app["id"]), jours, ns_ids)]}
    # Pas gérant de l'app : les lectures des tableaux qu'on gouverne, seules — ceux que
    # l'app déclare, ou qu'elle a lus. N'en gouverner aucun : le refus d'une app
    # illisible (rien ne dit qu'elle existe), ou 403 à qui la lit sans la gérer.
    _require(app is not None, "unknown_app",
             f"App {inp.app_id} doesn't exist or isn't shared with you.", 404)
    lignes = db.apps.reads_of_app(int(app["id"]), jours)
    candidats = {int(r["ns_id"]) for r in lignes} | {
        int(t["table"]) for t in ((app.get("manifest") or {}).get("tables") or {}).values()
        if isinstance(t, dict) and str(t.get("table") or "").isdigit()}
    if inp.tables:
        candidats &= {int(t) for t in inp.tables if str(t).isdigit()}
    gouvernes = {n for n in candidats if gere(n)}
    if not gouvernes:
        _require(_can(sub, int(app["id"]), "read"), "unknown_app",
                 f"App {inp.app_id} doesn't exist or isn't shared with you.", 404)
        raise AuthzDenied(403, "forbidden", "Only who manages this app, or the tables it "
                          "reads, reads its journal.")
    return {"reads": [_ligne_lue(r) for r in lignes if int(r["ns_id"]) in gouvernes]}


def _ligne_lue(r: dict, alias: Optional[dict] = None) -> dict:
    return {"app_id": r["app_id"], "app_title": r["app_title"],
            "table_id": str(r["ns_id"]), "table_name": r["table_name"],
            **({"table": alias.get(str(r["ns_id"]))} if alias is not None else {}),
            "sub": r["sub"], "email": r["email"], "access": r["access"],
            "day": str(r["day"]), "row_reads": r["row_reads"],
            "aggregate_reads": r["aggregate_reads"], "rows_served": r["rows_served"],
            "first_at": r["first_at"], "last_at": r["last_at"]}


def _retirer_par_le_tableau(sub: str, inp: AppInput) -> dict:
    """`revoke_through_app` par qui GOUVERNE les tableaux (`tables` : leurs NUMÉROS, tels
    que `oto_resource op=get` les liste sous `through_app`), sans droit sur l'app."""
    _require(inp.principal_type is not None and bool(inp.principal_id), "missing_principal",
             "`principal_type` and `principal_id` name whose access to remove.")
    _require(bool(inp.tables) and all(str(t).isdigit() for t in inp.tables), "missing_tables",
             "Without access to the app, name the tables by number in `tables`.")
    _require(all(ownership.can_govern(sub, ownership.TYPE_RESSOURCE_DATASTORE, str(t))
                 for t in inp.tables), "forbidden",
             "Only who can share these tables removes access to them.", 403)
    n = db.apps.revoke_through_app_tables(int(inp.app_id), inp.principal_type,
                                          str(inp.principal_id), [int(t) for t in inp.tables])
    return {"ok": n > 0}


def _roles(sub: str, row: dict, inp: AppInput) -> dict:
    """Les rôles d'app de ses partages (`apps_roles.py`) : les lire, en poser un. Le
    plan GOUVERNANCE, comme repartager (propriétaire, admin, Manage) : choisir ce qu'un
    lecteur voit, c'est choisir à qui l'app s'ouvre. Un rôle se pose sur un partage
    EXISTANT, et doit être déclaré par l'app (publiée, ou son brouillon : on prépare
    les rôles avant de publier)."""
    if inp.op == "revoke_through_app" and not ownership.can_govern(sub, APP_RTYPE,
                                                                   str(row["id"])):
        return _retirer_par_le_tableau(sub, inp)
    _require(ownership.can_govern(sub, APP_RTYPE, str(row["id"])), "forbidden",
             "Only who manages this app chooses its people's roles.", 403)
    manifest = row.get("manifest") or {}
    skipped = None
    if inp.op in ("set_role", "set_scope", "grant_through_app", "revoke_through_app"):
        _require(inp.principal_type is not None and bool(inp.principal_id), "missing_principal",
                 "`principal_type` and `principal_id` name who the app is shared with.")
    if inp.op in ("grant_through_app", "revoke_through_app"):
        vivants = {(g["principal_type"], g["principal_id"])
                   for g in db.apps.app_role_grants(str(row["id"]))}
        if inp.op == "revoke_through_app" and inp.tables:
            # Des tableaux nommés (noms de l'app, ou numéros) : ceux-là seulement.
            decl = manifest.get("tables") or {}
            ns = [int(decl[t]["table"]) if t in decl else int(t) for t in inp.tables
                  if t in decl or str(t).isdigit()]
            db.apps.revoke_through_app_tables(int(row["id"]), inp.principal_type,
                                              str(inp.principal_id), ns)
        elif inp.op == "revoke_through_app":
            db.apps.revoke_through_app(int(row["id"]), inp.principal_type, str(inp.principal_id))
        else:
            _require((inp.principal_type, str(inp.principal_id)) in vivants, "not_shared",
                     "This app isn't shared with them: share it first.", 404)
            declares = manifest.get("tables") or {}
            noms = inp.tables if inp.tables is not None else list(declares)
            inconnus = [n for n in noms if n not in declares]
            _require(not inconnus, "undeclared_table",
                     f"This app declares no table named {', '.join(inconnus)}.")
            skipped = []
            for alias in noms:
                t = declares[alias]
                ns = str(t.get("table"))
                # Ouvrir un tableau, même par l'app seule, c'est le PARTAGER : il faut
                # pouvoir le partager (gérant, propriétaire, admin).
                if not ownership.can_govern(sub, ownership.TYPE_RESSOURCE_DATASTORE, ns):
                    skipped.append({"table": alias, "reason": "you can't share this table"})
                    continue
                # Sous le partage vivant de l'app (son identité) : il a pu partir depuis
                # la lecture de `vivants`, et rien ne s'écrit alors.
                _require(db.apps.grant_through_app(
                    int(row["id"]), int(ns), inp.principal_type, str(inp.principal_id),
                    "write" if (t.get("set") or t.get("add")) else "read", sub),
                    "not_shared", "This app isn't shared with them: share it first.", 404)
    if inp.op == "set_role":
        # Changer le rôle de qui tient des tableaux PAR L'APP, c'est changer ce qu'il en
        # voit : il faut pouvoir partager chacun de ces tableaux.
        ouverts = [a for a in db.apps.through_app_of(int(row["id"]))
                   if (a["principal_type"], a["principal_id"])
                   == (inp.principal_type, str(inp.principal_id))]
        _require(all(ownership.can_govern(sub, ownership.TYPE_RESSOURCE_DATASTORE,
                                          str(a["ns_id"])) for a in ouverts),
                 "forbidden", "They see tables through this app that you can't share: "
                 "only who can share them changes their role.", 403)
        if inp.role is not None:
            brouillon = db.app_versions.draft(int(row["id"]))
            declares = set(manifest.get("roles") or {}) | set(
                ((brouillon or {}).get("manifest") or {}).get("roles") or {})
            _require(inp.role in declares, "unknown_role",
                     f"This app declares no role “{inp.role}”: add it to `manifest.roles`.")
        _require(db.apps.set_app_role(str(row["id"]), inp.principal_type,
                                      str(inp.principal_id), inp.role),
                 "not_shared", "This app isn't shared with them: share it first, then set "
                 "their role.", 404)
    if inp.op == "set_scope":
        _poser_perimetre(sub, row, inp)
    from .apps_roles import ordre
    roles = manifest.get("roles") or {}
    alias_de = {str(t.get("table")): a for a, t in (manifest.get("tables") or {}).items()}
    par_l_app: dict[tuple, list] = {}
    for a in db.apps.through_app_of(int(row["id"])):
        par_l_app.setdefault((a["principal_type"], a["principal_id"]), []).append(
            {"table": alias_de.get(str(a["ns_id"]), str(a["ns_id"])),
             "permission": a["permission"]})
    return {**({"skipped": skipped} if skipped is not None else {}),
            # Pour l'écran de partage : ce que chaque rôle borne par lignes (`rows`, par
            # nom de tableau : `$share` = la valeur à poser sur le partage, `$email` =
            # l'adresse vérifiée) et ses sections ; les numéros des tableaux nommés.
            "roles": [{"key": k, "label": roles[k].get("label") or k,
                       "rows": roles[k].get("rows") or None,
                       "sections": roles[k].get("sections")} for k in ordre(manifest)],
            "tables": {a: str(t.get("table")) for a, t in (manifest.get("tables") or {}).items()},
            "default_role": manifest.get("default_role"),
            "grants": [{"principal_type": g["principal_type"], "principal_id": g["principal_id"],
                        "share_role": g["role"], "app_role": g["app_role"],
                        "scope": {alias_de.get(ns, ns): v
                                  for ns, v in (g.get("app_scope") or {}).items()} or None,
                        "expires_at": g["expires_at"],
                        "through_app": par_l_app.get((g["principal_type"], g["principal_id"]), [])}
                       for g in db.apps.app_role_grants(str(row["id"]))]}


def _poser_perimetre(sub: str, row: dict, inp: AppInput) -> None:
    """`op=set_scope` : le PÉRIMÈTRE DE LIGNES d'un partage (`resource_grants.app_scope`,
    `docs/apps.md` § Périmètre de lignes) — `{tableau: {champ: valeur}}`, rangé par
    NUMÉRO de tableau. Le poser, c'est choisir quelles lignes d'un tableau ce lecteur
    voit ; le retirer ou le changer, c'est lui en ouvrir d'autres : il faut pouvoir
    PARTAGER chaque tableau du périmètre posé ET de celui qu'il remplace. Chaque champ
    est une colonne déclarée d'un type qu'une égalité compare ; chaque valeur un
    scalaire non vide (une égalité sur le vide verrait les lignes de personne)."""
    from ..datastore.core import make_store
    from .apps_roles import TYPES_DE_PERIMETRE, _valeur_de_perimetre

    brouillon = db.app_versions.draft(int(row["id"]))
    declares = {**(((brouillon or {}).get("manifest") or {}).get("tables") or {}),
                **((row.get("manifest") or {}).get("tables") or {})}
    numeros = {str(t.get("table")) for t in declares.values() if isinstance(t, dict)}
    raw = inp.scope or {}
    _require(isinstance(raw, dict), "invalid_scope", "`scope` maps a table to {field: value}.")
    neuf: dict[str, dict] = {}
    problemes: list[str] = []
    for nom, champs in raw.items():
        t = declares.get(nom)
        ns = str(t.get("table")) if isinstance(t, dict) else (nom if nom in numeros else None)
        if ns is None or not ns.isdigit():
            problemes.append(f"{nom}: this app declares no such table.")
            continue
        if not isinstance(champs, dict) or not champs:
            problemes.append(f"{nom}: a scope maps a field to a value.")
            continue
        _require(ownership.can_govern(sub, ownership.TYPE_RESSOURCE_DATASTORE, ns), "forbidden",
                 "Only who can share these tables chooses which of their rows someone "
                 "sees.", 403)
        schema = make_store(sub).get_schema(ns) or {}
        types = {f.get("key"): f.get("type") for f in schema.get("fields") or []
                 if isinstance(f, dict)}
        for champ, v in champs.items():
            if champ not in types:
                problemes.append(f"{nom}.{champ}: the table declares no such field.")
            elif types[champ] not in TYPES_DE_PERIMETRE:
                problemes.append(f"{nom}.{champ}: a scope can't use a {types[champ]} column.")
            elif not _valeur_de_perimetre(v):
                problemes.append(f"{nom}.{champ}: the value must be a non-empty text, "
                                 "number or boolean.")
        neuf.setdefault(ns, {}).update(champs)
    _require(not problemes, "invalid_scope", "; ".join(problemes))
    ancien = next((g.get("app_scope") or {} for g in db.apps.app_role_grants(str(row["id"]))
                   if (g["principal_type"], g["principal_id"])
                   == (inp.principal_type, str(inp.principal_id))), None)
    _require(ancien is not None, "not_shared",
             "This app isn't shared with them: share it first, then set their scope.", 404)
    touches = set(neuf) | set(ancien)
    _require(all(ownership.can_govern(sub, ownership.TYPE_RESSOURCE_DATASTORE, ns)
                 for ns in touches), "forbidden",
             "Only who can share these tables chooses which of their rows someone sees.", 403)
    _require(db.apps.set_app_scope(str(row["id"]), inp.principal_type, str(inp.principal_id),
                                   neuf or None),
             "not_shared", "This app isn't shared with them: share it first.", 404)


def _thumb_of(inp: AppInput) -> Any:
    if inp.clear_thumbnail:
        return None
    return _thumbnail(inp.thumbnail) if inp.thumbnail is not None else ...


def _update_brouillon(ctx: ResolvedCtx, row: dict, inp: AppInput,
                      manifest: Optional[dict]) -> dict:
    """`update` d'une app à brouillon : le manifeste et le HTML vont au brouillon ; le
    titre et la vignette — la carte, que les lecteurs voient dans la liste — en direct,
    s'ils sont donnés aussi. `expected_rev` nomme le brouillon (ou, sans lui, l'état
    vivant dont il naît)."""
    courant = db.app_versions.draft(row["id"])
    base_manifest = courant["manifest"] if courant else row["manifest"]
    base_html = courant["html"] if courant else row["html"]
    extra = _avertir(manifest if manifest is not None else base_manifest,
                     inp.html if inp.html is not None else base_html)
    # La carte se juge AVANT d'écrire quoi que ce soit : une vignette refusée ou un
    # auteur qui casserait l'app publiée ne laisse pas un brouillon écrit à moitié.
    thumb = _thumb_of(inp)
    carte = inp.title is not None or thumb is not ...
    if carte:
        _devenir_auteur_ou_refus(ctx, row)
    V.ecrire(ctx, row, expected_rev=inp.expected_rev, manifest=manifest, html=inp.html,
             auteur_ou_refus=_devenir_auteur_ou_refus)
    if carte:
        row = _update(ctx.sub, row, None, title=inp.title.strip() if inp.title else None,
                      thumbnail=thumb)
    return {"app": _view(ctx.sub, row), "target": "draft",
            "draft": V.resume(db.app_versions.draft(row["id"])), **extra}


def _update(sub: str, row: dict, expected_rev: Optional[str], **fields) -> dict:
    try:
        out = db.apps.update(row["id"], by=sub, expected_rev=expected_rev, **fields)
    except db.apps.AppConflict as e:
        raise AuthzDenied(409, "conflict",
                          f"This app changed since you read it (now at rev {e.current_rev}): "
                          "read it again before writing.") from None
    _require(out is not None, "unknown_app", f"App {row['id']} doesn't exist.", 404)
    return out


# ── Liens vers l'extérieur : un choix d'instance ──────────────────────────────
#
# L'hôte qui affiche une app peut ouvrir, après l'accord du lecteur, un site hors de
# l'app (`oto.open(url)`, ou un `<a href="https://…">` que le pont lui remet : message
# `open-link`, `docs/apps.md` §Liens vers l'extérieur). Tous les hôtes ne le font pas :
# sur un hôte qui ne gère pas `open-link`, `oto.open` n'existe pas, et un agent qui
# suivrait la description écrirait un appel qui lève. L'instance le DÉCLARE donc,
# explicitement, et la description d'`oto_apps` ne nomme `open(url)` que dans ce cas.
# Ce n'est pas un repli : c'est ce que sait faire l'hôte de CETTE instance. Lu au
# chargement du module (la description d'un outil est figée au démarrage) ; une valeur
# illisible lève, l'instance refuse de démarrer.
ENV_LIENS_EXTERIEURS = "OTO_APPS_LIENS_EXTERIEURS"


def liens_exterieurs() -> bool:
    """L'hôte des apps de cette instance ouvre-t-il un lien vers l'extérieur ?"""
    return interrupteurs.oui_non(ENV_LIENS_EXTERIEURS,
                                 os.environ.get(ENV_LIENS_EXTERIEURS))


_PONT_LIENS_EXTERIEURS = (
    " (or a link <a href=\"#/route\">) / open(url): a site outside the app, from a "
    "click handler — or just a plain <a href=\"https://…\">, preferred. The viewer is shown the whole address and it "
    "opens in a new tab once they confirm (a target on the link is ignored); http(s) "
    "only — a mailto: or other non-http(s), non-# link closes the app; no user part "
    "(https://a@b/); only right after a viewer's click or key, never on load or a "
    "timer. Never window.open or location.href (they do nothing in the sandbox); keep "
    "addresses readable, no data packed in the query.")


def description_app(liens: bool) -> str:
    """La description servie d'`oto_apps` ; `liens` = l'instance déclare que son hôte
    ouvre un lien vers l'extérieur (`liens_exterieurs`)."""
    return (
            "Apps: screens people build on the platform — dashboards, review queues. "
            "Before building or changing one, read the guide: oto_guide op=read "
            "slug=build-apps. "
            "An app is a manifest (the tables, fields and agents it touches) plus HTML "
            "that runs sandboxed and talks to the platform through its `window` "
            "bridge. Not a memory page. An app has its OWN owner and shares, like a "
            "table: a new one belongs to you, in your active organization, private "
            "unless the org shares new apps by default; share or transfer it with "
            "oto_resource (resource_type=app). Projects only FILE apps (shown on the "
            "project page), they give no access.\n"
            "op=list (no project: your active org's apps; `project_ids`: those filed "
            "there; cards, no HTML) / get (`app_id`; with manifest, HTML and `rev`) / "
            "create (`title`, `manifest`, `html`, optional `thumbnail`, optional "
            "`project_id` to file it) / read_file (`app_id`, `file_id`: a file a person "
            "dropped in the app, for the run they started — PDF text, a sheet as CSV, "
            "text inline, plus a signed link to the original) / put_file (`app_id`, `name`, "
            "`content` or `content_base64`, `media_type`: hand a RESULT file back to the "
            "app, for the person whose run this is — they get it with oto.files() / "
            "oto.download; PDF, Word, Excel, text, CSV, JSON, 20 MB) / update (`app_id`, any of those; pass "
            "`expected_rev` from get → 409 if it changed) / patch (`app_id`, `edits`: "
            "exact find → replace in the HTML, each `find` unique, all or nothing; "
            "returns `rev`, `replaced` and `warnings`, never the HTML — prefer it to "
            "update for any change smaller than the whole page) / archive / unarchive / "
            "revisions (each with `published_at` and `note` when it was a published "
            "version) / revision (`revision_id`: one version in full, to read or compare "
            "before restoring; editors) / restore (`revision_id`, optional `note`; "
            "`to_draft: true` puts it in the draft instead of making it live) / file / "
            "unfile (`app_id`, `project_id`) / validate (`manifest`, and `app_id` for an "
            "existing app, optional `html`: the problems a create would refuse, nothing "
            "written).\n"
            "Roles: the manifest may declare `roles` {name: {label, tables?: {table: true | "
            "{fields?, set?, add?}}, rows?: {table: {field: \"$email\" | \"$share\"}}, "
            "agents?: [names], files?, sections?: [names]}} and a "
            "required `default_role`; each role only NARROWS the manifest (a key it omits "
            "keeps the manifest's). A viewer who can't edit the app gets the role set on "
            "their share (op=set_role `app_id`, `principal_type`, `principal_id`, `role`; "
            "op=roles lists roles and shares; managers only), else `default_role` — enforced "
            "on every app call; op=get returns the role's narrowed manifest and `access` {role, "
            "sections}. Sections: mark markup with `data-oto-section=\"name\"` (several "
            "names, space-separated: shown if the role opens any); op=get REMOVES every "
            "marked element (with its content and inner scripts) whose sections the viewer's "
            "role doesn't open — it never reaches them. Only marked markup is protected: data "
            "in an unmarked <script> or built by JS still ships to everyone, so keep "
            "section-only content inside marked elements. While a role declares `sections` (unless "
            "`strict_sections: false`), every <script>, <style> and on… handler MUST be marked "
            "— with a section, or `data-oto-section=\"*\"` for code every role gets — or the "
            "write is refused (sections_not_classified). A role with `rows` can't add or set "
            "its table's unique key. To let someone use the app WITHOUT any "
            "access to its tables elsewhere: op=grant_through_app (`app_id`, "
            "`principal_type`, `principal_id`, optional `tables`; you must be able to share "
            "each table) — they then read and write those tables only inside this app, as "
            "their role allows; op=revoke_through_app removes it. `rows` keeps a viewer to "
            "rows matching their verified email or their share's value (op=set_scope, "
            "same principal, `scope` {table: {field: value}}, which also limits alone; "
            "share each table); unresolved = no rows. "
            "op=reads (`app_id`, "
            "optional `tables`, `days` ≤ 180, default 30): who read what through the app, "
            "per day — counts of row reads, aggregates and rows served, and whether "
            "through their own access, the app's, or an agent run (managers; a table's "
            "manager sees that table only, also without `app_id` given `tables` by "
            "number).\n"
            "Drafts: an app with `drafts_enabled` (op=set_drafts `enabled`) keeps its "
            "viewers on the PUBLISHED version — update and patch write its manifest and "
            "HTML to a DRAFT (`target: \"draft\"` in the reply; title and thumbnail stay "
            "live). op=get answers the published app, plus `draft` {rev} for editors; "
            "read the draft with op=get `draft: true` and write against ITS rev "
            "(409 draft_exists if you pass the published rev while a draft exists). "
            "op=publish (`expected_rev` = the draft's rev, optional `note`) makes the draft "
            "live for everyone and you its author; op=delete_draft drops it. "
            "create, update, patch, restore, publish and validate also return `warnings`: "
            "oto.rows / oto.aggregate calls that sort, filter, group or measure on a "
            "field the manifest doesn't declare (`_created_at` / `_updated_at` are "
            "always sortable and filterable) — the open app would refuse them, so fix "
            "them first. Every declared "
            "table and agent must be within YOUR reach and in the app's organization: "
            "otherwise 400 invalid_manifest, each problem listed.\n"
            "In the HTML, `window.oto`: rows(table, {filter, orderBy, limit}) / "
            "aggregate(table, {groupBy, metrics, filter}) / set(table, rowId, field, value) / "
            "add(table, values) / upload(file) → {id, name, mediaType, size} (manifest "
            "`files: true`; PDF, Word, Excel, text, CSV, JSON; 20 MB a file, 1 GB per "
            "viewer per app, kept 30 days) / files() → each with origin upload or agent / "
            "download(fileId) (opens the viewer's own file) / "
            "runAgent(agent, {input, files: [ids]}) → {jobId} — the run gets each file's "
            "text and reads it in full with op=read_file / job(jobId) / runs(agent) / "
            "go(route)"
            + (_PONT_LIENS_EXTERIEURS if liens else ".") + " "
            "A click in the app does NOT approve anything: every set, add, "
            "upload and runAgent is shown by the HOST screen (outside the app) with what "
            "it writes, and runs only once the viewer confirms there — cancelled, it "
            "rejects. Ask for one gesture at a time, from a click. One run of "
            "an agent waits at a time (`run_already_queued`): collect files, then one "
            "runAgent with files: [...]."
    )


CAPABILITIES += [
    Capability(
        key="me.app", handler=_app, Input=AppInput, Output=AppOut, authz=SUB_ONLY,
        description=description_app(liens_exterieurs()),
        mcp="oto_apps",
        rest=RestBinding("POST", "/api/me/apps", allow_api_token=False),
    ),
]


# ── Le kind `app` d'`ownership` : propriétaire, partages, org de rangement ──────

def _proprietaire(rid: str) -> Optional[tuple[str, str]]:
    row = db.apps.owner(int(rid))
    if row is None or not row.get("owner_type") or row.get("owner_id") is None:
        return None
    return (row["owner_type"], str(row["owner_id"]))


def _org_de_rangement(rid: str) -> Optional[int]:
    row = db.apps.owner(int(rid))
    return org_de_l_app(row) if row else None


def _reparenter(rid: str, new_type: str, new_id: str) -> None:
    """Donner l'app à une PERSONNE la range dans une org où elle est : celle où l'app
    vivait si elle en est membre (ses tableaux et ses agents y restent lisibles), sinon
    sa maison (son org active) — la règle des projets (`ownership._project_context_org`). Sans elle,
    l'app resterait dans une org que son nouveau propriétaire ne voit pas."""
    from .. import org_store, roles

    ctx = None
    if new_type == "user":
        avant = org_de_l_app(db.apps.owner(int(rid)) or {})
        ctx = (avant if avant is not None and roles.is_org_member(new_id, int(avant))
               else org_store.ensure_home_org(new_id))
    db.apps.reparent(int(rid), new_type, new_id, context_org_id=ctx)


ownership.register_kind(APP_RTYPE, ownership.ResourceKind(
    owner_getter=_proprietaire,
    reparent=_reparenter,
    context_org=_org_de_rangement,
))
