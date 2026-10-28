"""Capacité `search` (lot 3, Ship 1) — le verbe « retrouver », sur deux faces.

MCP `oto_search` + REST `GET /api/me/search` : un seul chemin de code
(`oto_mcp/search.py`), le dashboard (popup ⌘K / page /search) consomme la face
REST. Erreurs (plan Ship 1 §3) : pas d'org active → 400 `no_active_org`
(invocation DÉLIBÉRÉE — ≠ un îlot d'accueil chargé d'office, qui rend 200 et vide) ;
`scope='project'` sans `project` → 400 ; projet hors contexte → refus neutre.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, field_validator
from starlette.concurrency import run_in_threadpool

from .. import ownership, search as search_mod
from ._authz import SUB_ONLY
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES


class SearchHit(BaseModel):
    """Un résultat, quelle que soit sa source.

    ⚠️ **`ref` est polymorphe** — c'est la forme réelle, pas une approximation : un
    identifiant simple pour la plupart des sources (slug d'une procédure, id d'une
    page), mais un objet `{scope, slug}` pour un guide, dont le slug ne suffit pas à
    désigner (le même slug peut exister en plateforme, en org et en perso).
    `matched_by` dit par quel chemin le hit est remonté — un même document trouvé
    par les deux voies est dédupliqué, ses rangs sommés (RRF)."""
    kind: str                                    # page | brief | procedure | guide | tableau | ligne | fichier | connecteur | app
    ref: object
    title: str
    description: Optional[str] = None
    passage: Optional[str] = None                # extrait surligné, absent si non pertinent
    updated_at: Optional[str] = None
    matched_by: Optional[str] = None             # lexical | semantic
    project_id: Optional[int] = None
    project_name: Optional[str] = None
    # L'ORG où vit l'objet (ADR 0071) — pas le chemin par lequel on l'atteint : un
    # tableau personnel reste à son org de naissance, le projet d'un collègue partagé
    # reste à l'org de ce collègue. Toujours présents (jamais omis), `null` compris :
    # - `origin_org_id` : l'org, ou `null` (inconnue, ou aucune org : plateforme,
    #   catalogue de connecteurs — `other_org` départage) ;
    # - `other_org` : `true` = vit dans une AUTRE org que l'org active (un partage :
    #   ne jamais le présenter comme appartenant à l'org active) ; `false` = vit dans
    #   l'org active, ou dans aucune org ; `null` = on ne sait pas (tableau/ligne ou
    #   note personnels : leur org de naissance n'est pas enregistrée).
    origin_org_id: Optional[int] = None
    other_org: Optional[bool] = None


class SearchResults(BaseModel):
    hits: list[SearchHit]
    count: int
    # DÉRIVÉ des hits, jamais codé en dur : semantic si tous le sont, mixed si les
    # deux familles cohabitent, lexical sinon.
    matched_by: str
    # Présent UNIQUEMENT quand il n'y a aucun résultat — un conseil de reformulation,
    # pas un champ d'erreur.
    hint: Optional[str] = None
    # Présents UNIQUEMENT quand la coupe a mordu (oto#42) : `count` dit ce que la
    # réponse CONTIENT, `total` ce que la recherche a TROUVÉ. Sans le second, « 20
    # résultats » se lit « voilà tout ce qui existe » — et l'agent conclut à une
    # absence qui n'en est pas une. Rien n'est ajouté quand tout est rendu.
    total: Optional[int] = None
    truncated: Optional[bool] = None
    hint_truncated: Optional[str] = None


class SearchInput(BaseModel):
    q: str
    scope: Literal["org", "project"] = "org"
    project: Optional[int] = None
    kinds: Optional[list[str]] = None      # sous-ensemble de search_mod.KINDS
    limit: int = 20

    @field_validator("kinds", mode="before")
    @classmethod
    def _csv(cls, v):
        # Face REST (GET) : `?kinds=page,tableau` arrive en string CSV.
        if isinstance(v, str):
            v = [k.strip() for k in v.split(",") if k.strip()]
        return v or None

    @field_validator("kinds")
    @classmethod
    def _known(cls, v):
        if v:
            bad = set(v) - set(search_mod.KINDS)
            if bad:
                raise ValueError(f"kinds inconnus : {sorted(bad)} (valides : {list(search_mod.KINDS)})")
        return v

    @field_validator("limit")
    @classmethod
    def _cap(cls, v):
        return max(1, min(int(v), 50))


def _preparer(ctx: ResolvedCtx, inp: SearchInput) -> tuple[list[dict], list[dict]]:
    """Le SQL d'avant l'embedding, SYNC (appelé au threadpool) : le projet demandé
    est-il visible, puis le catalogue de connecteurs visible et les apps que la liste
    des apps de l'org rend. Rend `(catalogue, apps)`."""
    if inp.scope == "project":
        # Refus NEUTRE (pas de distinction inexistant/inaccessible) — plan Ship 1 §3.
        if not ownership.visible_in_org(ctx.sub, ctx.org_id, "project", str(inp.project)):
            raise AuthzDenied(404, "unknown_project", f"Projet #{inp.project} inconnu.")

    # Source connecteurs : le catalogue VISIBLE (activation × RBAC), injecté ici —
    # le module search reste sous la couche capacité. Best-effort (source optionnelle).
    catalog: list[dict] = []
    try:
        from .connectors import selection as connectors_selection
        catalog = connectors_selection._visible_catalog(ctx)
    # noqa: SILENT — source de recherche optionnelle (catalogue connecteurs)
    except Exception:  # noqa: BLE001
        pass
    # Source apps : EXACTEMENT ce que la liste des apps de l'org rend
    # (`apps._org_list` — « cherchable ⇔ lisible »). Pas d'app dans un scope projet :
    # une app n'appartient à aucun projet, elle y est seulement rangée.
    apps: list[dict] = []
    if inp.scope == "org" and (not inp.kinds or "app" in inp.kinds):
        from .apps import _org_list
        apps, _ = _org_list(ctx, False)
    return catalog, apps


async def _search(ctx: ResolvedCtx, inp: SearchInput) -> dict:
    # Handler ASYNC pour l'embedding de la requête (httpx async, awaité) ; TOUT le SQL
    # part au threadpool. La FTS n'est pas « indexée, courte » partout : `search_docs_fts`
    # tenait la boucle mono-loop en prod, gels ≥ 1 s (cf. `docs/event-loop-perf.md`).
    if ctx.org_id is None:
        raise AuthzDenied(400, "no_active_org",
                          "Aucune org active — passe `_org=<id>` ou `oto_use_org`.")
    q = (inp.q or "").strip()
    if len(q) < 2:
        raise AuthzDenied(400, "query_too_short", "Requête trop courte (≥ 2 caractères).")
    if inp.scope == "project" and inp.project is None:
        raise AuthzDenied(400, "project_required",
                          "`scope='project'` exige `project=<id>`.")
    catalog, apps = await run_in_threadpool(_preparer, ctx, inp)

    # Vecteur de requête (fusion sémantique) — None si désactivé/échec → lexical seul.
    # Calculé dès qu'UNE source à sémantique est demandée : page/brief/guide + ligne (#67 V2.2).
    _semantic_kinds = {"page", "brief", "guide", "ligne"}
    want_semantic = not inp.kinds or bool(_semantic_kinds & set(inp.kinds))
    from .. import embeddings
    qvec = await embeddings.embed_query(q) if want_semantic else None

    return await run_in_threadpool(
        search_mod.search,
        ctx.sub, ctx.org_id, q, scope=inp.scope, project_id=inp.project,
        kinds=inp.kinds, limit=inp.limit, connectors_catalog=catalog,
        query_embedding=qvec, apps_catalog=apps)


CAPABILITIES += [
    Capability(
        key="me.search",
        handler=_search,
        Input=SearchInput,
        Output=SearchResults,
        authz=SUB_ONLY,
        description=(
            "SEARCH across everything readable in the active org — one query, ranked "
            "together: project pages & briefs (passages with highlighted fragment), "
            "procedures, guides, containers by name (tableaux, files, connectors), AND "
            "datastore ROWS by content (kind=ligne — find the row where a value appears, "
            "not just the table's name), and apps by title (kind=app, the apps the org's "
            "app list shows you). "
            "Lexical (French stemming, accent-insensitive); reformulate with the exact "
            "words if 0 hits. `scope='project'`+`project=<id>` narrows to one project. "
            "`count` is what this response holds; when the cut bit, `total` says how "
            "many were FOUND and `truncated` is true — 20 hits out of 300 must not "
            "read as « that is all there is ». "
            "`kinds` filters (page|brief|procedure|guide|tableau|ligne|fichier|connecteur|app). "
            "Every hit carries WHERE the object lives, not how you reach it: "
            "`origin_org_id` (the org it lives in) and `other_org` — true = it lives in "
            "ANOTHER org than the active one (a share: never present it as the active "
            "org's own); false = it lives in the active org, or in no org (platform "
            "guide, connector); null = UNKNOWN (a personal table/row or personal note "
            "does not record its org yet) — say so, do not guess. "
            "SEARCH when you know what you're looking for; NAVIGATE (oto_project op=get "
            "include=['spine']) when the question is structural. Then open the hit: "
            "oto_doc op=get (page), data_rows (tableau/ligne), oto_procedure op=get, "
            "oto_apps op=get app_id=<ref> (app)."),
        mcp="oto_search",
        rest=RestBinding("GET", "/api/me/search"),
    ),
]
