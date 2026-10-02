"""`oto_recipe` — les recettes : un outil de connecteur vers un tableau, sans modèle.

Une recette dit quel outil appeler, avec quels arguments, comment parcourir ses
pages, quel champ va dans quelle colonne et quelle colonne identifie une ligne. Un
agent l'écrit une fois — `sample` montre la forme de ce que l'outil rend, `test`
l'éprouve sur une vraie page sans rien écrire — puis le serveur l'exécute (`run`)
autant qu'on veut : l'agent ne relit plus les pages et ne les recopie plus.

**Proposer et publier sont ouverts au membre, dans sa portée** : une recette n'appelle
que des outils que son exécutant peut déjà appeler, et son plafond de dépense
(`limits.max_units`) est obligatoire. **Publier éprouve** : une page réelle, sans
écriture, qui doit produire des lignes ; le remplissage par colonne est gardé avec la
version.

⚠️ **Refusé dans un agent hébergé, pour l'instant.** Le jeton d'un travail du runner ne
porte pas la liste d'outils de son déclencheur ; sans elle, une recette pourrait faire
appeler à un agent un outil que sa liste ne lui donne pas. Le lien jeton → travail
viendra dans un lot à part.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .. import session_org, tool_alias, tool_registry
from ..auth.hooks import current_token_axes
from ..db import recipes as db_recipes
from ..recipes import contrat, moteur
from ._authz import BY_OP, ORG_MEMBER_OPT, SUB_ONLY
from ._types import AuthzDenied, Capability, ResolvedCtx
from .registry import CAPABILITIES

_PORTEE = BY_OP({None: ORG_MEMBER_OPT("org"), "org": ORG_MEMBER_OPT("org"),
                 "user": SUB_ONLY}, fields=("scope",))
_APPELANTES = ("sample", "test", "publish", "run")


class RecipeInput(BaseModel):
    op: Literal["list", "get", "versions", "create", "propose", "sample", "test",
                "publish", "run"]
    slug: Optional[str] = None
    scope: Optional[Literal["org", "user"]] = None
    org: Optional[int] = None
    version: Optional[int] = Field(default=None, description=(
        "get/test/publish/run: the version (default: the published one, else the latest)."))
    title: Optional[str] = None
    description: Optional[str] = None
    recipe: Optional[dict[str, Any]] = Field(default=None, description=(
        "create/propose: the recipe body. test/run without `slug`: an inline recipe. "
        "Keys: tool, arguments, params, source {items, pagination {type page|cursor|none, "
        "param, start, size, size_param, last, next}}, where [{path, op, value}], "
        "map {column: path | {path, max, default} | {template} | {const}}, values, "
        "key {column, template}, on_existing skip|update, limits {max_units, max_pages}, "
        "units items|calls. Templates: {{params.x}}, {{item.a.b}}, filters |slug "
        "|lower |upper |strip |unaccent."))
    note: Optional[str] = None
    expected_version: Optional[int] = Field(default=None, description=(
        "propose: the latest version you read."))
    params: Optional[dict[str, Any]] = Field(default=None, description=(
        "test/publish/run: the recipe's parameters."))
    datastore: Optional[Any] = Field(default=None, description=(
        "run: the table number to write into (test: optional, checks the key)."))
    resume: Optional[str] = Field(default=None, description=(
        "run: the `resume` token of a previous partial run, to continue it."))
    tool: Optional[str] = Field(default=None, description="sample: the tool to call once.")
    arguments: Optional[dict[str, Any]] = Field(default=None, description=(
        "sample: its arguments (ask for the smallest page: the call is billed)."))
    items: Optional[str] = Field(default=None, description=(
        "sample: path to the list of items in the result (empty = the result itself)."))


class RecipeOut(BaseModel):
    recipe: Optional[dict] = None
    recipes: Optional[list[dict]] = None
    version: Optional[dict] = None
    versions: Optional[list[dict]] = None
    receipt: Optional[dict] = None
    shape: Optional[dict] = None


def _besoin(valeur, code: str, message: str):
    if valeur is None or (isinstance(valeur, str) and not valeur.strip()):
        raise AuthzDenied(400, code, message)
    return valeur


def _proprietaire(ctx: ResolvedCtx, inp: RecipeInput) -> tuple[str, str]:
    if inp.scope == "user":
        return "user", ctx.sub
    if ctx.org_id is None:
        raise AuthzDenied(400, "no_active_org", "No active org: pass `org=<id>`, or "
                                                "`scope='user'` for a recipe of your own.")
    return "org", str(ctx.org_id)


def _corps(ctx: ResolvedCtx, brut: Any) -> dict:
    """Le corps validé, l'outil ramené à son nom canonique (un agent de tenant le lit
    sous son préfixe)."""
    try:
        corps = contrat.valider(brut)
    except contrat.RecetteInvalide as e:
        raise AuthzDenied(400, "invalid_recipe", str(e),
                          details={"problems": e.problemes}) from None
    corps["tool"] = tool_alias.canonical(corps["tool"], tool_alias.prefix_for(ctx.sub))
    return corps


def _fiche(ctx: ResolvedCtx, inp: RecipeInput) -> dict:
    slug = _besoin(inp.slug, "missing_slug", f"`slug` is required for op={inp.op}.")
    owner = _proprietaire(ctx, inp)
    fiche = db_recipes.get_recipe(owner[0], owner[1], slug)
    if fiche is None:
        raise AuthzDenied(404, "unknown_recipe", f"No recipe `{slug}` here. "
                                                 "`oto_recipe(op='list')` shows yours.")
    return fiche


def _version(fiche: dict, numero: Optional[int]) -> dict:
    numero = numero or fiche["published_version"] or fiche["latest_version"]
    version = db_recipes.get_version(fiche["id"], numero) if numero else None
    if version is None:
        raise AuthzDenied(404, "unknown_version", f"`{fiche['slug']}` has no version {numero}.")
    return version


def _gerer(ctx: ResolvedCtx, inp: RecipeInput) -> dict:
    """Les ops de stockage — synchrones, hors boucle."""
    if inp.op == "list":
        if inp.scope == "user":
            owners = [("user", ctx.sub)]
        else:
            owners = [("org", str(ctx.org_id))] if ctx.org_id is not None else []
            owners += [] if inp.scope == "org" else [("user", ctx.sub)]
        return {"recipes": db_recipes.list_recipes(owners)}
    if inp.op == "create":
        slug = _besoin(inp.slug, "missing_slug", "`slug` is required for create.")
        if not contrat.SLUG.match(slug):
            raise AuthzDenied(400, "invalid_slug", "`slug`: lowercase letters, digits and "
                                                   "dashes, 2 to 63 characters.")
        title = _besoin(inp.title, "missing_title", "`title` is required for create.")
        corps = _corps(ctx, _besoin(inp.recipe, "missing_recipe", "`recipe` is required."))
        owner = _proprietaire(ctx, inp)
        try:
            fiche = db_recipes.create_recipe(
                owner_type=owner[0], owner_id=owner[1], slug=slug, title=title.strip(),
                description=inp.description or "", created_by=ctx.sub, body=corps,
                note=inp.note)
        except db_recipes.RecipeExists:
            raise AuthzDenied(409, "recipe_exists", f"Recipe `{slug}` already exists here: "
                                                    "propose a new version.") from None
        return {"recipe": fiche, "version": {"version": 1, "status": "proposee"}}
    fiche = _fiche(ctx, inp)
    if inp.op == "versions":
        return {"recipe": fiche, "versions": db_recipes.list_versions(fiche["id"])}
    if inp.op == "get":
        return {"recipe": fiche, "version": _version(fiche, inp.version)}
    # propose
    attendue = _besoin(inp.expected_version, "missing_expected_version",
                       "`expected_version` is required: the latest version you read "
                       f"(currently {fiche['latest_version']}).")
    corps = _corps(ctx, _besoin(inp.recipe, "missing_recipe", "`recipe` is required."))
    try:
        numero = db_recipes.propose_version(fiche["id"], expected_version=attendue,
                                            body=corps, note=inp.note, proposed_by=ctx.sub)
    except db_recipes.VersionConflict as e:
        raise AuthzDenied(409, "version_conflict", f"The latest version is {e.courante}, "
                                                   f"you read {e.attendue}.") from None
    return {"recipe": db_recipes.get_recipe(fiche["owner_type"], fiche["owner_id"],
                                            fiche["slug"]),
            "version": {"version": numero, "status": "proposee"}}


def _params(corps: dict, fournis: Any) -> dict:
    try:
        return contrat.params_resolus(corps, fournis)
    except contrat.RecetteInvalide as e:
        raise AuthzDenied(400, "invalid_params", str(e),
                          details={"problems": e.problemes}) from None


def _instance():
    fastmcp = tool_registry.bound_instance()
    if fastmcp is None:
        raise AuthzDenied(503, "server_not_ready", "The tool registry is not bound yet.")
    return fastmcp


def _epingler_org(ctx: ResolvedCtx):
    """L'org de la RECETTE pour tout ce que l'exécution fait : la clé du connecteur,
    sa facturation et son journal suivent l'org que l'appel a résolue (garde
    d'appartenance comprise), pas l'org maison de la session. `None` sans org."""
    return session_org.set_call_org(ctx.org_id) if ctx.org_id is not None else None


async def _executer(ctx: ResolvedCtx, inp: RecipeInput, corps: dict, *, ecrire: bool,
                    pages_max: Optional[int] = None) -> dict:
    fastmcp = _instance()
    jeton = _epingler_org(ctx)
    try:
        return await moteur.executer(corps, _params(corps, inp.params), fastmcp=fastmcp,
                                     sub=ctx.sub, datastore=inp.datastore,
                                     reprise=inp.resume, ecrire=ecrire, pages_max=pages_max)
    except moteur.RecetteRefusee as e:
        raise AuthzDenied(400, e.code, str(e)) from None
    finally:
        if jeton is not None:
            session_org.reset_call_org(jeton)


async def _recipe(ctx: ResolvedCtx, inp: RecipeInput) -> dict:
    if inp.op in _APPELANTES and current_token_axes().get("token_kind") == "delegation":
        raise AuthzDenied(403, "hosted_runs_not_supported",
                          "Recipes don't run inside hosted agents yet: the agent's job "
                          "doesn't carry its trigger's tool list to the server.")
    if inp.op not in _APPELANTES:
        return await run_in_threadpool(_gerer, ctx, inp)
    if inp.op == "sample":
        outil = tool_alias.canonical(_besoin(inp.tool, "missing_tool", "`tool` is required."),
                                     tool_alias.prefix_for(ctx.sub))
        fastmcp = _instance()
        jeton = _epingler_org(ctx)
        try:
            forme = await moteur.echantillon(fastmcp, ctx.sub, outil,
                                             inp.arguments or {}, inp.items or "")
        except moteur.RecetteRefusee as e:
            raise AuthzDenied(400, e.code, str(e)) from None
        finally:
            if jeton is not None:
                session_org.reset_call_org(jeton)
        return {"shape": forme}
    if inp.slug is None and inp.op in ("test", "run"):
        corps = _corps(ctx, _besoin(inp.recipe, "missing_recipe",
                                    "Pass `slug` (a stored recipe) or `recipe` (inline)."))
        recu = await _executer(ctx, inp, corps, ecrire=inp.op == "run",
                               pages_max=1 if inp.op == "test" else None)
        return {"receipt": recu}
    fiche = await run_in_threadpool(_fiche, ctx, inp)
    version = await run_in_threadpool(_version, fiche, inp.version)
    if inp.op == "run" and version["status"] != "publiee":
        raise AuthzDenied(409, "not_published",
                          f"Version {version['version']} of `{fiche['slug']}` is "
                          f"{version['status']}: only a published version runs. Publish "
                          "it, or pass the body inline as `recipe`.")
    if inp.op == "run":
        return {"recipe": fiche, "version": {"version": version["version"]},
                "receipt": await _executer(ctx, inp, version["body"], ecrire=True)}
    recu = await _executer(ctx, inp, version["body"], ecrire=False, pages_max=1)
    if inp.op == "test":
        return {"recipe": fiche, "version": {"version": version["version"]}, "receipt": recu}
    # publish : l'épreuve doit avoir produit des lignes, sans refus.
    if recu["stopped"] not in (None, "max_pages") or not recu["rows_built"]:
        raise AuthzDenied(409, "test_failed",
                          "The test page produced no row (or was refused): fix the recipe "
                          "or its params before publishing.", details={"receipt": recu})
    rapport = {k: recu[k] for k in ("items_seen", "rows_built", "skipped_where",
                                    "skipped_no_key", "fill")}
    await run_in_threadpool(db_recipes.decide_version, fiche["id"], version["version"],
                            status="publiee", decided_by=ctx.sub, test_report=rapport)
    return {"recipe": await run_in_threadpool(db_recipes.get_recipe, fiche["owner_type"],
                                              fiche["owner_id"], fiche["slug"]),
            "version": {"version": version["version"], "status": "publiee"}, "receipt": recu}


CAPABILITIES += [
    Capability(
        key="me.recipe", handler=_recipe, Input=RecipeInput, Output=RecipeOut,
        authz=BY_OP({op: _PORTEE for op in ("list", "get", "versions", "create", "propose",
                                            "sample", "test", "publish", "run")}),
        description=(
            "RECIPES: move a connector tool's results into a table on the server, with no "
            "model reading or retyping the rows. A recipe names the tool and its arguments, "
            "how to page through the results, which field goes to which column, and the key "
            "column that identifies a row; existing rows are left untouched by default. "
            "op=sample (tool, arguments, items → the shape of its items: paths, types, fill; "
            "one billed call) · op=test (slug or inline `recipe`, params → one real page, "
            "nothing written, fill rate per column) · op=create (slug, title, recipe → "
            "version 1, PROPOSED) · op=propose (slug, expected_version, recipe) · "
            "op=publish (slug, version, params → test page, then in service) · op=run "
            "(slug or inline `recipe`, params, datastore → counts only; `resume` continues "
            "a partial run) · op=list · op=get · op=versions. `limits.max_units` is "
            "required: a hard cap in the tool's own billed units. Scope: your active org "
            "(default) or `scope='user'`. PROVISIONAL surface."),
        mcp="oto_recipe",
    ),
]
