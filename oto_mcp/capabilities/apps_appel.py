"""Ce qu'une app OUVERTE demande — `POST /api/me/apps/{app_id}/call`, face REST seule.

Une app s'exécute chez son lecteur, dans un cadre isolé sans réseau : tout ce qu'elle
lit, écrit ou lance passe par le front, qui le relaie ICI, au nom du lecteur. C'est
la seule porte : le front n'appelle plus les tableaux ni les agents pour une app.

**Ce qui est servi** : lire (`read_rows`, `read_aggregate`), écrire une valeur
déclarée (`write_cell`), ajouter une ligne (`add_row`), lancer un agent
(`start_agent`), suivre ses runs (`agent_runs`, `run_result`).

**Un geste qui écrit ou lance exige le CONSENTEMENT du lecteur** (oto-backend#1386).
Le serveur ne distingue pas un clic du lecteur d'un appel que l'app fait seule à son
ouverture : c'est le cadre PARENT, hors de l'iframe, qui le sait. Il demande un jeton
(`POST …/consent`, même corps que l'appel) quand le lecteur vient d'agir dans l'app —
l'activation utilisateur du navigateur (`navigator.userActivation`), qu'un clic dans
l'iframe transmet à la fenêtre parente et que l'app ne peut pas fabriquer : le clic
EST l'accord (décidé avec Julien le 28/10/2026) — et sinon après avoir demandé
confirmation au lecteur. Une rafale de gestes sur un seul clic est bornée côté front. Le jeton vaut
pour CE geste — app, lecteur, action, empreinte des arguments calculée ici aux deux
bouts — une fois, deux minutes. L'appel le brûle avant d'agir ; brûlé, il reste comme
journal des gestes d'app (`app_consents` : qui, quelle app, quoi, le `geste_id` des
révisions écrites, le travail lancé). L'iframe n'a pas de réseau : elle ne peut ni
demander un jeton, ni en voir un.

**Les runs** : le statut et les dates des runs d'un agent DÉCLARÉ, que l'auteur et le
lecteur voient tous deux (les mêmes que l'écran de l'agent montre au lecteur) ; ce
que l'agent a conclu (`outcome`, `note`) et `run_result` seulement pour les runs que le
LECTEUR a lancés.

**La règle : droits d'une app = droits de son AUTEUR ∩ droits de son LECTEUR.**
L'auteur (`apps.updated_by`, le dernier à avoir écrit l'app) choisit ce qu'elle
touche ; le lecteur l'ouvre avec ses propres droits. Sans la règle, quelqu'un qui peut
éditer l'app sans lire un tableau le déclarerait et le ferait lire par la session d'un
collègue. Chaque appel juge donc, sur le tableau DÉCLARÉ au manifeste :
- **l'org de l'app** (`org_de_l_app`, celle de son propriétaire) : le tableau doit y vivre (`apps_manifeste.
  tableau_de_l_org`) — ni l'org active de l'auteur, ni celle du lecteur ;
- **l'auteur**, par l'ownership seul (`ownership.can_access` sur le NUMÉRO du
  tableau) — jamais par sa session : il n'est pas là, et une résolution par nom se
  ferait dans SON org active (ADR 0023) ;
- **le lecteur**, en exécutant pour lui les capacités existantes (`_list_rows`,
  `_aggregate`, `_update_row`, `_append_row`, `run_now`) : leurs règles, pas une copie.
Un agent se juge pareil, dans l'org de l'app : l'auteur par `_acces_agent.niveau`
(écrire pour lancer), le lecteur par `run_now` lui-même.
Auteur inconnu ou qui a perdu l'accès : refus nommé, rien n'est servi.

**Ce que l'app voit est borné par son manifeste**, ici et non plus dans le front
seul : champs déclarés (projection, filtres, tri, regroupement, mesures). Un manifeste
stocké est déjà normalisé (`apps_manifeste.forme`) et ses tableaux sont des numéros.

**Session interactive seule** (`allow_api_token=False`) : aucun jeton `oto_`,
délégation d'agent hébergé comprise — un agent n'est jamais le lecteur d'une app.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from .. import db, geste, ownership
from . import _acces_agent
from ._authz import ORG_MEMBER
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .apps_manifeste import org_de_l_app, tableau_de_l_org
from .registry import CAPABILITIES

APP_RTYPE = "app"
MAX_ROWS = 200
# Ce qu'une ligne porte toujours, en plus des champs déclarés : de quoi la désigner
# et la dater.
_META = frozenset({"_id", "_revision", "_created_at", "_updated_at"})
_DATES = frozenset({"_created_at", "_updated_at"})
_AGG_OPS = frozenset({"count_rows", "count", "sum", "avg", "min", "max"})
MAX_INPUT = 300
MAX_NOTE = 4000
MAX_RUNS = 50
_JOB_DONE = frozenset({"done", "failed", "expired"})
# Ce qui écrit ou lance : servi seulement avec un consentement du lecteur.
GESTES = frozenset({"write_cell", "add_row", "start_agent"})
CONSENT_TTL = 120

Action = Literal["read_rows", "read_aggregate", "write_cell", "add_row",
                 "start_agent", "agent_runs", "run_result"]


class AppCallInput(BaseModel):
    app_id: int
    action: Action
    table: Optional[str] = Field(default=None, description="The table's name in the manifest.")
    # read_rows : limit, offset, order_by, order_dir, filter, q — read_aggregate :
    # group_by, metrics, filter — agent_runs : limit.
    options: dict = Field(default_factory=dict)
    agent: Optional[str] = Field(default=None, description="The agent's name in the manifest.")
    row_id: Optional[str] = None
    field: Optional[str] = None
    value: Optional[str] = None
    expected_revision: Optional[str] = None
    values: Optional[dict] = None
    input: Optional[str] = None
    job_id: Optional[int] = None
    consent: Optional[str] = Field(
        default=None, description="write_cell, add_row, start_agent: the token "
        "`POST /api/me/apps/{app_id}/consent` returned for this exact gesture.")


class AppRun(BaseModel):
    job_id: int
    status: str
    started_at: Optional[Any] = None
    finished_at: Optional[Any] = None
    error: Optional[str] = None
    # Seulement pour un run que le lecteur a lancé.
    mine: bool = False
    outcome: Optional[str] = None
    note: Optional[str] = None


class AppCallOut(BaseModel):
    rows: Optional[list[dict]] = None
    total: Optional[int] = None
    groups: Optional[list[dict]] = None
    row: Optional[dict] = None
    job_id: Optional[int] = None
    runs: Optional[list[AppRun]] = None
    run: Optional[AppRun] = None


class AppConsentOut(BaseModel):
    consent: str
    expires_in: int


def _refus(status: int, code: str, msg: str) -> AuthzDenied:
    return AuthzDenied(status, code, msg)


def _app_pour(ctx: ResolvedCtx, app_id: int) -> dict:
    app = db.apps.get_for_call(int(app_id))
    if (app is None or app.get("archived_at") is not None
            or not ownership.can_access(ctx.sub, APP_RTYPE, str(app["id"]), "read")):
        raise _refus(404, "unknown_app", f"App {app_id} doesn't exist or isn't shared with you.")
    if not app.get("updated_by"):
        raise _refus(403, "app_author_unknown",
                     "Nobody is known to have written this app, so it can't act for anyone.")
    return app


def _table(app: dict, alias: Optional[str], want: str = "read") -> tuple[str, dict]:
    t = (app["manifest"].get("tables") or {}).get(alias or "")
    if not isinstance(t, dict) or not str(t.get("table") or "").isdigit():
        raise _refus(400, "undeclared_table", f"This app doesn't declare a table named “{alias}”.")
    ns = str(t["table"])
    if not tableau_de_l_org(ns, org_de_l_app(app)):
        raise _refus(403, "table_outside_project_org",
                     f"Table {ns} no longer lives in this app's organization.")
    if not ownership.can_access(app["updated_by"], ownership.TYPE_RESSOURCE_DATASTORE, ns, want):
        raise _refus(403, "app_author_lost_access",
                     f"This app's author can no longer {want} table {ns}: the app can't either.")
    return ns, t


def _lisible(t: dict, champ: str) -> bool:
    """Un champ déclaré — ou l'élément d'une liste par sa colonne (`tags[]`,
    `contacts[].role`). Sans liste de champs, tout ce que le lecteur voit."""
    fields = t.get("fields")
    return fields is None or champ.split("[")[0].split(".")[0] in fields


def _filtre(t: dict, raw: Any) -> Optional[str]:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise _refus(400, "invalid_filter", "A filter is an object of field → value.")
    caches = sorted(k for k in raw if k not in _DATES and not _lisible(t, k))
    if caches:
        raise _refus(403, "undeclared_field",
                     f"An app can only filter on the fields it declares (not {', '.join(caches)}).")
    return json.dumps(raw) if raw else None


def _entier(o: dict, cle: str, defaut: int) -> int:
    """Une option numérique : absente → `defaut`, autre chose qu'un entier → 400."""
    v = o.get(cle)
    if v is None:
        return defaut
    if isinstance(v, bool) or not isinstance(v, int):
        raise _refus(400, "invalid_options", f"`options.{cle}` must be an integer.")
    return v


def _projeter(t: dict, row: dict) -> dict:
    fields = t.get("fields")
    if fields is None:
        return row
    return {k: v for k, v in row.items() if k in fields or k in _META}


def _agent(app: dict, alias: Optional[str], org: Optional[int], viewer: str, *,
           run: bool) -> dict:
    """L'agent DÉCLARÉ sous `alias`, vu dans l'org de l'app : le lecteur doit
    le voir, l'auteur le voir (lire ses runs) ou l'écrire (le lancer)."""
    a = (app["manifest"].get("agents") or {}).get(alias or "")
    if not isinstance(a, dict) or not str(a.get("id") or "").isdigit():
        raise _refus(400, "undeclared_agent", f"This app doesn't declare an agent named “{alias}”.")
    if run and not a.get("run"):
        raise _refus(403, "agent_not_runnable", f"This app may read “{alias}” but not start it.")
    trig = db.get_trigger(int(a["id"]), int(org)) if org is not None else None
    if trig is None or _acces_agent.niveau(viewer, int(org), trig) is None:
        raise _refus(404, "unknown_agent", f"Agent {a['id']} isn't one you can see here.")
    niveau_auteur = _acces_agent.niveau(app["updated_by"], int(org), trig)
    if niveau_auteur is None or (run and niveau_auteur not in _acces_agent.ECRIRE):
        raise _refus(403, "app_author_lost_access",
                     f"This app's author can no longer {'run' if run else 'see'} agent "
                     f"{a['id']}: the app can't either.")
    return {**a, "trigger": trig}


def _run(job: dict, viewer: str, resultats: dict) -> dict:
    mine = job.get("sub") == viewer
    r = (resultats.get(job.get("run_id") or "") or {}) if mine else {}
    note = r.get("note")
    return {"job_id": int(job["id"]), "status": job["status"],
            "started_at": job.get("created_at"), "finished_at": job.get("finished_at"),
            "error": job.get("last_error"), "mine": mine, "outcome": r.get("outcome"),
            "note": note[:MAX_NOTE] if isinstance(note, str) else None}


def _empreinte(inp: AppCallInput) -> str:
    """Les arguments du geste, tels que le serveur les lit — jamais une empreinte
    envoyée par le client."""
    corps = {k: getattr(inp, k) for k in ("action", "table", "agent", "row_id", "field",
                                           "value", "expected_revision", "values", "input")}
    brut = json.dumps(corps, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      default=str)
    return hashlib.sha256(brut.encode("utf-8")).hexdigest()


def _hash_jeton(jeton: str) -> str:
    return hashlib.sha256(jeton.encode("utf-8")).hexdigest()


def _geste(ctx: ResolvedCtx, app: dict, inp: AppCallInput):
    """Juge un geste qui écrit ou lance — TOUT ce que l'appel jugera, sans rien
    faire — et rend de quoi le faire. Le consentement est émis après ce jugement ;
    l'appel le refait, puis brûle le jeton, puis agit."""
    from .datastore import rows as R

    if inp.action == "write_cell":
        ns, t = _table(app, inp.table, "write")
        allowed = (t.get("set") or {}).get(inp.field or "")
        if not allowed:
            raise _refus(403, "undeclared_write", f"This app may not change “{inp.field}”.")
        if inp.value not in allowed:
            raise _refus(403, "undeclared_value",
                         f"“{inp.value}” isn't a value this app may set {inp.field} to.")
        if not inp.row_id or not inp.expected_revision:
            # Une app écrit une ligne qu'elle a MONTRÉE : sans révision, elle écraserait
            # ce qu'un autre a changé depuis.
            raise _refus(400, "revision_required",
                         "An app's write names the row's `_revision` as it was shown.")

        def faire(_consent_id: int) -> dict:
            row = R._update_row(ctx, R.UpdateRowInput(
                datastore=ns, row_id=inp.row_id, patch={inp.field: inp.value},
                expected_revision=inp.expected_revision))
            return {"row": _projeter(t, row)}
        return faire

    if inp.action == "add_row":
        ns, t = _table(app, inp.table, "write")
        values = inp.values or {}
        cols = t.get("add") or []
        if not cols:
            raise _refus(403, "undeclared_write", "This app may not add rows to this table.")
        extra = sorted(k for k in values if k not in cols)
        if extra:
            raise _refus(403, "undeclared_write", f"This app may not fill: {', '.join(extra)}.")
        if not values:
            raise _refus(400, "empty_row", "The new row is empty.")

        def faire(_consent_id: int) -> dict:
            row = R._append_row(ctx, R.AppendRowInput(datastore=ns, row=values))
            return {"row": _projeter(t, row)}
        return faire

    # start_agent
    from .runner_triggers import TriggerInput, _triggers_sync

    org = org_de_l_app(app)
    a = _agent(app, inp.agent, org, ctx.sub, run=True)
    handoff = None
    if inp.input:
        if not a.get("input"):
            raise _refus(403, "agent_input_not_allowed",
                         f"This app may not pass an input to “{inp.agent}”.")
        if len(inp.input) > MAX_INPUT:
            raise _refus(400, "agent_input_too_long", f"An input is at most {MAX_INPUT} characters.")
        # Marquée comme une DONNÉE venue d'une app : le run la lit après sa consigne.
        handoff = f"Input from the app “{app['title']}” (app {app['id']}):\n{inp.input}"

    def faire(consent_id: int) -> dict:
        # ⚠️ `run_now` ne déduplique pas un run qui reçoit un `handoff` (une chaîne
        # d'agents en passe un à chaque maillon). Un run lancé par une app, si : un
        # lecteur qui clique deux fois, ou deux lecteurs, ne doublent pas le run.
        en_attente = db.file_du_declencheur(int(a["id"]), int(org))["pending"]
        if en_attente:
            raise _refus(409, "run_already_queued",
                         "A run of this agent is already waiting for a worker: it will "
                         "start as soon as one is free.")
        # Lancé dans l'org de l'app, au nom du lecteur : `run_now` juge son
        # accès en écriture à l'agent, son abonnement, le modèle, la clé.
        out = _triggers_sync(ResolvedCtx(sub=ctx.sub, org_id=int(org)),
                             TriggerInput(op="run_now", trigger_id=int(a["id"]),
                                          handoff=handoff))
        job_id = int(out["job"]["id"])
        db.apps.consent_job(consent_id, job_id)
        return {"job_id": job_id}
    return faire


def _consent(ctx: ResolvedCtx, inp: AppCallInput) -> dict:
    """Émet le jeton d'UN geste, après l'avoir jugé comme l'appel le jugera."""
    if inp.action not in GESTES:
        raise _refus(400, "consent_not_needed", f"`{inp.action}` doesn't take a consent.")
    app = _app_pour(ctx, inp.app_id)
    _geste(ctx, app, inp)
    jeton = secrets.token_urlsafe(32)
    db.apps.consent_issue(int(app["id"]), ctx.sub, inp.action, _empreinte(inp),
                          _hash_jeton(jeton), CONSENT_TTL)
    return {"consent": jeton, "expires_in": CONSENT_TTL}


def _call(ctx: ResolvedCtx, inp: AppCallInput) -> dict:
    from .datastore import rows as R

    app = _app_pour(ctx, inp.app_id)
    o = inp.options or {}

    if inp.action in GESTES:
        faire = _geste(ctx, app, inp)
        if not inp.consent:
            raise _refus(403, "consent_required",
                         "This gesture needs the viewer's consent: ask "
                         "`POST /api/me/apps/{app_id}/consent` with the same body.")
        g = geste.courant()
        consent_id = db.apps.consent_burn(int(app["id"]), ctx.sub, inp.action,
                                          _empreinte(inp), _hash_jeton(inp.consent),
                                          g.geste_id if g else None)
        if consent_id is None:
            raise _refus(403, "consent_invalid",
                         "This consent is used, expired, or for another gesture: ask again.")
        return faire(consent_id)

    if inp.action == "agent_runs":
        org = org_de_l_app(app)
        a = _agent(app, inp.agent, org, ctx.sub, run=False)
        jobs = db.apps.agent_jobs(int(org), int(a["id"]),
                                  min(max(_entier(o, "limit", 10), 1), MAX_RUNS))
        resultats = db.apps.run_results(j.get("run_id") for j in jobs if j.get("sub") == ctx.sub)
        return {"runs": [_run(j, ctx.sub, resultats) for j in jobs]}

    if inp.action == "run_result":
        if inp.job_id is None:
            raise _refus(400, "missing_job_id", "`job_id` is required.")
        org = org_de_l_app(app)
        job = db.apps.job_of_org(int(org), int(inp.job_id)) if org is not None else None
        declares = {str(a.get("id")): alias
                    for alias, a in (app["manifest"].get("agents") or {}).items()
                    if isinstance(a, dict)}
        # Un run que le LECTEUR a lancé, d'un agent que l'app déclare.
        if job is None or job.get("sub") != ctx.sub or job.get("trigger_id") not in declares:
            raise _refus(404, "unknown_run", "No run you started of this app's agents has that id.")
        _agent(app, declares[job["trigger_id"]], org, ctx.sub, run=False)
        resultats = db.apps.run_results([job.get("run_id")]) if job["status"] in _JOB_DONE else {}
        return {"run": _run(job, ctx.sub, resultats)}

    ns, t = _table(app, inp.table)

    if inp.action == "read_rows":
        order_by = o.get("order_by")
        if order_by is not None and (not isinstance(order_by, str)
                                     or (order_by not in _DATES and not _lisible(t, order_by))):
            raise _refus(403, "undeclared_field", "An app can only sort on a field it declares.")
        q = o.get("q")
        if q and t.get("fields") is not None:
            raise _refus(403, "search_needs_all_fields",
                         "Search runs over every field: it needs a table declared without a field list.")
        page = R._list_rows(ctx, R.ListRowsInput(
            datastore=ns, limit=min(max(_entier(o, "limit", 50), 1), MAX_ROWS),
            offset=max(_entier(o, "offset", 0), 0), order_by=order_by,
            order_dir="asc" if o.get("order_dir") == "asc" else "desc",
            q=q if isinstance(q, str) and q else None, filter=_filtre(t, o.get("filter"))))
        return {"rows": [_projeter(t, r) for r in page.get("rows") or []],
                "total": page.get("total")}

    # read_aggregate
    group_by = o.get("group_by")
    if group_by is not None and (not isinstance(group_by, str) or not _lisible(t, group_by)):
        raise _refus(403, "undeclared_field", "An app can only group on a field it declares.")
    metrics = o.get("metrics") or [{"op": "count_rows", "label": "count"}]
    if not isinstance(metrics, list) or len(metrics) > 10:
        raise _refus(400, "invalid_metrics", "Metrics are a list of at most 10.")
    propres = []
    for i, m in enumerate(metrics):
        m = m if isinstance(m, dict) else {}
        if m.get("op") not in _AGG_OPS:
            raise _refus(400, "invalid_metrics", f"Unknown metric “{m.get('op')}”.")
        if m.get("field") is not None and (not isinstance(m["field"], str) or not _lisible(t, m["field"])):
            raise _refus(403, "undeclared_field", "An app can only measure the fields it declares.")
        # Pas de `where` par métrique : il filtrerait sur un champ non déclaré.
        propres.append({"op": m["op"], **({"field": m["field"]} if m.get("field") else {}),
                        "label": m.get("label") if isinstance(m.get("label"), str) else f"m{i}"})
    out = R._aggregate(ctx, R.AggregateInput(
        datastore=ns, group_by=group_by, metrics=json.dumps(propres),
        filter=_filtre(t, o.get("filter"))))
    return {"groups": out.get("groups") or []}


CAPABILITIES += [
    Capability(
        key="me.app.call", handler=_call, Input=AppCallInput, Output=AppCallOut,
        authz=ORG_MEMBER,
        description=(
            "What an open app asks, relayed by the front on behalf of its viewer: read "
            "rows or aggregates of a declared table (declared fields only), set a "
            "declared field to a declared value (with the row's `_revision`), add a "
            "row with declared columns, start a declared agent, follow its runs. "
            "Allowed only when BOTH the app's author and its viewer may do it, on "
            "tables and agents of the app's organization. Writing and "
            "starting need `consent`, from `POST /api/me/apps/{app_id}/consent`. "
            "Interactive sessions only (no API token)."
        ),
        mcp=None,
        rest=RestBinding("POST", "/api/me/apps/{app_id}/call", allow_api_token=False),
    ),
    Capability(
        key="me.app.consent", handler=_consent, Input=AppCallInput, Output=AppConsentOut,
        authz=ORG_MEMBER,
        description=(
            "The viewer approved one gesture of an open app (write_cell, add_row, "
            "start_agent): judged exactly as the call will be, it returns a one-time "
            "token, valid two minutes, for that gesture and those arguments only. "
            "Asked by the front's parent frame after the viewer confirms — never by the "
            "app. Interactive sessions only (no API token)."
        ),
        mcp=None,
        rest=RestBinding("POST", "/api/me/apps/{app_id}/consent", allow_api_token=False),
    ),
]
