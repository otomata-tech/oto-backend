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
pas ; `op=validate` rend cette liste sans rien écrire.

**Écrire une app est réservé à la population bêta** (option `beta`, la même que celle
qui ouvre `oto_apps` et l'écran du front) : `create`, `update`, `archive`,
`unarchive`, `restore`, `file`, `unfile` refusent 403 `apps_beta_only` aux autres, sur
les deux faces, fail-closed. Lire une app reste ouvert à qui la lit.

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
from .apps_manifeste import auteur, forme, org_de_l_app
from .orgs import partage_par_defaut
from .registry import CAPABILITIES

logger = logging.getLogger(__name__)

PROJECT_RTYPE = "project"
APP_RTYPE = "app"
MAX_HTML = 512_000
MAX_TITLE = 200
MAX_PROJECTS = 200
_THUMB_KEYS = {"image", "color", "emoji", "design"}
_ECRITURES = frozenset({"create", "update", "archive", "unarchive", "restore", "file",
                        "unfile"})


class AppInput(BaseModel):
    op: Literal["list", "get", "create", "update", "archive", "unarchive",
                "revisions", "restore", "validate", "file", "unfile", "read_file"]
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
    revision_id: Optional[int] = None
    archived: Optional[bool] = None
    file_id: Optional[int] = Field(
        default=None, description="read_file: a file a person dropped in the app (its id "
        "comes with the run's input).")


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
    # read_file : le fichier rendu pour un agent (`file_content.render_for_agent`).
    file: Optional[dict] = None


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


def _cards(sub: str, rows: list[dict], writable: Optional[set[int]] = None) -> list[dict]:
    """Les cartes servies : de quoi dessiner la tuile, où l'app est rangée, et si
    l'appelant peut la modifier. `writable` : les apps que la liste SAIT modifiables
    (possédées, ou partagées en écriture) — sans lui, chaque carte se juge."""
    filed = db.apps.projects_of(r["id"] for r in rows)
    return [{**r, "project_ids": filed.get(int(r["id"]), []),
             "can_edit": (int(r["id"]) in writable if writable is not None
                          else _can(sub, r["id"], "write"))} for r in rows]


def _view(sub: str, row: dict) -> dict:
    row = _cards(sub, [row])[0]
    return {k: row[k] for k in AppView.model_fields if k in row}


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
    granted = []
    for r in db.apps.list_granted_to(ownership.principaux_de_liste(sub, org), archived=archived):
        if r.get("permission") == "write":
            writable.add(int(r["id"]))
        if not any(int(o["id"]) == int(r["id"]) for o in owned):
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

    if inp.op in _ECRITURES:
        _beta_ou_refus(ctx)

    if inp.op == "validate":
        # Jugé contre l'org où l'app vit : la sienne, ou l'org active pour une app à
        # créer.
        org = (org_de_l_app(_app_or_404(sub, inp.app_id, "read")) if inp.app_id is not None
               else ctx.org_id)
        _, problems = _problemes(sub, org, inp.manifest)
        return {"ok": not problems, "problems": problems}

    if inp.op == "create":
        _require(ctx.org_id is not None, "no_active_org",
                 "An app is created in your active organization: pick one first.")
        if inp.project_id is not None:
            _require(_can_project(sub, int(inp.project_id), "write"), "forbidden",
                     "You can't file an app in this project.", 403)
        _content(inp, creating=True)
        manifest = _checked_manifest(ctx, int(ctx.org_id), inp.manifest)
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
        return {"app": _view(sub, row)}

    row = _app_or_404(sub, inp.app_id,
                      "read" if inp.op in ("get", "revisions", "read_file") else "write")

    if inp.op == "get":
        return {"app": _view(sub, row)}

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
        changed = _update(sub, row, inp.expected_rev, title=old["title"],
                          manifest=_checked_manifest(ctx, org_de_l_app(row), old["manifest"]),
                          html=old["html"], thumbnail=old["thumbnail"])
        return {"app": _view(sub, changed)}

    # update
    _content(inp, creating=False)
    if inp.manifest is not None:
        manifest = _checked_manifest(ctx, org_de_l_app(row), inp.manifest)
    else:
        manifest = None
        # Toute écriture fait de son auteur celui de l'app (`updated_by`), dont les
        # droits bornent chaque lecteur. Changer le titre ou la vignette sans pouvoir
        # lire ce que l'app déclare la casserait pour tous : rejugé, refusé nommé.
        if row.get("updated_by") != ctx.sub:
            _, problems = _problemes(ctx.sub, org_de_l_app(row), row["manifest"])
            if problems:
                raise AuthzDenied(
                    403, "would_break_app",
                    "Saving would make you this app's author, and you can't reach "
                    "everything it uses: " + " ".join(problems))
    thumb: Any = ...
    if inp.clear_thumbnail:
        thumb = None
    elif inp.thumbnail is not None:
        thumb = _thumbnail(inp.thumbnail)
    changed = _update(sub, row, inp.expected_rev, title=inp.title.strip() if inp.title else None,
                      manifest=manifest, html=inp.html, thumbnail=thumb)
    return {"app": _view(sub, changed)}


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
            "Apps: screens people build on the platform — dashboards, review queues. "
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
            "text inline, plus a signed link to the original) / update (`app_id`, any of those; pass "
            "`expected_rev` from get → 409 if it changed) / archive / unarchive / "
            "revisions / restore (`revision_id`) / file / unfile (`app_id`, "
            "`project_id`) / validate (`manifest`, and `app_id` for an existing app: "
            "the problems a create would refuse, nothing written). Every declared "
            "table and agent must be within YOUR reach and in the app's organization: "
            "otherwise 400 invalid_manifest, each problem listed. Writing apps is open "
            "to beta accounts only.\n"
            "In the HTML, `window.tulina`: rows(table, {filter, orderBy, limit}) / "
            "aggregate(table, {groupBy, metrics, filter}) / set(table, rowId, field, value) / "
            "add(table, values) / upload(file) → {id, name, mediaType, size} (manifest "
            "`files: true`; PDF, Word, Excel, text, CSV, JSON; 20 MB a file, 1 GB per "
            "viewer per app, kept 30 days) / files() / "
            "runAgent(agent, {input, files: [ids]}) → {jobId} — the run gets each file's "
            "text and reads it in full with op=read_file / job(jobId) / runs(agent) / "
            "go(route). A click in the app approves what it does — act right after it "
            "(the browser keeps a click about 5 s; a write after a long wait asks the "
            "viewer); anything the app does on its own asks the viewer first. One run of "
            "an agent waits at a time (`run_already_queued`): collect files, then one "
            "runAgent with files: [...]."
        ),
        mcp="oto_apps",
        rest=RestBinding("POST", "/api/me/apps", allow_api_token=False),
        errors=(DeclaredError(403, "apps_beta_only",
                              "create, update, archive, unarchive, restore, file or "
                              "unfile by an account without the beta option"),),
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
    son org perso — la règle des projets (`ownership._project_context_org`). Sans elle,
    l'app resterait dans une org que son nouveau propriétaire ne voit pas."""
    from .. import org_store, roles

    ctx = None
    if new_type == "user":
        avant = org_de_l_app(db.apps.owner(int(rid)) or {})
        ctx = (avant if avant is not None and roles.is_org_member(new_id, int(avant))
               else org_store.ensure_personal_org(new_id))
    db.apps.reparent(int(rid), new_type, new_id, context_org_id=ctx)


ownership.register_kind(APP_RTYPE, ownership.ResourceKind(
    owner_getter=_proprietaire,
    reparent=_reparenter,
    context_org=_org_de_rangement,
))
