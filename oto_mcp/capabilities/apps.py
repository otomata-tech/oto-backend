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

from .. import db, interrupteurs, ownership
from ._authz import SUB_ONLY
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .apps_html import avertissements
from . import apps_versions as V
from .apps_manifeste import auteur, forme, org_de_l_app
from .orgs import partage_par_defaut
from .remplacements import RemplacementRefuse, appliquer
from .registry import CAPABILITIES


PROJECT_RTYPE = "project"
APP_RTYPE = "app"
MAX_HTML = 512_000
MAX_TITLE = 200
MAX_PROJECTS = 200
_THUMB_KEYS = {"image", "color", "emoji", "design"}
_ECRITURES = frozenset({"create", "update", "patch", "archive", "unarchive", "restore",
                        "file", "unfile", "publish", "delete_draft", "set_drafts"})


class AppInput(BaseModel):
    op: Literal["list", "get", "create", "update", "patch", "archive", "unarchive",
                "revisions", "revision", "restore", "validate", "file", "unfile",
                "read_file", "put_file", "publish", "delete_draft", "set_drafts"]
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


class AppCard(BaseModel):
    id: int
    owner_type: Optional[str] = None
    owner_id: Optional[str] = None
    # Les projets où l'app est rangée — affichage seulement, aucun accès.
    project_ids: list[int] = Field(default_factory=list)
    # L'appelant peut-il la modifier (éditeur, gérant, propriétaire) ?
    can_edit: bool = False
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


def _cards(sub: str, rows: list[dict], writable: Optional[set[int]] = None) -> list[dict]:
    """Les cartes servies : de quoi dessiner la tuile, où l'app est rangée (parmi les
    projets que l'appelant lit), et s'il peut la modifier. `writable` : les apps que la
    liste SAIT modifiables (possédées, ou partagées en écriture) — sans lui, chaque
    carte se juge."""
    rangees = db.apps.projects_of(r["id"] for r in rows)
    lus = _projets_lisibles(sub, rangees)
    filed = {a: [p for p in pids if p in lus] for a, pids in rangees.items()}
    return [{**r, "project_ids": filed.get(int(r["id"]), []),
             "can_edit": (int(r["id"]) in writable if writable is not None
                          else _can(sub, r["id"], "write"))} for r in rows]


def _view(sub: str, row: dict) -> dict:
    row = _cards(sub, [row])[0]
    return {k: row[k] for k in AppView.model_fields if k in row}


def _avertir(manifest: Optional[dict], html: Optional[str]) -> dict:
    """`{"warnings": […]}` quand le HTML demande hors du manifeste, sinon `{}`.
    Appelé AVANT l'écriture : un défaut du lecteur de HTML refuse sans rien écrire,
    plutôt qu'un 500 après une création commitée qu'un agent rejouerait en doublon."""
    w = avertissements(manifest or {}, html)
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
        clean, problems = _problemes(sub, org, inp.manifest)
        out: dict = {"ok": not problems, "problems": problems}
        if not problems:
            manifeste = app["manifest"] if inp.manifest is None and app is not None else clean
            html = inp.html if inp.html is not None else (app or {}).get("html")
            out.update(_avertir(manifeste, html))
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

    row = _app_or_404(sub, inp.app_id,
                      "read" if inp.op in ("get", "revisions", "read_file", "put_file") else "write")

    if inp.op == "get":
        view = _view(sub, row)
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
