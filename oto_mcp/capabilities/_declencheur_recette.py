"""Un agent qui joue une RECETTE, sans modèle (27/11/2026).

Un déclencheur (`runner_triggers`) joue d'ordinaire une procédure avec un modèle. Il
peut aussi jouer une recette publiée de son org : même horloge (`kind=schedule`), même
porte (`kind=webhook`), même `run_now`, même file, mêmes livraisons — mais aucun
modèle, aucune clé de modèle, aucun abonnement. Le travail porte `payload.recipe` et la
famille `recette` (`runner_models.FAMILLE_RECETTE`) : seul un worker qui réserve avec
`provider="recette"` le voit (la clause de famille de `claim_next_job`), et il ne fait
qu'appeler `POST /api/me/recipes op=run` avec le jeton délégué du travail.

⚠️ **La version est ÉPINGLÉE** : le run tourne sous l'identité du propriétaire de
l'agent, et tout membre de l'org peut publier une nouvelle version d'une recette-code.
Suivre « la dernière publiée » ferait exécuter le code d'un autre avec les droits du
propriétaire. La version se pose à la création (celle en service) et ne change que par
une retouche explicite (`recipe.version`).

⚠️ **Base partagée avec l'ancien code** : la ligne garde `procedure = ''`, `tools = []`
et `model = NULL`. L'ancien tick et l'ancien webhook la lisent « sans modèle » et
sautent l'occurrence (`refused_no_model`) — jamais un run de modèle. La lecture sert
`procedure: null` (`NULLIF` dans `_COLS`).
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from .. import runner_models
from ..db import recipes as db_recipes
from ..recipes import code as code_recette
from ..recipes import contrat, outils_cites
from ._types import AuthzDenied

#: Le genre de travail que joue un agent-recette.
FAMILLE = runner_models.FAMILLE_RECETTE

#: Les champs d'un agent à modèle, refusés sur un agent-recette (nommés dans le refus).
CHAMPS_D_AGENT = ("procedure", "model", "input", "tools", "max_steps", "max_tokens",
                  "max_run_seconds", "platform_tools", "payload_mode", "payload_fields",
                  "effort", "temperature")


class RecipeSpec(BaseModel):
    """`recipe` d'`oto_trigger` : la recette publiée que l'agent joue."""
    slug: str = Field(description="An org recipe slug (the trigger's org), published.")
    version: Optional[int] = Field(default=None, description=(
        "The PINNED version. create: omitted → the version in service now. update: a "
        "number re-pins; omitted → unchanged. A newer published version never runs "
        "until re-pinned."))
    params: Optional[dict[str, Any]] = Field(default=None, description=(
        "The recipe's params (required ones must be given unless they have a default)."))
    datastore: Optional[int] = Field(default=None, description=(
        "The table number the run writes into, writable by the agent's owner. Required "
        "for a declarative recipe."))


def spec(brut: Any) -> dict:
    """`recipe` tel que l'entrée le porte (un objet nu : les schémas d'outil MCP sont
    PLATS, sans `$defs` — `test_with_signature_flat`), validé par `RecipeSpec`."""
    from pydantic import ValidationError
    try:
        return RecipeSpec.model_validate(brut).model_dump()
    except ValidationError as e:
        raise AuthzDenied(400, "invalid_recipe_spec",
                          "`recipe` = {slug, version?, params?, datastore?} : "
                          + "; ".join(f"{'.'.join(str(x) for x in err['loc']) or 'recipe'}: {err['msg']}"
                                      for err in e.errors())) from None


def exiger_un_worker(org_id: int) -> None:
    """Refuse de promettre une exécution qu'aucun worker `recette` vivant n'assure —
    la garde `no_runner_armed` / `exige_servi` des agents, dite pour une recette."""
    from . import _modele
    etat = _modele.exige_un_runner(org_id)
    if FAMILLE not in (etat.get("families") or []):
        raise AuthzDenied(
            409, "recipe_worker_unavailable",
            "aucun worker n'exécute les agents-recettes pour cette org en ce moment : "
            "l'exécution resterait en attente, puis périmerait. Reprends le geste quand "
            "l'exploitant aura démarré le worker `recette` d'oto-runner.")


def est_recette(t: Optional[dict]) -> bool:
    return bool((t or {}).get("recipe"))


def champs_refuses(inp: Any) -> list[str]:
    """Les champs d'agent à modèle passés dans `inp` (valeur non nulle)."""
    return [c for c in CHAMPS_D_AGENT if getattr(inp, c, None) not in (None, [], "")]


def _fiche_et_version(org_id: int, slug: str, version: Optional[int]) -> tuple[dict, dict]:
    fiche = db_recipes.get_recipe("org", str(int(org_id)), slug)
    if fiche is None:
        raise AuthzDenied(404, "unknown_recipe",
                          f"No recipe `{slug}` in this org. An agent plays a recipe of "
                          "its own org (`oto_recipe op=list scope=org`).")
    if not fiche.get("published_version"):
        raise AuthzDenied(409, "recipe_not_published",
                          f"`{slug}` has no published version: only a published version "
                          "runs. Publish it first (`oto_recipe op=publish`).")
    numero = int(version or fiche["published_version"])
    v = db_recipes.get_version(fiche["id"], numero)
    if v is None or v.get("status") != "publiee":
        raise AuthzDenied(409, "recipe_version_unknown",
                          f"`{slug}` has no published version {numero}.")
    return fiche, v


def _params_valides(corps: dict, params: Optional[dict]) -> None:
    try:
        contrat.params_resolus(corps, params or {})
    except contrat.RecetteInvalide as e:
        manquants = [p.split("`")[1] for p in e.problemes
                     if p.startswith("missing required param")]
        raise AuthzDenied(400, "recipe_missing_params" if manquants else "invalid_params",
                          str(e), details={"problems": e.problemes,
                                           **({"missing": manquants} if manquants else {})}
                          ) from None


def _tableau_ecrivable(datastore: Any) -> None:
    """Le tableau, écrivable par l'APPELANT (qui pose l'agent) — la même résolution que
    l'exécution (`recipes/ecriture._ouvrir_tableau`). Chaque refus nommé."""
    from ..recipes import ecriture
    try:
        ecriture._ouvrir_tableau(str(int(datastore)), ecrire=True)
    except ecriture.TableauIndisponible as e:
        raise AuthzDenied(400 if e.code != "datastore_not_found" else 404,
                          e.code, str(e)) from None


def valider(org_id: int, spec: dict, actuel: Optional[dict] = None, *,
            verifier_tableau: bool = True) -> dict:
    """La recette à stocker sur l'agent : `{slug, version, params, datastore}`, la
    version ÉPINGLÉE. `actuel` = la recette déjà posée (update) : ce qui n'est pas passé
    est gardé, la version comprise (même slug)."""
    base = dict(actuel or {})
    slug = (spec.get("slug") or base.get("slug") or "").strip()
    if not slug:
        raise AuthzDenied(400, "missing_fields", "`recipe.slug` is required.")
    meme = slug == base.get("slug")
    version = spec.get("version") or (base.get("version") if meme else None)
    params = spec["params"] if spec.get("params") is not None else (
        base.get("params") if meme else None)
    datastore = spec["datastore"] if spec.get("datastore") is not None else (
        base.get("datastore") if meme else None)
    _, v = _fiche_et_version(org_id, slug, version)
    corps = v.get("body") or {}
    _params_valides(corps, params)
    if datastore is None and not code_recette.est_code(corps):
        raise AuthzDenied(400, "missing_fields",
                          "`recipe.datastore` is required: a declarative recipe writes "
                          "its rows into a table.")
    if datastore is not None and verifier_tableau:
        _tableau_ecrivable(datastore)
    return {"slug": slug, "version": int(v["version"]),
            "params": params or None, "datastore": int(datastore) if datastore else None}


def motif_de_refus(t: dict) -> Optional[tuple[str, str]]:
    """Revérifié À L'ENFILAGE (tick, webhook, run_now) : la recette existe, la version
    épinglée est publiée, les params tiennent. `(code, message)` ou None. Le tableau se
    juge à l'exécution (l'identité de la requête n'existe pas dans le tick)."""
    r = t.get("recipe") or {}
    try:
        _, v = _fiche_et_version(int(t["org_id"]), r.get("slug") or "", r.get("version"))
        _params_valides(v.get("body") or {}, r.get("params"))
    except AuthzDenied as e:
        return e.code, e.message
    return None


def outils(org_id: int, r: dict) -> list[str]:
    """La liste d'outils du travail : `oto_recipe` et ce que cite la version épinglée —
    la garde du serveur (`outils_du_travail`) n'en laisse passer aucun autre."""
    fiche = db_recipes.get_recipe("org", str(int(org_id)), r.get("slug") or "")
    v = db_recipes.get_version(fiche["id"], int(r["version"])) if fiche else None
    cites = outils_cites.outils_cites((v or {}).get("body"))
    return list(dict.fromkeys(["oto_recipe", *cites]))


def charge(t: dict, origine: str, *, project_id: Optional[int] = None) -> dict:
    """La charge du travail `start` d'un agent-recette — UNE définition pour l'horloge,
    le webhook et `run_now`. Aucune instruction, aucun modèle."""
    r = t["recipe"]
    return {
        "recipe": {"slug": r["slug"], "version": int(r["version"]), "scope": "org",
                   "params": r.get("params") or {},
                   **({"datastore": int(r["datastore"])} if r.get("datastore") else {})},
        "model_family": FAMILLE,
        "tools": outils(int(t["org_id"]), r),
        "label": t.get("label") or f"{origine} — recette {r['slug']}",
        "trigger_id": t["id"],
        "project_id": project_id if project_id is not None else t.get("project_id"),
    }


def lecture(org_id: int, ts: list[dict]) -> list[dict]:
    """Les agents servis, augmentés pour un agent-recette de `recipe_title` et
    `recipe_latest_version` (la version publiée aujourd'hui : « v3 épinglée, v4
    disponible »). Une lecture par recette distincte."""
    memo: dict[str, Optional[dict]] = {}
    out = []
    for t in ts:
        r = t.get("recipe")
        if not r:
            out.append(t)
            continue
        slug = r.get("slug") or ""
        if slug not in memo:
            memo[slug] = db_recipes.get_recipe("org", str(int(org_id)), slug)
        f = memo[slug]
        out.append({**t, "recipe_title": (f or {}).get("title"),
                    "recipe_latest_version": (f or {}).get("published_version")})
    return out
