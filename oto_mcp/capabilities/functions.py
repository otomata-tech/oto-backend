"""`oto_function` — les fonctions : du code pur, stocké et exécuté par Oto (ADR 0073).

Une fonction est un calcul propre à un client — entrée JSON, sortie JSON, avertissements
et fichiers — qui ne lit rien d'Oto, n'appelle aucun réseau et ne voit aucun secret.
Elle remplace le micro-service qu'on montait à côté pour 200 lignes de calcul.

**Proposer et publier sont ouverts au membre, dans sa portée** (0073 §D3, ouvert le
28/10/2026 : le bac à sable tourne en production depuis les recettes-code, sans réseau ni
secret). Un membre de l'org — ou son agent — crée une fonction ou en propose une version ;
**tester, publier et refuser** exécutent ou décident du code non publié : une PERSONNE
les fait, jamais un agent hébergé (`_code_humain`). **Exécuter** (`run`) ne joue que la
version PUBLIÉE, et c'est ouvert à qui lit la fonction.

L'exécution passe par `functions/executor.py` (Pyodide sous Deno, sans réseau), toujours
HORS de la boucle : c'est un sous-processus qu'on attend.
"""
from __future__ import annotations

import base64
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .. import db, media_store, ownership
from ..db import functions as db_functions
from ..functions import contract, executor
from . import _code_humain
from ._authz import BY_OP, ORG_MEMBER_OPT, SUB_ONLY
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

# Le palier suit le SCOPE, comme `oto_procedure` : l'org (défaut) ou soi. Membre de l'org
# suffit pour lire, proposer et exécuter la version publiée.
_PORTEE = BY_OP({None: ORG_MEMBER_OPT("org"), "org": ORG_MEMBER_OPT("org"),
                 "user": SUB_ONLY}, fields=("scope",))

# Un fichier produit est rangé dans le projet : au plus ce poids, par fichier.
_MAX_FICHIER = 20 * 1024 * 1024


class FunctionInput(BaseModel):
    op: Literal["list", "get", "versions", "create", "propose", "run", "test", "publish",
                "refuse"]
    slug: Optional[str] = None
    scope: Optional[Literal["org", "user"]] = None
    org: Optional[int] = None
    # get / test / publish / refuse : la version visée.
    version: Optional[int] = None
    # create
    title: Optional[str] = None
    description: Optional[str] = None
    # create / propose : le contenu d'une version.
    sources: Optional[dict[str, str]] = Field(default=None, description=(
        "Files of this version, {filename: content}: flat `.py` modules (tests are "
        "`test_*.py`) and `.json` data files."))
    entrypoint: Optional[str] = Field(default=None, description=(
        "`module:function`, called with the input and returning "
        "{result, warnings, files}."))
    requirements: Optional[list[str]] = None
    note: Optional[str] = None
    # propose : la dernière version que l'appelant a lue — refus si une autre est passée.
    expected_version: Optional[int] = None
    # run : l'entrée de la fonction, et le projet où ranger les fichiers qu'elle produit.
    input: Optional[dict[str, Any]] = None
    project: Optional[int] = None


class FunctionCard(BaseModel):
    id: int
    owner_type: str
    owner_id: str
    slug: str
    title: str
    description: str = ""
    published_version: Optional[int] = None
    latest_version: Optional[int] = None
    created_by: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class FunctionVersion(BaseModel):
    version: int
    # Domaine fermé par le CHECK de `function_versions.status` (#437).
    status: Literal["proposee", "publiee", "refusee"]
    entrypoint: Optional[str] = None
    requirements: Optional[list[str]] = None
    note: Optional[str] = None
    proposed_by: Optional[str] = None
    proposed_at: Optional[str] = None
    decided_by: Optional[str] = None
    decided_at: Optional[str] = None
    test_report: Optional[dict] = None
    # `get` seulement : le code. La liste et l'historique ne le portent pas.
    sources: Optional[dict[str, str]] = None


class FunctionFile(BaseModel):
    file_id: int
    name: str
    mime: str
    size_bytes: int
    download_url: Optional[str] = None


class FunctionOut(BaseModel):
    function: Optional[FunctionCard] = None
    functions: Optional[list[FunctionCard]] = None
    version: Optional[FunctionVersion] = None
    versions: Optional[list[FunctionVersion]] = None
    # run
    result: Optional[Any] = None
    warnings: Optional[list[str]] = None
    files: Optional[list[FunctionFile]] = None
    # test / publish : le compte rendu, un cas par test.
    tests: Optional[list[dict]] = None
    ok: Optional[bool] = None
    # run / test : ce que le code a imprimé, et la durée de l'exécution.
    log: Optional[str] = None
    duration_ms: Optional[int] = None


def _besoin(valeur, code: str, message: str):
    if valeur is None or (isinstance(valeur, str) and not valeur.strip()):
        raise AuthzDenied(400, code, message)
    return valeur


def _proprietaire(ctx: ResolvedCtx, inp: FunctionInput) -> tuple[str, str]:
    """Le propriétaire visé : soi, ou l'org résolue par la garde."""
    if inp.scope == "user":
        return "user", ctx.sub
    if ctx.org_id is None:
        raise AuthzDenied(400, "no_active_org",
                          "Aucune org active : passe `org=<id>`, ou `scope='user'` "
                          "pour une fonction à toi seul.")
    return "org", str(ctx.org_id)


def _fonction(ctx: ResolvedCtx, inp: FunctionInput) -> dict:
    slug = _besoin(inp.slug, "missing_slug", f"`slug` requis pour op={inp.op}.")
    owner = _proprietaire(ctx, inp)
    fiche = db_functions.get_function(owner[0], owner[1], slug)
    if fiche is None:
        raise AuthzDenied(404, "unknown_function",
                          f"Aucune fonction `{slug}` ici. `oto_function(op='list')` "
                          "montre celles que tu vois.")
    return fiche


def _version(fiche: dict, numero: Optional[int]) -> dict:
    version = db_functions.get_version(fiche["id"], numero) if numero else None
    if version is None:
        raise AuthzDenied(404, "unknown_version",
                          f"`{fiche['slug']}` n'a pas de version {numero}.")
    return version


def _version_valide(inp: FunctionInput) -> tuple[dict, str, list[str]]:
    sources = _besoin(inp.sources, "missing_sources", "`sources` requis.")
    entrypoint = _besoin(inp.entrypoint, "missing_entrypoint", "`entrypoint` requis.")
    requirements = inp.requirements or []
    try:
        contract.valider(sources, entrypoint, requirements)
    except contract.VersionInvalide as e:
        raise AuthzDenied(400, "invalid_version", str(e)) from None
    return sources, entrypoint, requirements


def _executer(version: dict, mode: str, entree: Optional[dict] = None) -> dict:
    """Le bac à sable, et ses deux pannes traduites en refus nommés."""
    try:
        return executor.executer(sources=version["sources"], entrypoint=version["entrypoint"],
                                 requirements=version["requirements"] or [], mode=mode,
                                 entree=entree)
    except executor.BacASableAbsent as e:
        raise AuthzDenied(503, "sandbox_unavailable", str(e)) from None
    except executor.ExecutionEchouee as e:
        raise AuthzDenied(502, "sandbox_failed", str(e)) from None


def _tester(fiche: dict, version: dict) -> dict:
    sortie = _executer(version, "test")
    return {"function": fiche, "version": {"version": version["version"],
                                           "status": version["status"]},
            "ok": sortie["ok"], "tests": sortie["tests"], "log": sortie["journal"],
            "duration_ms": sortie["duree_ms"]}


def _ranger_les_fichiers(ctx: ResolvedCtx, fichiers: list[dict], projet: Optional[int],
                         slug: str) -> list[dict]:
    """Chaque fichier produit devient un fichier du PROJET, rendu avec une URL signée."""
    if not fichiers:
        return []
    if projet is None:
        raise AuthzDenied(400, "missing_project",
                          f"`{slug}` produit {len(fichiers)} fichier(s) : passe "
                          "`project=<id>` pour les ranger dans un projet.")
    if not ownership.can_access(ctx.sub, "project", str(projet), "write"):
        raise AuthzDenied(403, "forbidden", f"Écriture refusée sur le projet #{projet}.")
    ranges = []
    for f in fichiers:
        contenu = base64.b64decode(f["base64"])
        # Le type qu'une fonction DÉCLARE ne fait que départager : le fichier est
        # enregistré et servi sous le type décidé sur son contenu (#562).
        mime = media_store.type_servi(contenu, f["mime"]).content_type
        try:
            cle = media_store.upload_object("project-files", str(projet), contenu, mime,
                                            f["name"], max_bytes=_MAX_FICHIER)
        except media_store.MediaError as e:
            raise AuthzDenied(502, "file_storage_failed", str(e)) from None
        ligne = db.add_project_file(projet, cle, f["name"], mime=mime,
                                    size_bytes=len(contenu), title=f["name"],
                                    description=f"Produit par la fonction `{slug}`.",
                                    created_by=ctx.sub)
        ranges.append({"file_id": ligne["id"], "name": f["name"], "mime": mime,
                       "size_bytes": len(contenu),
                       "download_url": media_store.presign_get(cle)})
    return ranges


def _dispatch(ctx: ResolvedCtx, inp: FunctionInput) -> dict:
    if inp.op == "list":
        if inp.scope == "user":
            owners = [("user", ctx.sub)]
        else:
            owners = ([("org", str(ctx.org_id))] if ctx.org_id is not None else [])
            owners += [] if inp.scope == "org" else [("user", ctx.sub)]
        return {"functions": db_functions.list_functions(owners)}

    if inp.op == "create":
        slug = _besoin(inp.slug, "missing_slug", "`slug` requis pour create.")
        if not contract.SLUG.match(slug):
            raise AuthzDenied(400, "invalid_slug",
                              "`slug` : minuscules, chiffres et tirets, 2 à 63 caractères.")
        title = _besoin(inp.title, "missing_title", "`title` requis pour create.")
        sources, entrypoint, requirements = _version_valide(inp)
        owner = _proprietaire(ctx, inp)
        try:
            fiche = db_functions.create_function(
                owner_type=owner[0], owner_id=owner[1], slug=slug, title=title.strip(),
                description=inp.description or "", created_by=ctx.sub, sources=sources,
                entrypoint=entrypoint, requirements=requirements, note=inp.note)
        except db_functions.FunctionExists:
            raise AuthzDenied(409, "function_exists",
                              f"La fonction `{slug}` existe déjà ici : propose une "
                              "nouvelle version avec op='propose'.") from None
        return {"function": fiche, "version": {"version": 1, "status": "proposee"}}

    fiche = _fonction(ctx, inp)

    if inp.op == "versions":
        return {"function": fiche, "versions": db_functions.list_versions(fiche["id"])}

    if inp.op == "get":
        return {"function": fiche, "version": _version(
            fiche, inp.version or fiche["published_version"] or fiche["latest_version"])}

    if inp.op == "propose":
        attendue = _besoin(inp.expected_version, "missing_expected_version",
                           "`expected_version` requis : la dernière version que tu as lue "
                           f"(aujourd'hui {fiche['latest_version']}).")
        sources, entrypoint, requirements = _version_valide(inp)
        try:
            numero = db_functions.propose_version(
                fiche["id"], expected_version=attendue, sources=sources,
                entrypoint=entrypoint, requirements=requirements, note=inp.note,
                proposed_by=ctx.sub)
        except db_functions.VersionConflict as e:
            raise AuthzDenied(409, "version_conflict",
                              f"La dernière version est la {e.courante}, tu as lu la "
                              f"{e.attendue} : relis-la avant de proposer.") from None
        return {"function": db_functions.get_function(
                    fiche["owner_type"], fiche["owner_id"], fiche["slug"]),
                "version": {"version": numero, "status": "proposee"}}

    if inp.op == "run":
        if fiche["published_version"] is None:
            raise AuthzDenied(409, "not_published",
                              f"`{fiche['slug']}` n'a aucune version publiée : rien ne "
                              "s'exécute avant la publication par la plateforme.")
        numero = inp.version or fiche["published_version"]
        version = _version(fiche, numero)
        if version["status"] != "publiee":
            raise AuthzDenied(409, "not_published",
                              f"La version {numero} de `{fiche['slug']}` n'est pas "
                              "publiée : seule une version publiée s'exécute.")
        sortie = _executer(version, "run", inp.input or {})
        if not sortie.get("ok"):
            raise AuthzDenied(422, "function_error",
                              f"`{fiche['slug']}` v{numero} a levé une erreur.",
                              details={"trace": sortie.get("error"),
                                       "log": sortie.get("journal")})
        return {"function": fiche, "version": {"version": numero, "status": "publiee"},
                "result": sortie["result"], "warnings": sortie["warnings"],
                "files": _ranger_les_fichiers(ctx, sortie["files"], inp.project,
                                              fiche["slug"]),
                "log": sortie["journal"], "duration_ms": sortie["duree_ms"]}

    version = _version(fiche, _besoin(inp.version, "missing_version",
                                      f"`version` requis pour op={inp.op}."))

    if inp.op == "test":
        return _tester(fiche, version)

    if inp.op == "refuse":
        motif = _besoin(inp.note, "missing_note", "`note` requis : le motif du refus.")
        if version["status"] != "proposee":
            raise AuthzDenied(409, "not_proposed",
                              f"La version {version['version']} est {version['status']} : "
                              "seule une version proposée se refuse.")
        db_functions.decide_version(fiche["id"], version["version"], status="refusee",
                                    decided_by=ctx.sub, test_report={"motif": motif})
        return {"function": fiche, "version": {"version": version["version"],
                                               "status": "refusee"}}

    # publish — les tests d'abord, et ils doivent exister.
    if version["status"] == "refusee":
        raise AuthzDenied(409, "refused_version",
                          f"La version {version['version']} a été refusée : propose-en "
                          "une nouvelle.")
    rapport = _tester(fiche, version)
    if not rapport["tests"]:
        raise AuthzDenied(409, "no_tests",
                          f"La version {version['version']} n'a aucun test (`test_*.py`) : "
                          "une fonction ne se publie pas sans preuve.")
    if not rapport["ok"]:
        raise AuthzDenied(409, "tests_failed",
                          f"La version {version['version']} ne passe pas ses tests.",
                          details={"tests": [t for t in rapport["tests"] if not t["ok"]]})
    db_functions.decide_version(fiche["id"], version["version"], status="publiee",
                                decided_by=ctx.sub,
                                test_report={"tests": rapport["tests"],
                                             "duration_ms": rapport["duration_ms"]})
    rapport["function"] = db_functions.get_function(fiche["owner_type"], fiche["owner_id"],
                                                    fiche["slug"])
    rapport["version"] = {"version": version["version"], "status": "publiee"}
    return rapport


#: Les gestes qui exécutent ou décident du code non publié : une personne, jamais un
#: agent hébergé (`_code_humain`).
_OPS_HUMAINES = ("test", "publish", "refuse")


async def _function(ctx: ResolvedCtx, inp: FunctionInput) -> dict:
    if inp.op in _OPS_HUMAINES:
        _code_humain.exiger_une_personne(inp.op)
    return await run_in_threadpool(_dispatch, ctx, inp)


CAPABILITIES += [
    Capability(
        key="me.function", handler=_function, Input=FunctionInput, Output=FunctionOut,
        authz=BY_OP({"list": _PORTEE, "get": _PORTEE, "versions": _PORTEE,
                     "create": _PORTEE, "propose": _PORTEE, "run": _PORTEE,
                     "test": _PORTEE, "publish": _PORTEE, "refuse": _PORTEE}),
        description=(
            "FUNCTIONS: pure code stored and run by Oto for an exact, deterministic "
            "calculation (JSON in → JSON result, warnings and files out). A function reads "
            "nothing from Oto, calls no network and sees no secret: pass it everything it "
            "needs in `input`. op=run (slug, input, project = where to store the files it "
            "produces → result, warnings, files with a signed download_url) runs the "
            "PUBLISHED version only · op=list · op=get (slug, optional version: default the "
            "published one, else the latest) · op=versions (history, without code) · "
            "op=create (slug, title, description, sources, entrypoint, requirements, note "
            "→ version 1, PROPOSED) · op=propose (slug, expected_version = the latest "
            "version you read, sources, entrypoint, requirements, note → next version, "
            "PROPOSED). `sources` is {filename: content} — flat `.py` modules (tests are "
            "`test_*.py`, plain functions named `test_*`) and `.json` data. A proposed "
            "version never runs until it is tested and published "
            "(op=test/publish/refuse, by a person in scope: never from a hosted "
            "agent). Scope: your active org (default) or "
            "`scope='user'`. PROVISIONAL surface."),
        mcp="oto_function",
        rest=RestBinding(verb="POST", path="/api/me/functions"),
        errors=(_code_humain.REFUS,),
    ),
]
