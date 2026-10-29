"""Apps : écrans bâtis par les utilisateurs, rangés dans un projet (`oto_apps`,
`POST /api/me/apps`).

**Une app n'est pas une page de mémoire.** Jusqu'ici une app vivait dans une page
(un bloc de code dans un `oto_doc`) : elle polluait la mémoire du projet et la
recherche, la liste des apps devait les DEVINER par recherche plein texte, et le
serveur ne savait ni la valider ni dire qui l'avait écrite. Elle a désormais sa table
(`db/schema/apps.py`), ses révisions et cette capacité — `docs/apps.md`.

**L'accès est celui du projet** (`ownership.can_access`, lecture / écriture), comme
une page. Une app inconnue OU illisible rend le même 404 : rien ne dit qu'elle existe.

**Le manifeste se juge à l'écriture** (`apps_manifeste.py`) — création, mise à jour
ET restauration d'une révision : sa forme, que chaque tableau, champ et agent déclarés
sont à la portée de l'AUTEUR, et qu'ils vivent dans l'org du PROJET de l'app. Un
manifeste invalide est refusé en 400 `invalid_manifest` avec la liste de ce qui ne va
pas ; `op=validate` rend cette liste sans rien écrire.

**Écrire une app est réservé à la population bêta** (option `beta`, la même que celle
qui ouvre `oto_apps` et l'écran du front) : `create`, `update`, `archive`,
`unarchive`, `restore` refusent 403 `apps_beta_only` aux autres, sur les deux faces,
fail-closed. Lire une app reste ouvert à qui lit son projet.

**Face REST : session interactive seule** (`allow_api_token=False`) — aucun jeton
`oto_`, délégation d'agent hébergé comprise : une app s'écrit par un humain ou par son
agent en MCP, pas par un script porteur de clé.
"""
from __future__ import annotations

import logging
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from .. import access, db, ownership
from ..tool_visibility import BETA_OPTION
from ._authz import SUB_ONLY
from ._types import AuthzDenied, Capability, DeclaredError, ResolvedCtx, RestBinding
from .apps_html import avertissements
from .apps_manifeste import auteur, forme, org_du_projet
from .registry import CAPABILITIES

logger = logging.getLogger(__name__)

PROJECT_RTYPE = "project"
MAX_HTML = 512_000
MAX_TITLE = 200
MAX_PROJECTS = 200
_THUMB_KEYS = {"image", "color", "emoji", "design"}
_ECRITURES = frozenset({"create", "update", "archive", "unarchive", "restore"})


class AppInput(BaseModel):
    op: Literal["list", "get", "create", "update", "archive", "unarchive",
                "revisions", "restore", "validate"]
    project_id: Optional[int] = None
    project_ids: Optional[list[int]] = Field(
        default=None, description="list: the projects to list apps from (at most 200).")
    app_id: Optional[int] = None
    title: Optional[str] = None
    manifest: Optional[dict] = None
    html: Optional[str] = None
    thumbnail: Optional[dict] = None
    clear_thumbnail: Optional[bool] = None
    expected_rev: Optional[str] = None
    revision_id: Optional[int] = None
    archived: Optional[bool] = None


class AppCard(BaseModel):
    id: int
    project_id: int
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


class AppRevision(BaseModel):
    id: int
    title: str
    edited_by: Optional[str] = None
    created_at: Optional[Any] = None


class AppOut(BaseModel):
    app: Optional[AppView] = None
    apps: Optional[list[AppCard]] = None
    revisions: Optional[list[AppRevision]] = None
    # validate : vide = le manifeste passe.
    problems: Optional[list[str]] = None
    ok: Optional[bool] = None
    # create, update, restore, validate : ce que le HTML demande hors du manifeste
    # (`apps_html.py`) — un avertissement, l'app est écrite quand même.
    warnings: Optional[list[str]] = None


def _require(cond, code: str, msg: str, status: int = 400) -> None:
    if not cond:
        raise AuthzDenied(status, code, msg)


def _can(sub: str, project_id: int, want: str) -> bool:
    return ownership.can_access(sub, PROJECT_RTYPE, str(project_id), want)


def _app_or_404(sub: str, app_id: Optional[int], want: str) -> dict:
    _require(app_id is not None, "missing_app_id", "`app_id` is required for this op.")
    row = db.apps.get(int(app_id))
    # Inconnue ou illisible : la même réponse, rien ne dit qu'elle existe.
    _require(row is not None and _can(sub, row["project_id"], "read"),
             "unknown_app", f"App {app_id} doesn't exist or isn't shared with you.", 404)
    if want == "write":
        _require(_can(sub, row["project_id"], "write"), "forbidden",
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


def _beta_ou_refus(ctx: ResolvedCtx) -> None:
    """Écrire une app : la population `beta` seule, celle du front et de `oto_apps`.
    Fail-CLOSED : une panne du seam des options refuse, et le journal le dit."""
    try:
        ok = access.has_option(ctx.sub, BETA_OPTION, org=ctx.org_id)
    except Exception:
        logger.warning("apps beta lookup fail-CLOSED for %s in org %s", ctx.sub,
                       ctx.org_id, exc_info=True)
        ok = False
    if not ok:
        raise AuthzDenied(403, "apps_beta_only",
                          "Writing apps is open to beta accounts only for now.")


def _problemes(sub: str, project_id: int, raw: Optional[dict]) -> tuple[dict, list[str]]:
    clean, problems = forme(raw if raw is not None else {})
    if not problems:
        problems = auteur(sub, org_du_projet(project_id), clean)
    return clean, problems


def _checked_manifest(ctx: ResolvedCtx, project_id: int, raw: Optional[dict]) -> dict:
    clean, problems = _problemes(ctx.sub, project_id, raw)
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


def _view(row: dict) -> dict:
    return {k: row[k] for k in AppView.model_fields if k in row}


def _ecrite(row: dict) -> dict:
    """La réponse d'une écriture : l'app, et ce que son HTML demande hors du manifeste."""
    out: dict = {"app": _view(row)}
    warnings = avertissements(row.get("manifest") or {}, row.get("html"))
    if warnings:
        out["warnings"] = warnings
    return out


def _app(ctx: ResolvedCtx, inp: AppInput) -> dict:
    sub = ctx.sub

    if inp.op == "list":
        ids = list(inp.project_ids or ([] if inp.project_id is None else [inp.project_id]))
        _require(ids, "missing_project", "`project_ids` (or `project_id`) is required to list apps.")
        _require(len(ids) <= MAX_PROJECTS, "too_many_projects",
                 f"At most {MAX_PROJECTS} projects per list.")
        owners = db.apps.project_owners(ids)
        readable = ownership.can_access_many(sub, PROJECT_RTYPE, owners, "read") if owners else {}
        allowed = [int(pid) for pid, ok in readable.items() if ok]
        return {"apps": db.apps.list_for_projects(allowed, archived=inp.archived is True)}

    if inp.op in _ECRITURES:
        _beta_ou_refus(ctx)

    if inp.op == "validate":
        # Jugé contre l'org du projet où l'app vivra : celui nommé, ou celui de l'app.
        app = None
        if inp.app_id is not None:
            app = _app_or_404(sub, inp.app_id, "read")
            pid = app["project_id"]
        else:
            _require(inp.project_id is not None, "missing_project",
                     "`project_id` (or `app_id`) is required to validate a manifest.")
            _require(_can(sub, int(inp.project_id), "read"), "forbidden",
                     "You can't read this project.", 403)
            pid = int(inp.project_id)
        clean, problems = _problemes(sub, pid, inp.manifest)
        # Le HTML se juge contre le manifeste passé, sinon contre celui de l'app — et
        # pas contre un manifeste à corriger : chaque appel y paraîtrait hors manifeste.
        html = inp.html if inp.html is not None else (app or {}).get("html")
        out: dict = {"ok": not problems, "problems": problems}
        warnings = [] if problems else avertissements(
            clean if inp.manifest is not None else (app or {}).get("manifest") or {}, html)
        if warnings:
            out["warnings"] = warnings
        return out

    if inp.op == "create":
        _require(inp.project_id is not None, "missing_project", "`project_id` is required.")
        _require(_can(sub, int(inp.project_id), "write"), "forbidden",
                 "You can't add an app to this project.", 403)
        _content(inp, creating=True)
        manifest = _checked_manifest(ctx, int(inp.project_id), inp.manifest)
        row = db.apps.create(int(inp.project_id), title=inp.title.strip(), manifest=manifest,
                             html=inp.html or "", thumbnail=_thumbnail(inp.thumbnail), by=sub)
        db.log_project_activity(int(inp.project_id), sub, "app.create", row["title"])
        return _ecrite(row)

    row = _app_or_404(sub, inp.app_id, "read" if inp.op in ("get", "revisions") else "write")

    if inp.op == "get":
        return {"app": _view(row)}

    if inp.op == "revisions":
        return {"revisions": db.apps.revisions(row["id"])}

    if inp.op in ("archive", "unarchive"):
        out = db.apps.set_archived(row["id"], inp.op == "archive")
        db.log_project_activity(row["project_id"], sub, f"app.{inp.op}", row["title"])
        return {"app": _view(out)}

    if inp.op == "restore":
        _require(inp.revision_id is not None, "missing_revision", "`revision_id` is required.")
        old = db.apps.revision(row["id"], int(inp.revision_id))
        _require(old is not None, "unknown_revision", "No such revision of this app.", 404)
        # Restaurer, c'est RÉÉCRIRE l'app sous son nom : le manifeste d'hier est rejugé
        # contre celui qui restaure (il devient l'auteur) et l'org du projet d'aujourd'hui.
        changed = _update(sub, row, inp.expected_rev, title=old["title"],
                          manifest=_checked_manifest(ctx, row["project_id"], old["manifest"]),
                          html=old["html"], thumbnail=old["thumbnail"])
        db.log_project_activity(row["project_id"], sub, "app.restore", row["title"])
        return _ecrite(changed)

    # update
    _content(inp, creating=False)
    manifest = (_checked_manifest(ctx, row["project_id"], inp.manifest)
                if inp.manifest is not None else None)
    thumb: Any = ...
    if inp.clear_thumbnail:
        thumb = None
    elif inp.thumbnail is not None:
        thumb = _thumbnail(inp.thumbnail)
    changed = _update(sub, row, inp.expected_rev, title=inp.title.strip() if inp.title else None,
                      manifest=manifest, html=inp.html, thumbnail=thumb)
    db.log_project_activity(row["project_id"], sub, "app.update", changed["title"])
    return _ecrite(changed)


def _update(sub: str, row: dict, expected_rev: Optional[str], **fields) -> dict:
    try:
        out = db.apps.update(row["id"], by=sub, expected_rev=expected_rev, **fields)
    except db.apps.AppConflict as e:
        raise AuthzDenied(409, "conflict",
                          f"This app changed since you read it (now at rev {e.current_rev}): "
                          "read it again before writing.") from None
    _require(out is not None, "unknown_app", f"App {row['id']} doesn't exist.", 404)
    return out


CAPABILITIES += [
    Capability(
        key="me.app", handler=_app, Input=AppInput, Output=AppOut, authz=SUB_ONLY,
        description=(
            "Apps: screens people build on the platform — dashboards, review queues — "
            "kept in a project and opened with the project's access. An app is a "
            "manifest (the tables and fields it reads; `set`, `add`, `agents` and "
            "`files` are checked but not served yet: an open app only reads) plus HTML "
            "that runs sandboxed and talks to the platform through its `window` "
            "bridge. Not a memory page.\n"
            "op=list (`project_ids`; cards, no HTML) / get (`app_id`; with manifest, "
            "HTML and `rev`) / create (`project_id`, `title`, `manifest`, `html`, "
            "optional `thumbnail`) / update (`app_id`, any of those; pass "
            "`expected_rev` from get → 409 if it changed) / archive / unarchive / "
            "revisions / restore (`revision_id`) / validate (`manifest` and "
            "`project_id` or `app_id`, optional `html`: the problems a create would "
            "refuse, nothing written). create, update, restore and validate also "
            "return `warnings`: calls in the HTML that sort, filter, group or measure "
            "on a field the manifest doesn't declare (`_created_at` / `_updated_at` "
            "are always sortable and filterable) — the open app would refuse them, "
            "so fix them before anyone opens it. Every declared table and agent must be within YOUR reach and "
            "in the project's organization: otherwise 400 invalid_manifest, each "
            "problem listed. Writing apps is open to beta accounts only."
        ),
        mcp="oto_apps",
        rest=RestBinding("POST", "/api/me/apps", allow_api_token=False),
        errors=(DeclaredError(403, "apps_beta_only",
                              "create, update, archive, unarchive or restore by an "
                              "account without the beta option"),),
    ),
]
