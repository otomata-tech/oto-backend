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

from datetime import date, datetime, timedelta, timezone
from typing import Literal, Optional

from pydantic import BaseModel, field_validator

from .. import db
from . import usage
from ..connectors import orphelins as connecteurs_orphelins
from ._authz import PLATFORM_ADMIN
from ._types import cap_limit, AuthzDenied, Capability, DeclaredError, ResolvedCtx, RestBinding
from .registry import CAPABILITIES
from ._lecture_bornee import bornee


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


class TransportInput(BaseModel):
    days: int = 7
    # Pas de filtre d'environnement : la réponse VENTILE par environnement, elle ne
    # le choisit pas. Préproduction et production écrivent dans la même base ; un
    # filtre par défaut y cacherait la moitié du trafic sans le dire.


class TransportRefusalRow(BaseModel):
    env: str                      # `production` | `canari` | `inconnu`
    cause: str                    # vocabulaire FERMÉ, cf. `transport_refusals._MARQUEURS`
    statut: Optional[int] = None  # le code HTTP servi — plusieurs causes le partagent
    n: int
    last_at: Optional[datetime] = None


class TransportVersionRow(BaseModel):
    env: str
    version_demandee: str
    n: int


class RateLimitRow(BaseModel):
    env: str                      # `production` | `canari` | `inconnu`
    classe: str                   # jeton | anonyme | sensible | webhook | webhook_adresse | echecs
    mode: str                     # report | enforce — `would_refuse` se lit en report
    minutes: int                  # minutes de la fenêtre où la classe a servi
    allowed: Optional[int] = None
    would_refuse: Optional[int] = None
    refused: Optional[int] = None
    peak_pct: Optional[float] = None   # plus haut usage d'un seau (% de sa rafale)
    limit_per_minute: Optional[int] = None
    last_at: Optional[datetime] = None


class RateLimitTopRow(BaseModel):
    """Une clé HACHÉE que `enforce` refuse ou refuserait, un jour donné (#1232)."""
    jour: date                    # jour UTC
    env: str                      # `production` | `canari` | `inconnu`
    limiteur: str                 # debit | platform_rate | corps | outil | sql | session | credits
    # debit : la classe du seau ; platform_rate : le connecteur ; bornes du processus
    # (`bornes_processus`) : corps → defaut|upload, outil → l'outil, sql → la fonction
    # appelante, session → mcp ; credits (budget d'unités, des SIGNAUX : `refus` y
    # compte les signaux) → unpriced:<connector>.<function> | unreported:<outil>
    classe: str
    cle: str                      # empreinte (sha256 tronqué) ; `*` = clés au-delà de la borne
    rang: int                     # 1 = la clé la plus refusée du (jour, env, limiteur)
    refus: int
    mode: str                     # report | enforce — le dernier vu ce jour-là
    premier_at: Optional[datetime] = None
    dernier_at: Optional[datetime] = None


class TransportRefusals(BaseModel):
    """Ce que le transport refuse avant tout dispatch, ventilé par cause, et la mesure
    de la limite de débit applicative."""
    since_days: int
    total: int
    by_cause: list[TransportRefusalRow]
    # Les versions de protocole réclamées puis refusées : le signal qui dira, le jour
    # venu, qu'un logiciel client est écarté par notre plancher de protocole.
    protocol_versions_refused: list[TransportVersionRow]
    # La limite de débit (F-025, `limite_debit_mesure`) : une ligne par
    # (environnement, classe, mode), sommée sur la fenêtre. De quoi régler les bornes
    # sur le trafic réel avant de passer `OTO_DEBIT_MODE` en `enforce`.
    rate_limit: list[RateLimitRow] = []
    # QUI serait coupé (#1232, `limite_refus_top`) : les premières clés hachées par
    # (jour, environnement, limiteur), limite de débit, clé plateforme et bornes
    # globales du processus (corps, délai d'outil, `statement_timeout`, sessions).
    rate_limit_top: list[RateLimitTopRow] = []


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

    # Le plafond du STORE (`db.CALLS_LIMITE_MAX`), pas un second, plus bas : la
    # lentille d'org passe par ce modèle, et un 200 posé ici écrêtait en silence le
    # `limit` qu'elle annonçait (usage signal 1553).
    @field_validator("limit")
    @classmethod
    def _cap_limit(cls, v):
        return cap_limit(v, db.CALLS_LIMITE_MAX)


class CallInput(BaseModel):
    call_id: int


class RestCallRow(BaseModel):
    id: int
    sub: Optional[str] = None
    email: Optional[str] = None
    route: str
    called_at: datetime
    duration_ms: Optional[int] = None
    ok: bool
    error: Optional[str] = None
    org_id: Optional[int] = None
    # La cible du « voir en tant que » APPLIQUÉE (ADR 0023, `ViewAsMiddleware` →
    # `RestCallLogger`) — `None` quand aucune vue n'a été appliquée, jamais l'en-tête brut. C'est le champ que cette lentille
    # existe pour servir (oto-backend#962).
    view_as_sub: Optional[str] = None


class RestCallsOutput(BaseModel):
    calls: list[RestCallRow]


class RestCallsInput(BaseModel):
    limit: int = 200
    days: Optional[int] = None
    sub: Optional[str] = None
    org_id: Optional[int] = None
    route: Optional[str] = None

    @field_validator("limit")
    @classmethod
    def _cap_limit(cls, v):
        return cap_limit(v, 200)


#: Les fenêtres d'un résumé d'appels (#1145). SANS périmètre (ni `org_id` ni `sub`),
#: la lecture parcourt le journal de TOUTE la plateforme : un agent l'a demandée le
#: 04/10/2026 sur un jour et elle a tenu une connexion 452 s, sur 60 jours plus de
#: deux minutes. Bornée plutôt que refusée — la console plateforme du tableau de
#: bord en est le consommateur légitime, sur 7 jours par défaut. Avec un périmètre,
#: la rétention du journal suffit comme borne.
RESUME_JOURS_MAX_SANS_PERIMETRE = 7
RESUME_JOURS_MAX = 90

_REFUS_RESUME = DeclaredError(
    400, "days_too_large",
    f"`days` au-delà de {RESUME_JOURS_MAX_SANS_PERIMETRE} sans `org_id` ni `sub`, "
    f"ou au-delà de {RESUME_JOURS_MAX}")


def _summary(ctx: ResolvedCtx, inp: SummaryInput) -> dict:
    if inp.org_id is None and not inp.sub and inp.days > RESUME_JOURS_MAX_SANS_PERIMETRE:
        raise AuthzDenied(
            400, "days_too_large",
            f"Sans `org_id` ni `sub`, le résumé couvre toute la plateforme : "
            f"`days` vaut au plus {RESUME_JOURS_MAX_SANS_PERIMETRE} ({inp.days} demandé). "
            f"Passer `org_id` ou `sub` pour une fenêtre jusqu'à {RESUME_JOURS_MAX} jours.")
    if inp.days > RESUME_JOURS_MAX:
        raise AuthzDenied(400, "days_too_large",
                          f"`days` vaut au plus {RESUME_JOURS_MAX} ({inp.days} demandé) : "
                          "fenêtre la plus large d'un résumé.")
    return db.tool_call_stats(since_days=inp.days, org_id=inp.org_id,
                              sub=_resolve_sub(inp.sub))


def _rest_stats(ctx: ResolvedCtx, inp: RestInput) -> dict:
    return db.rest_call_stats(since_days=inp.days, org_id=inp.org_id,
                              sub=_resolve_sub(inp.sub), route=inp.route)


def _transport_stats(ctx: ResolvedCtx, inp: TransportInput) -> dict:
    """Ce que le transport MCP refuse AVANT tout dispatch, ventilé par cause.

    Ces refus ne traversent aucun middleware : ils sont absents du journal d'appels
    d'outils, de Sentry et de toute télémétrie bâtie sur les hooks. Ce compteur est
    leur seule source (cf. `oto_mcp/transport_refusals.py`).

    ⚠️ Un volume non nul est le RÉGIME NORMAL, pas une panne : ~2,2 % des `POST /mcp`
    de production sont refusés en permanence, et toutes les adresses sources mesurées
    le 11/09/2026 appartenaient à la plage de sortie de claude.ai — aucune à nos
    propres machines. Lire une hausse comme un incident interne serait une erreur.

    `rate_limit` porte la mesure de la limite de débit applicative (F-025) : par
    environnement, classe et mode, les décisions qui passent, celles qu'`enforce`
    refuserait (`would_refuse`, mode `report`) ou a refusées, et le plus haut usage
    d'un seau. Cf. `oto_mcp/limite_debit_mesure.py`.

    `rate_limit_top` dit QUI : par jour, environnement et limiteur (`debit`,
    `platform_rate`, les bornes du processus), les clés HACHÉES les plus refusées ; et,
    sous `credits`, les signaux du budget d'unités (`unpriced:`, `unreported:`), qui ne
    sont pas des refus. Cf. `oto_mcp/limite_refus_top.py`."""
    return db.transport_refusal_stats(since_days=inp.days)


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


def calls_with_scope(inp: CallsInput, *, before: Optional[tuple[str, int]] = None) -> dict:
    """La page du journal et — quand elle est scopée à une org — ce que ce scope
    laisse dehors (#630).

    Scope d'une vue d'org = les appels RÉSOLUS sous cette org (`tool_calls.org_id`,
    stampé à l'appel). Un appel d'un run de l'org résolu sous une AUTRE org — l'axe
    `_org` absent fait retomber l'appel sur l'org maison de l'appelant (#631) — n'y
    figure pas, alors que `op=run` le montre. Vécu le 29/08 : trois lectures filtrées
    à zéro sur un refus que le déroulé montrait. La vue reste exacte dans son
    périmètre ; elle DIT désormais le périmètre, et compte ce qu'il exclut sous les
    mêmes filtres — même quand c'est 0, pour que le zéro se lise comme « regardé ».
    Sans scope d'org, rien n'est dehors : pas de champ.

    `before` = la position où reprendre (keyset, cf. `db.list_tool_calls_page`) ; la
    position de la suite revient sous `_next` — `None` quand la page est la dernière —
    que chaque lentille emballe dans SON curseur ou retire."""
    filtres = dict(sub=_resolve_sub(inp.sub), tool_name=inp.tool, errors_only=inp.errors,
                   since_days=inp.days, run_id=inp.run_id, session_id=inp.session_id,
                   min_duration_ms=inp.min_duration_ms, error_contains=inp.error_contains)
    page = db.list_tool_calls_page(limit=inp.limit, org_id=inp.org_id, before=before,
                                   **filtres)
    calls = page["calls"]
    if inp.org_id is None:
        return {"calls": calls, "_next": page["next"]}
    since, fenetre = _horizon(calls, inp)
    hors = db.count_calls_of_org_runs_elsewhere(inp.org_id, since=since, **filtres)
    out = {
        "calls": calls,
        "scope": (f"appels RÉSOLUS sous l'org {inp.org_id} (colonne org_id) — un appel "
                  "d'un run de cette org résolu sous une autre org (axe `_org` absent ⇒ "
                  "org maison de l'appelant) n'y figure pas ; `op=run` le montre"),
        "hors_scope": hors,
        "_next": page["next"],
    }
    if hors:
        out["hors_scope_hint"] = (
            f"{hors} appel(s) de runs de l'org {inp.org_id}, résolus sous une autre org, "
            f"correspondent aux mêmes filtres {fenetre} et ne sont PAS listés — "
            "`op=run run_id=…` les montre.")
    return out


def _calls(ctx: ResolvedCtx, inp: CallsInput) -> dict:
    out = calls_with_scope(inp)
    out.pop("_next", None)       # la console plateforme ne pagine pas (pas de curseur)
    return out


def _rest_calls(ctx: ResolvedCtx, inp: RestCallsInput) -> dict:
    """Lentille plateforme, ligne par ligne — cf. `db.list_rest_calls` (oto-backend#962)
    pour ce que `view_as_sub` porte et pourquoi ni `op=calls` (MCP only) ni `op=rest`
    (agrégats) ne peuvent le montrer."""
    return {"calls": db.list_rest_calls(limit=inp.limit or 200, days=inp.days,
                                        sub=_resolve_sub(inp.sub), org_id=inp.org_id,
                                        route=inp.route)}


def _call(ctx: ResolvedCtx, inp: CallInput) -> dict:
    row = db.get_tool_call(inp.call_id)
    if row is None:
        raise AuthzDenied(404, "unknown_call", f"Aucun appel id={inp.call_id}.")
    return {"call": row}


# ── console MCP consolidée `oto_admin_monitoring(op=…)` (pattern ADR 0047) ───

class MonitoringInput(BaseModel):
    op: Literal["summary", "rest", "rest_calls", "connectors", "transport", "funnel",
                "calls", "call", "runs", "run", "gaps", "tool_quality", "agents_en_echec",
                "connecteurs_orphelins"]
    days: Optional[int] = None            # fenêtre (défaut : 7 ; funnel/gaps/tool_quality : 30)
    limit: Optional[int] = None           # calls/rest_calls (défaut = plafond 200) / runs (défaut 100)
    sub: Optional[str] = None             # summary/rest/rest_calls/calls : appelant (email ou sub)
    tool: Optional[str] = None            # calls : filtre outil exact
    errors: bool = False                  # calls : erreurs seulement
    org_id: Optional[int] = None          # summary/rest/rest_calls/connectors/calls : un workspace
    route: Optional[str] = None           # rest/rest_calls : préfixe de `MÉTHODE /route` (`GET /api/orgs`)
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
    "rest_calls": {"days", "limit", "org_id", "sub", "route"},
    "connectors": {"days", "org_id"},
    "transport": {"days"},
    "funnel": {"days"},
    "calls": {"days", "limit", "sub", "tool", "errors", "org_id", "run_id",
              "session_id", "min_duration_ms", "error_contains"},
    "call": {"call_id"},
    "runs": {"limit"},
    "run": {"run_id"},
    "gaps": {"days"},
    "tool_quality": {"days"},
    "agents_en_echec": {"days", "org_id"},
    # Un état présent de la base, pas une fenêtre : aucun filtre (#905).
    "connecteurs_orphelins": set(),
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
    if inp.op == "rest_calls":
        return _rest_calls(ctx, RestCallsInput(limit=inp.limit or 200, days=inp.days,
                                               sub=inp.sub, org_id=inp.org_id,
                                               route=inp.route))
    if inp.op == "connectors":
        return _connector_stats(ctx, ConnectorsInput(days=inp.days or 7,
                                                     org_id=inp.org_id))
    if inp.op == "transport":
        return _transport_stats(ctx, TransportInput(days=inp.days or 7))
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
    if inp.op == "agents_en_echec":
        # 1 jour par défaut : c'est une alerte, pas une histoire. Une fenêtre
        # large mélangerait une panne réparée hier avec celle de ce matin.
        return {"agents": db.agents_en_echec(inp.days or 1, org_id=inp.org_id)}
    if inp.op == "gaps":
        return usage._gaps(ctx, usage.DaysInput(days=inp.days or 30))
    if inp.op == "connecteurs_orphelins":
        return connecteurs_orphelins.constat()
    return usage._tool_quality(ctx, usage.DaysInput(days=inp.days or 30))  # tool_quality


CAPABILITIES += [
    Capability(key="monitoring.summary", handler=bornee(_summary), Input=SummaryInput,
               authz=PLATFORM_ADMIN, errors=(_REFUS_RESUME,),
               rest=RestBinding("GET", "/api/admin/monitoring/summary")),
    Capability(key="monitoring.rest", handler=bornee(_rest_stats), Input=RestInput,
               authz=PLATFORM_ADMIN,
               rest=RestBinding("GET", "/api/admin/monitoring/rest")),
    Capability(key="monitoring.rest_calls", handler=bornee(_rest_calls),
               Input=RestCallsInput, Output=RestCallsOutput,
               authz=PLATFORM_ADMIN,
               rest=RestBinding("GET", "/api/admin/monitoring/rest-calls")),
    Capability(key="monitoring.connectors", handler=bornee(_connector_stats),
               Input=ConnectorsInput,
               authz=PLATFORM_ADMIN,
               rest=RestBinding("GET", "/api/admin/monitoring/connectors")),
    Capability(key="monitoring.transport", handler=bornee(_transport_stats),
               Input=TransportInput, Output=TransportRefusals,
               authz=PLATFORM_ADMIN,
               rest=RestBinding("GET", "/api/admin/monitoring/transport")),
    Capability(key="monitoring.funnel", handler=bornee(_funnel), Input=FunnelInput,
               authz=PLATFORM_ADMIN,
               rest=RestBinding("GET", "/api/admin/monitoring/funnel")),
    Capability(key="monitoring.calls", handler=bornee(_calls), Input=CallsInput,
               authz=PLATFORM_ADMIN,
               rest=RestBinding("GET", "/api/admin/monitoring/calls")),
    Capability(key="monitoring.call", handler=_call, Input=CallInput,
               authz=PLATFORM_ADMIN,
               rest=RestBinding("GET", "/api/admin/monitoring/calls/{call_id}")),
    Capability(
        key="admin.monitoring", handler=bornee(_monitoring), Input=MonitoringInput,
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
            "record incl. truncated args + correlation ids) / run (`run_id` → timeline; "
            "`content_archived` = body in cold archive, bounds kept) "
            "/ runs (recent runs) / rest (REST lens by route — `days`, `org_id`, `sub`, "
            "and `route` (prefix match) for an EXACT count/errors/last_call_at on one "
            "route — `by_route` is capped at the top 100, so a low-volume route can be "
            "invisible there without saying so; `by_status` splits the errors by "
            "HTTP code over the same window and filters, so an expected 4xx is not "
            "read as an outage — `status: null` means NO response went through the "
            "log: an unhandled exception (the 500 is served above it — that is what "
            "a crash looks like) or a client that left; ⚠️ `org_id` here is the consultation "
            "org claimed by a header (best-effort): a request without it carries none "
            "and drops out of the filter, so a 0 does not prove an idle org — "
            "cross-check with `sub`) / rest_calls (raw REST call log, newest first, "
            "same filters as `rest` — `route` is a prefix of `METHOD /route`, e.g. "
            "`GET /api/orgs` — plus `limit` (max 200); the ONLY lens that serves "
            "`view_as_sub`: the account a platform operator's view-as was actually "
            "APPLIED to (operator verified, existing target other than themself), "
            "null otherwise — a refused or no-op X-Oto-View-As header leaves it null. "
            "Rows logged before 2026-09-21 carry the header as claimed, unverified) / "
            "connectors (credential resolution failures; optional `org_id`) / transport "
            "(MCP transport refusals by cause, plus `rate_limit`: the app rate limiter "
            "per env/class/mode — `would_refuse` is what `enforce` would cut, "
            "`peak_pct` the fullest bucket; and `rate_limit_top`: per day, env and "
            "limiter (`debit` | `platform_rate` | the process bounds), the top hashed keys "
            "`enforce` would cut — and under `credits`, the units-budget SIGNALS "
            "`unpriced:<connector.function>` / `unreported:<tool>`, not refusals; "
            "`days`) / funnel "
            "(accounts vs real usage) / gaps · tool_quality (aggregated usage signals) / "
            "agents_en_echec (hosted agents whose JOBS are dying, grouped by agent AND "
            "by reason — `days` (default 1), optional `org_id`; each row carries "
            "`travaux`, `tentatives`, `depuis`, `dernier`). ⚠️ This is the one nobody "
            "asks for: a triggered agent breaks SILENTLY — its queue empties because "
            "dead jobs stop waiting, its screen says `active`, and its last run can be "
            "green. Grouped by reason because three times the same fault is a bug to "
            "fix, while three different ones is a misconfigured agent — one count would "
            "read them alike. This is the lens a nightly report reads to say it in one "
            "line. / connecteurs_orphelins (no filter): the rows that NAME a connector "
            "absent from the served catalog — member installations, removals, "
            "activations, credentials, settings, live instances, org kits — per table "
            "and per name (`lignes`). Such a row resolves nothing: an installation that "
            "mounts no tool, a key nobody reads. Every table is listed, an empty one "
            "included, so a zero reads as checked; `total` = 0 is the healthy state. "
            "Read only: nothing is moved or deleted here. "
            "For raw signals use oto_admin_signal."),
        mcp="oto_admin_monitoring",
    ),
]

