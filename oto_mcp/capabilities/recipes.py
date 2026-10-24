"""`oto_recipe` — les recettes : un outil de connecteur vers un tableau, sans modèle.

Une recette dit quel outil appeler, avec quels arguments, comment parcourir ses
pages, quel champ va dans quelle colonne et quelle colonne identifie une ligne. Un
agent l'écrit une fois — `sample` montre la forme de ce que l'outil rend, `test`
l'éprouve sur une vraie page sans rien écrire — puis le serveur l'exécute (`run`)
autant qu'on veut : l'agent ne relit plus les pages et ne les recopie plus.

**Proposer et publier sont ouverts au membre, dans sa portée** : une recette n'appelle
que des outils DÉCLARÉS EN LECTURE (`tools/lecture`) que son exécutant peut déjà
appeler, et son plafond de dépense (`limits.max_units`) est obligatoire. **Publier
éprouve** : une page réelle, sans écriture, qui doit produire des lignes ; le
remplissage par colonne est gardé avec la version. **Seule une version publiée
écrit** : une recette passée en ligne ne sert qu'à `test`.

**Un agent hébergé est un client comme un autre.** Sous le jeton de son travail, une
recette se juge comme pour une personne : l'identité du porteur, son org (tenue par le
verrou du jeton, `verrou_org.py`) et ses droits sur le tableau ; les refus du contrat
valent pour tous (outils de la plateforme et outils à modèle exclus, outil déclaré en
lecture seulement). Aucune règle propre aux agents.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .. import access, session_org, tool_alias, tool_registry
from ..db import recipes as db_recipes
from ..functions import executor as bac
from ..recipes import affichage, code, contrat, moteur
from ._authz import BY_OP, ORG_MEMBER_OF, ORG_MEMBER_OPT, SUB_ONLY
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

_PORTEE = BY_OP({None: ORG_MEMBER_OPT("org"), "org": ORG_MEMBER_OPT("org"),
                 "user": SUB_ONLY}, fields=("scope",))
_APPELANTES = ("sample", "test", "publish", "run")


class RecipeInput(BaseModel):
    op: Literal["list", "get", "versions", "create", "propose", "describe", "sample",
                "test", "publish", "run"]
    slug: Optional[str] = None
    scope: Optional[Literal["org", "user"]] = None
    org: Optional[int] = None
    version: Optional[int] = Field(default=None, description=(
        "get/test/publish/run: the version (default: the published one, else the latest)."))
    title: Optional[str] = None
    description: Optional[str] = None
    recipe: Optional[dict[str, Any]] = Field(default=None, description=(
        "create/propose: the recipe body. test without `slug`: an inline recipe (never "
        "written: only a published version runs). "
        "Keys: tool, arguments, params, source {items, pagination {type page|cursor|none, "
        "param, start, size, size_param, last, next}}, where [{path, op, value}], "
        "map {column: path | {path, max, default} | {template} | {const}}, values, "
        "key {column, template}, on_existing skip|update, limits {max_units, max_pages}, "
        "units items|calls, for_each {datastore, status_column, filter, max_parents, "
        "max_items_per_row} (each pending row of that table drives the call; cite it as "
        "{{row.col}}). where ops: eq ne in not_in contains_any empty not_empty, and "
        "in_table / not_in_table {table, column} (match against another table); any clause "
        "takes `normalize`. Templates: {{params.x}}, {{item.a.b}}, {{row.col}}, filters "
        "|slug |lower |upper |strip |unaccent |domain |email |email_domain |linkedin_slug "
        "|url |digits. "
        "A CODE recipe instead: {kind: \"code\", sources: {\"recipe.py\": \"async def "
        "run(oto, params): ...\"}, entrypoint: \"recipe:run\", params, writes?, "
        "allow_sending?, limits {max_units, max_calls?, max_seconds?, max_writes?}} — the "
        "code calls `await oto.call(tool, arguments)` (read-only connector tools, "
        "`data_rows`, `data_write(rows, key)` into the run's `datastore` only, and the "
        "CRM writes it declares in `writes`, e.g. \"folk_record:create\" — never in a "
        "test, never delete/merge; a batch via `items`/`ids` (≤50) counts one write per "
        "record against `max_writes`) and returns a receipt of counts; test/publish of "
        "a code recipe are platform-only."))
    display: Optional[dict[str, Any]] = Field(default=None, description=(
        "create/describe: what a process page shows where a process runs this recipe — "
        "{steps: [{label, detail?, credits?, per: contact|company|row}], saves?}: 1 to "
        f"{contrat.MAX_ETAPES} steps in the order the recipe runs them, `credits` what a step bills per `per` "
        "(omit both on a free step), `saves` one MEASURED sentence. On the recipe, not in "
        "a version: changing it republishes nothing. describe with `{}` clears it."))
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


def _affichage(brut: dict) -> Optional[dict]:
    """`display` validé ; `{}` = effacer (None)."""
    if brut == {}:
        return None
    try:
        return contrat.valider_affichage(brut)
    except contrat.RecetteInvalide as e:
        raise AuthzDenied(400, "invalid_display", str(e),
                          details={"problems": e.problemes}) from None


def _proprietaire(ctx: ResolvedCtx, inp: RecipeInput) -> tuple[str, str]:
    if inp.scope == "user":
        return "user", ctx.sub
    if ctx.org_id is None:
        raise AuthzDenied(400, "no_active_org", "No active org: pass `org=<id>`, or "
                                                "`scope='user'` for a recipe of your own.")
    return "org", str(ctx.org_id)


def _corps(ctx: ResolvedCtx, brut: Any) -> dict:
    """Le corps validé, l'outil ramené à son nom canonique (un agent de tenant le lit
    sous son préfixe). Une recette-code (`kind: "code"`) a son propre contrat."""
    try:
        if code.est_code(brut):
            return code.valider(brut)
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


def _numero(fiche: dict, numero: Optional[int] = None) -> Optional[int]:
    """La version demandée, sinon celle en service, sinon la dernière proposée."""
    return numero or fiche["published_version"] or fiche["latest_version"]


def _version(fiche: dict, numero: Optional[int]) -> dict:
    numero = _numero(fiche, numero)
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
        display = _affichage(inp.display) if inp.display is not None else None
        try:
            fiche = db_recipes.create_recipe(
                owner_type=owner[0], owner_id=owner[1], slug=slug, title=title.strip(),
                description=inp.description or "", created_by=ctx.sub, body=corps,
                note=inp.note, display=display)
        except db_recipes.RecipeExists:
            raise AuthzDenied(409, "recipe_exists", f"Recipe `{slug}` already exists here: "
                                                    "propose a new version.") from None
        return {"recipe": fiche, "version": {"version": 1, "status": "proposee"}}
    fiche = _fiche(ctx, inp)
    if inp.op == "versions":
        return {"recipe": fiche, "versions": db_recipes.list_versions(fiche["id"])}
    if inp.op == "get":
        return {"recipe": fiche, "version": _version(fiche, inp.version)}
    if inp.op == "describe":
        if inp.title is None and inp.description is None and inp.display is None:
            raise AuthzDenied(400, "nothing_to_describe",
                              "Pass `title`, `description` and/or `display`.")
        if inp.title is not None and not inp.title.strip():
            raise AuthzDenied(400, "missing_title", "`title` can't be empty.")
        champs: dict = {}
        if inp.display is not None:
            champs["display"] = _affichage(inp.display)
        db_recipes.describe_recipe(
            fiche["id"], title=inp.title.strip() if inp.title else None,
            description=inp.description, **champs)
        return {"recipe": db_recipes.get_recipe(fiche["owner_type"], fiche["owner_id"],
                                                fiche["slug"])}
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


#: Les arrêts qui sont une BORNE (la page d'épreuve s'est arrêtée parce qu'on l'a
#: bornée), pas un refus : avec des lignes bâties, l'épreuve a réussi. Une borne que le
#: moteur ajouterait s'inscrit ici ; un refus (`call_refused`, `mapping_drift`,
#: `repeated_failure`, un tableau indisponible…) n'y entre jamais.
BORNES_D_EPREUVE = frozenset({None, "max_pages", "spend_cap", "time_budget"})


async def _plateforme_pour_le_code(ctx: ResolvedCtx, corps: dict, op: str) -> None:
    """Éprouver ou publier une recette-CODE : la plateforme seule, comme une fonction.
    Le code tourne avec les droits de qui l'exécute, pas de qui l'a écrit (#1202)."""
    if code.est_code(corps) and not await run_in_threadpool(access.is_platform_operator,
                                                            ctx.sub):
        raise AuthzDenied(403, "platform_only",
                          f"op={op} on a code recipe is reserved to the platform: the code "
                          "runs with the rights of whoever runs it. `create` / `propose` "
                          "it, and ask the platform to test and publish it.")


async def _executer(ctx: ResolvedCtx, inp: RecipeInput, corps: dict, *, ecrire: bool,
                    pages_max: Optional[int] = None, temoin: Optional[dict] = None) -> dict:
    fastmcp = _instance()
    if code.est_code(corps) and inp.resume:
        raise AuthzDenied(400, "resume_not_supported",
                          "A code recipe has no resume token: run it again, it starts over "
                          "and its keyed writes do not duplicate.")
    jeton = _epingler_org(ctx)
    try:
        if code.est_code(corps):
            return await code.executer(corps, _params(corps, inp.params), fastmcp=fastmcp,
                                       sub=ctx.sub, datastore=inp.datastore, ecrire=ecrire)
        return await moteur.executer(corps, _params(corps, inp.params), fastmcp=fastmcp,
                                     sub=ctx.sub, datastore=inp.datastore,
                                     reprise=inp.resume, ecrire=ecrire, pages_max=pages_max,
                                     temoin=temoin)
    except moteur.RecetteRefusee as e:
        raise AuthzDenied(400, e.code, str(e)) from None
    except bac.BacASableAbsent:
        raise AuthzDenied(503, "sandbox_unavailable",
                          "This instance has no sandbox: it cannot run a code recipe.") from None
    except bac.ExecutionEchouee as e:
        raise AuthzDenied(502, "sandbox_failed", str(e)[:500]) from None
    finally:
        if jeton is not None:
            session_org.reset_call_org(jeton)


async def _recipe(ctx: ResolvedCtx, inp: RecipeInput) -> dict:
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
    if inp.slug is None and inp.op == "run":
        raise AuthzDenied(400, "inline_recipe_cannot_write",
                          "Only a published recipe writes into a table: `create` it, "
                          "`publish` it (one test page), then `run` it by `slug`. An "
                          "inline `recipe` only serves `test`.")
    if inp.slug is None and inp.op == "test":
        corps = _corps(ctx, _besoin(inp.recipe, "missing_recipe",
                                    "Pass `slug` (a stored recipe) or `recipe` (inline)."))
        await _plateforme_pour_le_code(ctx, corps, "test")
        recu = await _executer(ctx, inp, corps, ecrire=False, pages_max=1)
        return {"receipt": recu}
    fiche = await run_in_threadpool(_fiche, ctx, inp)
    version = await run_in_threadpool(_version, fiche, inp.version)
    if inp.op == "run" and version["status"] != "publiee":
        raise AuthzDenied(409, "not_published",
                          f"Version {version['version']} of `{fiche['slug']}` is "
                          f"{version['status']}: only a published version runs. Publish "
                          "it first.")
    if inp.op == "run":
        return {"recipe": fiche, "version": {"version": version["version"]},
                "receipt": await _executer(ctx, inp, version["body"], ecrire=True,
                                           temoin=version.get("test_report"))}
    await _plateforme_pour_le_code(ctx, version["body"], inp.op)
    recu = await _executer(ctx, inp, version["body"], ecrire=False, pages_max=1)
    if inp.op == "test":
        return {"recipe": fiche, "version": {"version": version["version"]}, "receipt": recu}
    if code.est_code(version["body"]):
        # publish d'une recette-code : l'épreuve doit avoir tourné sans erreur. Une borne
        # atteinte n'est pas un refus ; une erreur du code, si.
        if not recu["ok"]:
            raise AuthzDenied(409, "test_failed",
                              f"The test run failed: {str(recu.get('error'))[:500]}",
                              details={"receipt": recu})
        rapport = {k: recu[k] for k in ("calls", "units", "stopped")}
        await run_in_threadpool(db_recipes.decide_version, fiche["id"], version["version"],
                                status="publiee", decided_by=ctx.sub, test_report=rapport)
        return {"recipe": await run_in_threadpool(db_recipes.get_recipe, fiche["owner_type"],
                                                  fiche["owner_id"], fiche["slug"]),
                "version": {"version": version["version"], "status": "publiee"},
                "receipt": recu}
    # publish : l'épreuve doit avoir produit des lignes, sans refus. Une BORNE atteinte
    # (pages, dépense, horloge) n'est pas un refus : une recette à petit `max_units`
    # s'arrête sur `spend_cap` au milieu de sa première page, lignes bâties.
    if recu["stopped"] not in BORNES_D_EPREUVE or not recu["rows_built"]:
        raise AuthzDenied(409, "test_failed",
                          f"The test page built {recu['rows_built']} row(s) and stopped on "
                          f"`{recu['stopped'] or 'end of results'}`: publishing needs at "
                          "least one row and no refusal. Fix the recipe or its params "
                          "before publishing.", details={"receipt": recu})
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
                                            "describe", "sample", "test", "publish",
                                            "run")}),
        description=(
            "RECIPES: move a connector tool's results into a table on the server, with no "
            "model reading or retyping the rows. Only connector tools DECLARED READ-ONLY can "
            "be called (never one that sends, posts or writes at the provider; never a "
            "model connector). A recipe names the tool and its arguments, "
            "how to page through the results, which field goes to which column, and the key "
            "column that identifies a row; existing rows are left untouched by default. "
            "op=sample (tool, arguments, items → the shape of its items: paths, types, fill; "
            "one billed call) · op=test (slug or inline `recipe`, params → one real page, "
            "nothing written, fill rate per column) · op=create (slug, title, recipe → "
            "version 1, PROPOSED) · op=propose (slug, expected_version, recipe) · "
            "op=describe (slug, title?, description?, display → what a process page shows "
            "where a process runs the recipe: its steps and what each bills; on the "
            "recipe, not a version, so nothing is republished; name the recipe in the "
            "process flowchart's margin as `oto_recipe <slug>`) · "
            "op=publish (slug, version, params → test page, then in service) · op=run "
            "(slug of a PUBLISHED recipe, params, datastore → counts only; `max_pages` "
            "bounds each call, `resume` continues a partial run; a table without a declared "
            "key gets the recipe's key on the first run) · op=list · op=get · op=versions. "
            "`for_each` fans out: each row of a parent table whose `status_column` is empty "
            "drives the call (people per company…), then gets `done`, `empty` or "
            "`failed:<code>`; a parent whose cited input is empty is not selected (never "
            "called nor marked) and is counted in `skipped_empty_input` when the run ends; "
            "the parent table must be writable. A column the published "
            "test filled that comes back empty on a whole page is `mapping_drift`: the run "
            "stops (or, under `for_each`, that parent is marked and the next one runs). "
            "op=test under `for_each` bills up to 3 pages. A fresh run (no `resume`) starts "
            "its spend at 0: only the org's quotas bound the total across runs. "
            "`limits.max_units` is required: a hard cap on what the tool BILLS (its own "
            "units; `units_basis` in the receipt says when it falls back on the recipe's "
            "count). op=run with a `datastore` WRITES rows into that table, with the "
            "caller's own rights on it. Scope: your active org (default) or "
            "`scope='user'`. A hosted agent runs recipes like any caller: its identity and "
            "its job's org. A recipe can also be CODE (`kind: \"code\"`, Python in the "
            "function sandbox calling tools through `oto.call`): anyone in scope creates "
            "and runs it, only the platform tests and publishes it. A code recipe may also "
            "create or update CRM records through a closed list of tools, only the writes "
            "it declares (`writes`), at most `limits.max_writes` per run (default 50), in "
            "the run's org, with the caller's own connector account. PROVISIONAL surface."),
        mcp="oto_recipe",
        # La même capacité sur la face REST, pour qu'une page lance une recette en un
        # geste (un tableau, une ligne) sans session MCP : mêmes ops, même garde par op,
        # même reçu. Le corps est l'`Input` entier (`{op, slug, params, datastore…}`),
        # comme `POST /api/me/runner/jobs`. Elle rend aussi `create`/`propose`/`publish`
        # joignables en REST — sous la garde qu'ils ont déjà en MCP, rien de plus.
        # Provisoire comme la capacité ; déclarée lourde (`routes_lourdes`) : un `run`
        # appelle un outil page après page.
        rest=RestBinding(verb="POST", path="/api/me/recipes", provisoire=True),
    ),
]


# ── Lecture REST : les recettes d'une org, pour qu'une page les nomme ────────────
#
# La face MCP (`oto_recipe op=list`) sert l'agent ; une page de processus a besoin
# de savoir, sans session MCP, quelles recettes l'org possède et si chacune est en
# service. Lecture seule, fiches sans corps (le corps d'une recette-code est du
# code), membre de l'org. Provisoire : la forme suivra ce que la page affiche.


class OrgRecipesInput(BaseModel):
    org_id: int


class RecipeStepDisplay(BaseModel):
    label: str
    detail: str = ""
    credits: Optional[float] = None
    per: Optional[Literal["contact", "company", "row"]] = None


class RecipeDisplay(BaseModel):
    steps: list[RecipeStepDisplay]
    saves: Optional[str] = None
    source: Literal["described", "derived"] = Field(description=(
        "`described`: written on the recipe (`op=describe`), credits included — an "
        "ESTIMATE, not measured. `derived`: read from the body of the version in "
        "service, else the latest proposed (a declarative recipe), so it follows its "
        "body; no credits."))


class OrgRecipe(BaseModel):
    slug: str
    title: str
    description: str
    status: Literal["published", "proposed"] = Field(description=(
        "`published` when a version is in service (`published_version`), else "
        "`proposed`: only a published version runs."))
    published_version: Optional[int] = None
    latest_version: Optional[int] = None
    updated_at: Optional[str] = Field(default=None, description="ISO 8601.")
    display: Optional[RecipeDisplay] = Field(default=None, description=(
        "What a process page draws where it runs this recipe: the steps written by "
        "`oto_recipe op=describe`, else those read from a declarative recipe's body; "
        "null for a code recipe with none written."))


class OrgRecipes(BaseModel):
    recipes: list[OrgRecipe]


def _affichage_de(fiche: dict, corps: Any) -> Optional[dict]:
    """Ce que la page dessine : l'affichage ÉCRIT s'il y en a un, sinon les étapes LUES
    dans `corps`, celui de la version `_numero` (en service, sinon la dernière proposée)
    d'une recette déclarative. Le corps ne sort jamais d'ici."""
    if fiche.get("display"):
        return {**fiche["display"], "source": "described"}
    lues = affichage.etapes_lues(corps)
    return {**lues, "source": "derived"} if lues else None


def _lister_org(org_id: int) -> list[dict]:
    """Les fiches de l'org et leur affichage : deux requêtes quel que soit leur nombre
    (les fiches, puis les corps à lire, tous ensemble)."""
    fiches = db_recipes.list_recipes([("org", str(org_id))])
    a_lire = {f["id"]: _numero(f) for f in fiches if not f.get("display") and _numero(f)}
    corps = db_recipes.get_bodies(a_lire)
    return [{**f, "display": _affichage_de(f, corps.get(f["id"]))} for f in fiches]


async def _org_recipes(ctx: ResolvedCtx, inp: OrgRecipesInput) -> dict:
    fiches = await run_in_threadpool(_lister_org, inp.org_id)
    return {"recipes": [{
        "slug": f["slug"], "title": f["title"], "description": f["description"] or "",
        "status": "published" if f["published_version"] else "proposed",
        "published_version": f["published_version"], "latest_version": f["latest_version"],
        "updated_at": f["updated_at"].isoformat() if f.get("updated_at") is not None
        else None,
        "display": f.get("display"),
    } for f in fiches]}


CAPABILITIES += [
    Capability(
        key="org.recipes", handler=_org_recipes, Input=OrgRecipesInput, Output=OrgRecipes,
        authz=ORG_MEMBER_OF("org_id"), mcp=None,
        rest=RestBinding("GET", "/api/orgs/{id}/recipes", {"id": "org_id"}, provisoire=True),
        description=("The org's recipes (no bodies): slug, title, whether one is in service, "
                     "and the steps a process page draws where it runs one."),
    ),
]
