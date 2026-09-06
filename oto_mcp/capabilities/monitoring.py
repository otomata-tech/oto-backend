"""Capacités monitoring / investigation plateforme (ADR 0009/0017, console ADR 0047).

Les lentilles d'observabilité `/api/admin/monitoring/*` migrent des routes écrites
main d'`api/routes.py` vers des capacités co-déclarées — mêmes chemins, même autz
(PLATFORM_ADMIN), mêmes payloads (contrat dashboard inchangé) — et gagnent leur
face MCP via la console consolidée `oto_admin_monitoring(op=…)` : l'agent
plateforme investigue EN SESSION (drill-down agrégats → journal filtré → fiche
d'appel → run/session), plus seulement via le dashboard.

Les projections runs/gaps/tool_quality restent déclarées dans `usage.py` (leur
domicile ADR 0017) ; la console les réutilise telles quelles. Les signaux ont
déjà leur console (`oto_admin_signal`) — pas dupliqués ici.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from pydantic import BaseModel, field_validator

from .. import db
from . import usage
from ._authz import PLATFORM_ADMIN
from ._types import cap_limit, AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES


def _resolve_sub(target: Optional[str]) -> Optional[str]:
    """Filtre appelant en email OU sub → sub (confort agentique : on investigue
    « les appels de jane@acme.test », pas d'un sub opaque). None passe tel quel."""
    if not target:
        return None
    from .orgs.members import _resolve_target
    return _resolve_target(target)


# ── lentilles (une capacité par verbe, faces REST idem routes historiques) ───

class SummaryInput(BaseModel):
    days: int = 7
    org_id: Optional[int] = None   # restreindre à UN workspace
    sub: Optional[str] = None      # restreindre à UN appelant (email ou sub)


class RestInput(BaseModel):
    days: int = 7
    org_id: Optional[int] = None   # org de CONSULTATION revendiquée (best-effort, #451)
    sub: Optional[str] = None      # restreindre à UN appelant (email ou sub)
    # Une route (préfixe) — compte EXACT, jamais tronqué par le `LIMIT 100` de
    # `by_route` (oto-dashboard#125 : mesurer une route à faible volume, invisible
    # dans le top-100 sans que rien ne le dise).
    route: Optional[str] = None


class ConnectorsInput(BaseModel):
    days: int = 7
    org_id: Optional[int] = None   # les échecs subis SOUS cette org


class FunnelInput(BaseModel):
    days: int = 30


class CallsInput(BaseModel):
    limit: int = 200
    sub: Optional[str] = None            # email ou sub
    tool: Optional[str] = None
    errors: bool = False
    days: Optional[int] = None
    org_id: Optional[int] = None
    run_id: Optional[str] = None
    session_id: Optional[str] = None
    min_duration_ms: Optional[int] = None
    error_contains: Optional[str] = None

    @field_validator("limit")
    @classmethod
    def _cap_limit(cls, v):
        return cap_limit(v, 200)


class CallInput(BaseModel):
    call_id: int


def _summary(ctx: ResolvedCtx, inp: SummaryInput) -> dict:
    return db.tool_call_stats(since_days=inp.days, org_id=inp.org_id,
                              sub=_resolve_sub(inp.sub))


def _rest_stats(ctx: ResolvedCtx, inp: RestInput) -> dict:
    return db.rest_call_stats(since_days=inp.days, org_id=inp.org_id,
                              sub=_resolve_sub(inp.sub), route=inp.route)


def _connector_stats(ctx: ResolvedCtx, inp: ConnectorsInput) -> dict:
    return db.connector_failure_stats(since_days=inp.days, org_id=inp.org_id)


def _funnel(ctx: ResolvedCtx, inp: FunnelInput) -> dict:
    return db.activation_funnel(active_window_days=inp.days)


# Fenêtre du plancher quand rien ne la borne (ni `days`, ni une page pleine) : le
# compte parcourt le journal en temps linéaire (28 ms/jour mesurés en prod, #630).
FLOOR_WINDOW_DAYS = 30


def _instant(v) -> datetime:
    """`called_at` tel que le row factory le rend (chaîne naïve, UTC) ou un datetime."""
    if isinstance(v, str):
        v = datetime.fromisoformat(v)
    return v if v.tzinfo else v.replace(tzinfo=timezone.utc)


def _horizon(calls: list, inp: CallsInput) -> tuple[datetime, str]:
    """Depuis quand compter ce que la page ne montre pas — la fenêtre de la page
    elle-même, pour que les deux nombres se comparent : `days` s'il est donné ; sinon,
    page PLEINE ⇒ son appel le plus ancien ; sinon une fenêtre bornée, et DITE."""
    now = datetime.now(timezone.utc)
    if inp.days is not None:
        return now - timedelta(days=inp.days), f"sur {inp.days} j"
    if calls and len(calls) >= inp.limit:
        oldest = min(_instant(c["called_at"]) for c in calls)
        return oldest, f"dans la fenêtre de cette page (depuis {oldest.isoformat()})"
    return now - timedelta(days=FLOOR_WINDOW_DAYS), f"sur {FLOOR_WINDOW_DAYS} j"


def calls_with_scope(inp: CallsInput) -> dict:
    """La page du journal et — quand elle est scopée à une org — ce que ce scope
    laisse dehors (#630).

    Scope d'une vue d'org = les appels RÉSOLUS sous cette org (`tool_calls.org_id`,
    stampé à l'appel). Un appel d'un run de l'org résolu sous une AUTRE org — l'axe
    `_org` absent fait retomber l'appel sur l'org maison de l'appelant (#631) — n'y
    figure pas, alors que `op=run` le montre. Vécu le 29/08 : trois lectures filtrées
    à zéro sur un refus que le déroulé montrait. La vue reste exacte dans son
    périmètre ; elle DIT désormais le périmètre, et compte ce qu'il exclut sous les
    mêmes filtres — même quand c'est 0, pour que le zéro se lise comme « regardé ».
    Sans scope d'org, rien n'est dehors : pas de champ."""
    filtres = dict(sub=_resolve_sub(inp.sub), tool_name=inp.tool, errors_only=inp.errors,
                   since_days=inp.days, run_id=inp.run_id, session_id=inp.session_id,
                   min_duration_ms=inp.min_duration_ms, error_contains=inp.error_contains)
    calls = db.list_tool_calls(limit=inp.limit, org_id=inp.org_id, **filtres)
    if inp.org_id is None:
        return {"calls": calls}
    since, fenetre = _horizon(calls, inp)
    hors = db.count_calls_of_org_runs_elsewhere(inp.org_id, since=since, **filtres)
    out = {
        "calls": calls,
        "scope": (f"appels RÉSOLUS sous l'org {inp.org_id} (colonne org_id) — un appel "
                  "d'un run de cette org résolu sous une autre org (axe `_org` absent ⇒ "
                  "org maison de l'appelant) n'y figure pas ; `op=run` le montre"),
        "hors_scope": hors,
    }
    if hors:
        out["hors_scope_hint"] = (
            f"{hors} appel(s) de runs de l'org {inp.org_id}, résolus sous une autre org, "
            f"correspondent aux mêmes filtres {fenetre} et ne sont PAS listés — "
            "`op=run run_id=…` les montre.")
    return out


def _calls(ctx: ResolvedCtx, inp: CallsInput) -> dict:
    return calls_with_scope(inp)


def _call(ctx: ResolvedCtx, inp: CallInput) -> dict:
    row = db.get_tool_call(inp.call_id)
    if row is None:
        raise AuthzDenied(404, "unknown_call", f"Aucun appel id={inp.call_id}.")
    return {"call": row}


# ── console MCP consolidée `oto_admin_monitoring(op=…)` (pattern ADR 0047) ───

class MonitoringInput(BaseModel):
    op: Literal["summary", "rest", "connectors", "funnel", "calls", "call",
                "runs", "run", "gaps", "tool_quality"]
    days: Optional[int] = None            # fenêtre (défaut : 7 ; funnel/gaps/tool_quality : 30)
    limit: Optional[int] = None           # calls (défaut 200) / runs (défaut 100)
    sub: Optional[str] = None             # summary/rest/calls : appelant (email ou sub)
    tool: Optional[str] = None            # calls : filtre outil exact
    errors: bool = False                  # calls : erreurs seulement
    org_id: Optional[int] = None          # summary/rest/connectors/calls : un workspace
    route: Optional[str] = None           # rest : une route (préfixe), compte exact
    run_id: Optional[str] = None          # run (requis) / calls (filtre)
    session_id: Optional[str] = None      # calls : tous les appels d'une conversation
    min_duration_ms: Optional[int] = None  # calls : appels lents
    error_contains: Optional[str] = None  # calls : recherche dans le message d'erreur
    call_id: Optional[int] = None         # call (requis)

    # Console op-aware : plafond du plus large de ses ops (`calls`, 200). Écrête au
    # lieu de refuser — une valeur énorme partait telle quelle au SQL, une négative
    # faisait échouer la requête en 500 (#300).
    @field_validator("limit")
    @classmethod
    def _cap_limit(cls, v):
        return cap_limit(v, 200, default=None) if v is not None else None


def _need(val, code: str, msg: str):
    if val is None or (isinstance(val, str) and not val.strip()):
        raise AuthzDenied(400, code, msg)
    return val


# Ce que CHAQUE op LIT vraiment. Un champ hors de sa liste n'est pas « toléré », il est
# REFUSÉ (#451) : la console acceptait `sub`/`org_id` sur `op=rest` et ne les passait
# nulle part — on croyait lire l'activité REST d'un compte, on lisait la plateforme
# entière, sans rien pour s'en apercevoir. Accepter puis jeter est le pire des trois
# comportements possibles ; on honore là où la donnée existe, on refuse ailleurs.
# ⚠️ À tenir à jour avec le dispatch ci-dessous — le test le vérifie dans les DEUX sens.
_CHAMPS_LUS: dict[str, set[str]] = {
    "summary": {"days", "org_id", "sub"},
    "rest": {"days", "org_id", "sub", "route"},
    "connectors": {"days", "org_id"},
    "funnel": {"days"},
    "calls": {"days", "limit", "sub", "tool", "errors", "org_id", "run_id",
              "session_id", "min_duration_ms", "error_contains"},
    "call": {"call_id"},
    "runs": {"limit"},
    "run": {"run_id"},
    "gaps": {"days"},
    "tool_quality": {"days"},
}


def _refuser_les_champs_muets(inp: MonitoringInput) -> None:
    """Refus NOMMÉ des filtres que l'op choisie ne lit pas.

    ⚠️ Sur la face MCP, `model_fields_set` ne tranche rien (fastmcp remplit les défauts
    avant d'appeler le handler) : le seul discriminant est « la valeur diffère-t-elle du
    défaut ». D'où la comparaison aux défauts déclarés, pas aux champs « fournis »."""
    lus = _CHAMPS_LUS[inp.op]
    muets = sorted(
        nom for nom, champ in MonitoringInput.model_fields.items()
        if nom != "op" and nom not in lus and getattr(inp, nom) != champ.default
    )
    if not muets:
        return
    def _porteuses(nom: str) -> str:
        ops = sorted(o for o, champs in _CHAMPS_LUS.items() if nom in champs)
        return f"`{nom}` : " + (f"op={'/'.join(ops)}" if ops else "aucune op")

    raise AuthzDenied(400, "param_not_read_by_op", (
        f"op={inp.op} ne lit pas {', '.join('`' + m + '`' for m in muets)} : le filtre "
        "serait ignoré et la réponse porterait plus large que demandé (c'est ainsi "
        "qu'on lit la plateforme entière en croyant lire un compte). Là où ces filtres "
        "sont lus — " + " ; ".join(_porteuses(m) for m in muets) + "."))


def _monitoring(ctx: ResolvedCtx, inp: MonitoringInput) -> dict:
    _refuser_les_champs_muets(inp)
    if inp.op == "summary":
        return _summary(ctx, SummaryInput(days=inp.days or 7, org_id=inp.org_id, sub=inp.sub))
    if inp.op == "rest":
        return _rest_stats(ctx, RestInput(days=inp.days or 7, org_id=inp.org_id,
                                          sub=inp.sub, route=inp.route))
    if inp.op == "connectors":
        return _connector_stats(ctx, ConnectorsInput(days=inp.days or 7,
                                                     org_id=inp.org_id))
    if inp.op == "funnel":
        return _funnel(ctx, FunnelInput(days=inp.days or 30))
    if inp.op == "calls":
        return _calls(ctx, CallsInput(
            limit=inp.limit or 200, sub=inp.sub, tool=inp.tool, errors=inp.errors,
            days=inp.days, org_id=inp.org_id, run_id=inp.run_id,
            session_id=inp.session_id, min_duration_ms=inp.min_duration_ms,
            error_contains=inp.error_contains))
    if inp.op == "call":
        return _call(ctx, CallInput(call_id=_need(
            inp.call_id, "missing_call_id", "`call_id` requis pour call.")))
    if inp.op == "runs":
        return usage._runs(ctx, usage.RunsInput(limit=inp.limit or 100))
    if inp.op == "run":
        return usage._run(ctx, usage.RunInput(run_id=_need(
            inp.run_id, "missing_run_id", "`run_id` requis pour run.")))
    if inp.op == "gaps":
        return usage._gaps(ctx, usage.DaysInput(days=inp.days or 30))
    return usage._tool_quality(ctx, usage.DaysInput(days=inp.days or 30))  # tool_quality


CAPABILITIES += [
    Capability(key="monitoring.summary", handler=_summary, Input=SummaryInput,
               authz=PLATFORM_ADMIN,
               rest=RestBinding("GET", "/api/admin/monitoring/summary")),
    Capability(key="monitoring.rest", handler=_rest_stats, Input=RestInput,
               authz=PLATFORM_ADMIN,
               rest=RestBinding("GET", "/api/admin/monitoring/rest")),
    Capability(key="monitoring.connectors", handler=_connector_stats,
               Input=ConnectorsInput,
               authz=PLATFORM_ADMIN,
               rest=RestBinding("GET", "/api/admin/monitoring/connectors")),
    Capability(key="monitoring.funnel", handler=_funnel, Input=FunnelInput,
               authz=PLATFORM_ADMIN,
               rest=RestBinding("GET", "/api/admin/monitoring/funnel")),
    Capability(key="monitoring.calls", handler=_calls, Input=CallsInput,
               authz=PLATFORM_ADMIN,
               rest=RestBinding("GET", "/api/admin/monitoring/calls")),
    Capability(key="monitoring.call", handler=_call, Input=CallInput,
               authz=PLATFORM_ADMIN,
               rest=RestBinding("GET", "/api/admin/monitoring/calls/{call_id}")),
    Capability(
        key="admin.monitoring", handler=_monitoring, Input=MonitoringInput,
        authz=PLATFORM_ADMIN,
        description=(
            "Platform observability console (platform admin). ⚠️ TWO journals, never "
            "one: summary/calls cover AGENT (MCP) calls ONLY — what a person does from "
            "the dashboard or the API is NOT there, it is in op=rest. An account at "
            "zero calls is therefore NOT an idle account: check op=rest before saying "
            "so. ⚠️ A filter an op does not read is REFUSED (error "
            "`param_not_read_by_op`), never silently ignored. op=summary (aggregates: "
            "totals, by tool w/ avg+p95 latency, by user, by day; optional `days`, "
            "`org_id`, `sub` email|sub) / calls (raw MCP call log, newest first; filters "
            "`sub`, `tool`, `errors`, `days`, `org_id`, `run_id`, `session_id`, "
            "`min_duration_ms` slow calls, `error_contains`) / call (`call_id` → full "
            "record incl. truncated args + correlation ids) / run (`run_id` → timeline) "
            "/ runs (recent runs) / rest (REST lens by route — `days`, `org_id`, `sub`, "
            "and `route` (prefix match) for an EXACT count/errors/last_call_at on one "
            "route — `by_route` is capped at the top 100, so a low-volume route can be "
            "invisible there without saying so; ⚠️ `org_id` here is the consultation "
            "org claimed by a header (best-effort): a request without it carries none "
            "and drops out of the filter, so a 0 does not prove an idle org — "
            "cross-check with `sub`) / "
            "connectors (credential resolution failures; optional `org_id`) / funnel "
            "(accounts vs real usage) / gaps · tool_quality (aggregated usage signals). "
            "For raw signals use oto_admin_signal."),
        mcp="oto_admin_monitoring",
    ),
]

