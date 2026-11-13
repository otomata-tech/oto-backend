"""Observabilité au niveau ORG — l'étage manquant entre « moi » et « la plateforme ».

Il y avait deux sièges et pas de troisième : un membre voyait SON activité
(`/api/me/activity-summary`, `/api/me/calls`), un opérateur plateforme voyait TOUT
(`oto_admin_monitoring`, ADR 0047) — et le responsable d'une org, rien, hormis
l'export brut du journal d'audit (#67). Or c'est lui qui doit répondre à « qui dans
mon équipe s'en sert », « qu'est-ce qui casse chez nous », « qu'est-ce qui manque à
mes gens ». Ce module ouvre les mêmes lentilles, bornées à SON org.

Scope = **exact et unique** : `tool_calls.org_id` / `usage_signals.org_id`, l'org sous
laquelle l'appel a été émis (seam `current_org`), JAMAIS l'appartenance du membre —
un membre de N orgs n'apporte ici que ce qu'il a fait sous celle-ci. Même règle que
l'export d'audit, donc mêmes chiffres d'un écran à l'autre.

Deux lentilles plateforme ne descendent PAS : `rest` (télémétrie de surface `/api/*` —
santé d'infra, pas usage d'org) et `funnel` (comptes de toute la base). Le funnel a un
pendant org qui répond à la même question à l'échelle d'une équipe : `adoption`.

Surface : console MCP **`oto_org_monitoring(op=…, org_id=…)`** + une route REST par
lentille sous `/api/orgs/{id}/monitoring/*` (per-verbe, idiomatique dashboard — le
verbe en `op` reste la face MCP, cf. monitoring.py). Autz **`ORG_ADMIN_OF`** partout :
le grain nominatif (qui a appelé quoi) est une donnée de responsable, alignée sur
`org.audit_log.export` déjà gaté ainsi.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

from .. import db, deprecations
from . import audit_log, monitoring
from ._authz import COMMERCE_SERVICE, ORG_ADMIN_OF, ORG_MEMBER_OF
from ._lecture_bornee import bornee
from ._types import (cap_limit, AuthzDenied, Capability, DeclaredError, ResolvedCtx,
                     RestBinding)
from .registry import CAPABILITIES

_ID = {"id": "org_id"}


# ── sorties ─────────────────────────────────────────────────────────────────
# Deux invariants valent pour TOUTES les lentilles de ce module, et ils décident
# de ce qu'un chiffre veut dire :
#
# ① **Rétention en ligne `OTO_JOURNAL_RETENTION_DAYS` jours, 30 par défaut** (90
#    jusqu'au 22/10/2026) — portée par le timer `oto-journal-archive`, qui EXPORTE
#    chaque mois clos au froid S3, lisible à la demande, puis le supprime.
#    ⚠️ Jusqu'au 2026-08-28 la fenêtre effective était de ~30 jours et personne ne
#    l'avait décidé : le boot purgeait à 30 j sans archiver, donc plus court que la
#    politique écrite, qu'il vidait d'avance (ADR 0065 lot 0, oto-backend#426). Un
#    chiffre lu ici avant cette date porte donc au plus un mois d'histoire, quelle
#    que soit la fenêtre demandée. `days` accepte jusqu'à 365, mais au-delà de la
#    rétention il n'y a plus de lignes : une fenêtre large ne rend pas plus
#    d'histoire, elle rend le même mois avec un dénominateur trompeur.
# ② **Scope = ce qui a été ÉMIS SOUS cette org** (`tool_calls.org_id` / `usage_signals
#    .org_id`), jamais l'appartenance. Un membre de N orgs n'apporte ici que son
#    activité sous celle-ci — donc « inactif » veut dire « inactif ICI ».
#
# Les horodatages sortent en `"YYYY-MM-DD HH:MM:SS"` (sans `T`, sans offset).

class ToolStat(BaseModel):
    """Un outil sur la fenêtre. `avg_ms`/`p95_ms` sont `null` quand aucune durée n'a
    été enregistrée sur ces appels — pas `0`."""
    tool_name: str
    calls: int
    errors: int
    avg_ms: Optional[int] = None
    p95_ms: Optional[int] = None


class UserStat(BaseModel):
    sub: Optional[str] = None
    email: Optional[str] = None      # None si le sub n'a pas de ligne `users`
    name: Optional[str] = None
    calls: int
    errors: int


class DayStat(BaseModel):
    day: str                          # "YYYY-MM-DD"
    calls: int
    errors: int


class EmitterStat(BaseModel):
    client_name: Optional[str] = None   # None = ligne sans émetteur (antérieure à oto#187)
    calls: int


class OrgMonitoringSummary(BaseModel):
    """Agrégats d'activité de l'org sur `since_days`.

    ⚠️ **Les totaux et les ventilations n'ont pas le même dénominateur** :
    `total_calls`/`error_count`/`active_users` couvrent TOUT, alors que `by_tool` et
    `by_user` sont tronqués aux **100 premiers**. Sommer `by_tool[].calls` pour
    retrouver `total_calls` échoue dès qu'une org dépasse 100 outils distincts, et
    l'écart n'est signalé nulle part.

    ⚠️ **`by_day` n'a pas de ligne pour les jours à zéro** — ce sont des trous, pas des
    zéros : un graphe qui relie les points sans re-densifier la série ment sur la forme.

    `active_users` = subs distincts ayant appelé SOUS cette org dans la fenêtre — ce
    n'est ni le nombre de membres, ni le nombre de comptes."""
    since_days: int
    total_calls: int
    error_count: int
    active_users: int
    # oto#187 — la couverture de l'émetteur : sur `total_calls`, combien portent un
    # logiciel client NOMMÉ ; `by_emitter` les ventile (50 premiers, `client_name`
    # `None` = lignes sans émetteur, antérieures au lot). Déclaré : jamais opposable.
    emitter_named_calls: int = 0
    by_emitter: list[EmitterStat] = []
    by_tool: list[ToolStat]
    by_user: list[UserStat]
    by_day: list[DayStat]


class CallRow(BaseModel):
    """Une ligne du journal. Les noms `tool_name`/`called_at` sont des ALIAS de compat
    (`tool`/`created_at` en base) — la fiche `op=call`, elle, rend les noms bruts :
    les deux surfaces du même objet ne nomment pas ses champs pareil.

    La ligne ne porte pas les arguments : `arg_keys` en donne les CLÉS, triées (`[]` =
    l'appel n'en portait aucun), jamais une valeur — « cet appel portait-il un numéro
    d'entreprise ? » se répond ici ; le contenu, tronqué et masqué, est `call.args`
    sur la fiche (#634)."""
    id: int
    sub: Optional[str] = None
    email: Optional[str] = None
    name: Optional[str] = None
    # `tool`, `created_at` et `ok` sont NOT NULL en base : jamais absents (#437).
    tool_name: str
    called_at: str
    duration_ms: Optional[int] = None
    ok: bool
    error: Optional[str] = None
    session_id: Optional[str] = None
    run_id: Optional[str] = None
    org_id: Optional[int] = None
    # Id de l'event Sentry correspondant, quand l'erreur en a produit un — le pont
    # vers le traceback. None sur un appel réussi (et sur une erreur gérée).
    sentry_event_id: Optional[str] = None
    arg_keys: list[str] = []
    # FORME du résultat servi (#644) : `empty` | `non_empty` | `refused(<code>)` —
    # jamais le contenu. `None` = non mesurée (échec, historique, forme illisible).
    # Sépare « l'outil a rendu une liste vide » de « il a rendu un refus sous ok=true ».
    result_shape: Optional[str] = None
    # Nombre d'items TRAITÉS par cet appel (facturation du partenaire, 21/08) — `None` = non
    # tracé pour ce tool (l'écrasante majorité), à traiter comme 1 par un
    # consommateur, JAMAIS comme 0. Posé aujourd'hui par `linkedin_aiark_search`
    # (résultats rendus) et `fullenrich_enrich_linkedin` (contacts soumis) —
    # voir `tool_calls.quantity`'s DDL comment pour la liste vivante.
    quantity: Optional[int] = None
    # Les crédits que le FOURNISSEUR a déduits pour l'appel, dans son unité — à côté
    # de `quantity`, jamais dedans. `None` = non tracé : ni 1 ni 0 (cf. le commentaire
    # DDL de `tool_calls.provider_credits`).
    provider_credits: Optional[int] = None
    # Sous quelle clé l'appel est passé (`user|group|org|tenant|platform`).
    # `None` = aucun credential résolu, ou ligne antérieure à la colonne.
    key_mode: Optional[str] = None
    # L'ÉMETTEUR DÉCLARÉ (otomata-tech/oto#187) : le logiciel client que la session a
    # nommé à son `initialize` (`client_name`/`client_version`) et le mode de jeton
    # (`token_kind` : `user` | `delegation` ; `None` = session OAuth, ou ligne antérieure).
    # ⚠️ DÉCLARÉ par le client : lisible, jamais opposable — une surface (runner, CLI
    # d'agent, client web), pas la présence d'un humain. `None` partout = ligne
    # antérieure au lot.
    client_name: Optional[str] = None
    client_version: Optional[str] = None
    token_kind: Optional[str] = None


class OrgCalls(BaseModel):
    """Journal d'appels de l'org, plus récent d'abord — et ce que son scope laisse dehors.

    Scope = les appels RÉSOLUS sous cette org (`org_id` stampé à l'appel). Un appel d'un
    run de l'org résolu sous une autre org — l'axe `_org` absent le fait retomber sur
    l'org maison de l'appelant — n'y figure pas : `hors_scope` les compte sous les mêmes
    filtres (fenêtre = `days`, sinon la page, sinon 30 j), `hors_scope_hint` dit où les
    voir (`runs/{run_id}`). Un compte lu ici est « lignes + hors_scope », jamais les
    lignes seules (#630).

    ⚠️ `limit` est **silencieusement plafonné à 1000** côté store : demander 5000 rend
    1000 lignes sans le dire. Comme il n'y a ni total ni curseur, une liste de la
    taille du `limit` doit toujours être lue comme « probablement tronquée »."""
    calls: list[CallRow]
    scope: Optional[str] = None
    hors_scope: Optional[int] = None
    hors_scope_hint: Optional[str] = None


class CallDetail(BaseModel):
    """La ligne complète du journal, noms BRUTS (`tool`, `created_at` — pas les alias
    de la liste).

    ⚠️ **Pas de champ `args` ici** — décision d'Alexis du 15/09/2026 (oto-backend#563) :
    les arguments complets d'un appel (peuvent porter des PII — contenu de
    `data_write`, destinataire d'`email_send`) restent à la supervision PLATEFORME
    seule, jamais à l'org_admin de l'org émettrice. Le handler (`_call`) retire la clé
    du dict AVANT de le renvoyer — c'est LÀ qu'est la garde réelle, ce modèle ne fait
    que documenter fidèlement ce qui est servi."""
    id: int
    kind: Optional[str] = None
    server: Optional[str] = None
    sub: Optional[str] = None
    email: Optional[str] = None
    name: Optional[str] = None
    tool: Optional[str] = None
    ok: Optional[bool] = None
    error: Optional[str] = None
    duration_ms: Optional[int] = None
    created_at: Optional[str] = None
    session_id: Optional[str] = None
    run_id: Optional[str] = None
    org_id: Optional[int] = None
    org_name: Optional[str] = None
    client_id: Optional[str] = None
    sentry_event_id: Optional[str] = None
    # FORME du résultat servi (#644) — même vocabulaire que `CallRow.result_shape`.
    result_shape: Optional[str] = None
    # L'ÉMETTEUR DÉCLARÉ (otomata-tech/oto#187) : le logiciel client que la session a
    # nommé à son `initialize` (`client_name`/`client_version`) et le mode de jeton
    # (`token_kind` : `user` | `delegation` ; `None` = session OAuth, ou ligne antérieure).
    # ⚠️ DÉCLARÉ par le client : lisible, jamais opposable — une surface (runner, CLI
    # d'agent, client web), pas la présence d'un humain. `None` partout = ligne
    # antérieure au lot.
    client_name: Optional[str] = None
    client_version: Optional[str] = None
    token_kind: Optional[str] = None
    # Le jeton NOMMÉ employé (son id, jamais sa valeur) — `None` en session OAuth.
    token_id: Optional[int] = None


class OrgCall(BaseModel):
    """Fiche d'UN appel. ⚠️ `call.args` est **tronqué à l'écriture** (`truncated_args`) :
    ce n'est pas le payload intégral, et ça ne doit pas servir à rejouer un appel.

    L'id est un entier séquentiel donc devinable : un appel d'une AUTRE org rend le
    **même 404** qu'un id inexistant — l'absence de résultat ne prouve pas l'absence
    d'appel."""
    call: CallDetail


class ConnectorFailure(BaseModel):
    provider: str
    failures: int
    users_affected: int
    last_at: Optional[str] = None


class OrgConnectorHealth(BaseModel):
    """Échecs de **résolution de credential** par connecteur — « quel connecteur bloque
    mes membres ». Ce ne sont PAS des erreurs d'API tierce : le connecteur n'a même
    pas trouvé de clé à utiliser.

    ⚠️ `total_failures` est la somme de `by_provider`, lui-même **tronqué aux 100
    premiers providers** : sur une org qui dépasserait ce seuil, le total est
    sous-estimé, pas exact."""
    since_days: int
    total_failures: int
    by_provider: list[ConnectorFailure]


class AdoptionMember(BaseModel):
    """Un membre et son activité SOUS cette org.

    ⚠️ **`calls: 0` avec un `last_call_at` non nul ≠ « n'a jamais utilisé »** :
    `last_call_at` n'est pas borné par la fenêtre, il date le DERNIER appel connu. La
    combinaison décrit un **décrochage** — l'action de l'org_admin n'est pas la même
    que pour quelqu'un qui n'a jamais commencé (`last_call_at: null`).

    `connector_failures > 0` = le membre a essayé et rien ne résolvait ; c'est
    l'opposé de « n'a pas essayé », et ça se répare autrement."""
    sub: str
    email: Optional[str] = None
    name: Optional[str] = None
    org_role: Optional[str] = None
    calls: int
    errors: int
    last_call_at: Optional[str] = None
    connector_failures: int


class OrgAdoption(BaseModel):
    """Adoption membre par membre — la lentille qui part de `org_members`, pas des
    appels (sinon le membre à 0 appel, justement celui qu'on cherche, serait invisible).

    ⚠️ **`truncated: true` ne dégrade que la LISTE** : `members` est plafonné à 500,
    les compteurs (`total_members`, `active`, `never_active`, `blocked_by_connector`)
    portent sur toute la population. Ne jamais recompter depuis `members`.

    ⚠️ `never_active` veut dire « aucun appel **sous cette org** » : un membre très
    actif ailleurs y figure — c'est voulu (l'org ne voit que ce qui la concerne), mais
    ça ne se dit pas « il n'utilise pas oto »."""
    org_id: int
    window_days: int
    total_members: int
    active: int
    never_active: int
    blocked_by_connector: int
    truncated: bool
    members: list[AdoptionMember]


class RunRow(BaseModel):
    """Un déroulé. `slug` est un alias de compat = `guide` s'il y en a un, sinon
    `label` — pas un troisième identifiant.

    ⚠️ `guide` est servi aussi sous son nom d'hier, `doctrine`, jusqu'au 30/11/2026
    (#519) — cf. `docs/alias-deprecies.md`.

    ⚠️ `finished_at`/`outcome` à `null` = le déroulé n'a **pas été fermé** (`run_finish`
    jamais appelé, ou sa ligne purgée par la rétention). Ce n'est pas « en cours » au
    sens d'un processus vivant : personne ne le clôturera.

    ⚠️ `n_calls` compte les appels du `run_id` **toutes orgs confondues** — la seule
    valeur de cette lentille qui n'est pas scopée à l'org."""
    run_id: str
    slug: Optional[str] = None
    label: Optional[str] = None
    guide: Optional[str] = None
    doctrine: Optional[str] = None        # ALIAS déprécié (retrait 30/11/2026)
    sub: Optional[str] = None
    email: Optional[str] = None
    name: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    outcome: Optional[str] = None
    note: Optional[str] = Field(default=None, description=(
        "The `note` of the run's close (`run_finish`): for a hosted run that died, "
        "the reason the runner gave — the provider's refusal, the transport error. "
        "Null when the close carried none, or the run is not closed."))
    n_calls: int


class OrgRuns(BaseModel):
    """Déroulés récents. ⚠️ Le scope d'org est appliqué à la ligne `run_start` : un
    déroulé OUVERT sous une autre org mais poursuivi ici n'apparaît pas, même si ses
    appels, eux, sont dans le journal de l'org. `limit` est plafonné à 500."""
    runs: list[RunRow]


class RunCall(BaseModel):
    """Un appel de la timeline d'un déroulé.

    ⚠️ **Même ligne de journal que `CallDetail`, donc même retrait** : `args` n'est PAS
    un champ de ce modèle (oto-backend#563, décision d'Alexis du 15/09/2026) — la
    timeline et la fiche d'un appel lisent la même colonne, le handler (`_run`) retire
    `args` du dict pour la même raison que `_call`.
    """
    # `id`, `created_at`, `tool` et `ok` sont NOT NULL dans `tool_calls` et lus tels quels
    # par `get_run` : jamais absents ni nuls, comme sur `CallRow` (#437).
    id: int = Field(description=(
        "L'identifiant de CET appel — celui que prend `GET …/monitoring/calls/"
        "{call_id}` et `op=call`. ⚠️ Sans lui, une ligne de la timeline était une "
        "impasse : on voyait qu'un appel avait eu lieu sans pouvoir l'ouvrir. C'est "
        "le MÊME identifiant que `CallDetail.id` — la timeline et la fiche parlent de "
        "la même ligne de journal, elles la nomment donc pareil."))
    created_at: str
    tool: str
    ok: bool
    error: Optional[str] = None
    duration_ms: Optional[int] = None


class RunContentArchived(BaseModel):
    """Le corps d'un run parti à l'archive froide (#665, option B du 23/09/2026).

    Présent ⟹ la timeline ne porte plus que ce que l'archive a gardé en base (les
    bornes `run_start`/`run_finish`, et les appels des mois non archivés) : la page
    affiche `message` à la place du contenu, jamais une timeline réduite sans un mot."""
    archived_at: str                 # date de l'archivage (la plus récente si plusieurs mois)
    months: list[str]                # mois 'YYYY-MM' du run partis au froid
    message: str                     # la phrase à afficher, telle quelle


class OrgRun(BaseModel):
    """Timeline d'un déroulé, bornée aux appels émis SOUS cette org — un run à cheval
    sur deux orgs n'en montre donc que la tranche locale, sans le signaler. Un `run_id`
    deviné depuis une autre org rend une timeline vide, traduite en 404.
    `content_archived` : présent quand le corps du run est parti à l'archive (#665)."""
    run_id: str
    calls: list[RunCall]
    content_archived: Optional[RunContentArchived] = None


class GapRow(BaseModel):
    """Un manque signalé par un membre (`feedback(signal='gap')`). `intent` est le texte
    LIBRE saisi par l'agent — l'axe de regroupement, donc deux formulations du même
    besoin font deux lignes."""
    intent: Optional[str] = None
    kind: Optional[str] = None
    n: int
    last_at: Optional[str] = None
    # Emails distincts des rapporteurs, repli sur le sub si le compte est inconnu :
    # la liste MÉLANGE donc deux formats d'identifiant.
    users: list[str] = []


class OrgGaps(BaseModel):
    """Ce qui manque à TES membres. ⚠️ Une liste vide ne veut pas dire « rien ne
    manque » : elle veut dire que personne n'a émis de signal — c'est une mesure de
    la remontée, pas du besoin."""
    gaps: list[GapRow]


class ToolFeedbackRow(BaseModel):
    tool: Optional[str] = None
    kind: Optional[str] = None
    n: int
    last_at: Optional[str] = None
    users: list[str] = []


class OrgToolQuality(BaseModel):
    """Retours d'outil de TES membres, groupés par (outil, `kind`). ⚠️ `kind` porte
    aussi bien un compliment qu'un défaut : `n` est un volume de SIGNAL, pas un compte
    de problèmes — trier par `n` sans lire `kind` classe en tête un outil très aimé."""
    tools: list[ToolFeedbackRow]


# ── entrées (une par lentille : `org_id` porté par le path REST) ─────────────

class OrgSummaryInput(BaseModel):
    org_id: int
    days: int = 7
    sub: Optional[str] = None      # restreindre à UN membre (email ou sub)


class OrgWindowInput(BaseModel):
    org_id: int
    days: int = 7


class OrgDaysInput(BaseModel):
    org_id: int
    days: int = 30


class OrgCallsInput(BaseModel):
    org_id: int
    limit: int = 200
    sub: Optional[str] = None            # email ou sub
    tool: Optional[str] = None
    errors: bool = False
    days: Optional[int] = None
    run_id: Optional[str] = None
    session_id: Optional[str] = None
    min_duration_ms: Optional[int] = None
    error_contains: Optional[str] = None

    @field_validator("limit")
    @classmethod
    def _cap_limit(cls, v):
        return cap_limit(v, 200)


class OrgCallInput(BaseModel):
    org_id: int
    call_id: int


class OrgRunsInput(BaseModel):
    org_id: int
    limit: int = 100

    @field_validator("limit")
    @classmethod
    def _cap_limit(cls, v):
        return cap_limit(v, 100)


class OrgRunInput(BaseModel):
    org_id: int
    run_id: str


# ── handlers ────────────────────────────────────────────────────────────────

def _summary(ctx: ResolvedCtx, inp: OrgSummaryInput) -> dict:
    return db.tool_call_stats(since_days=inp.days, org_id=inp.org_id,
                              sub=monitoring._resolve_sub(inp.sub))


def _calls(ctx: ResolvedCtx, inp: OrgCallsInput) -> dict:
    # La page ET son plancher (#630) — le même geste que la console plateforme.
    return monitoring.calls_with_scope(monitoring.CallsInput(
        limit=inp.limit, sub=inp.sub, tool=inp.tool,
        errors=inp.errors, days=inp.days, org_id=inp.org_id,
        run_id=inp.run_id, session_id=inp.session_id,
        min_duration_ms=inp.min_duration_ms, error_contains=inp.error_contains))


def _call(ctx: ResolvedCtx, inp: OrgCallInput) -> dict:
    """Fiche d'un appel — l'id est un entier séquentiel, donc devinable : la garde
    d'org N'EST PAS une formalité. Un appel d'une autre org rend le MÊME 404 qu'un id
    inexistant (ne pas confirmer son existence).

    ⚠️ `args` est RETIRÉ ici, dans le dict — décision d'Alexis du 15/09/2026
    (oto-backend#563) : les arguments complets d'un appel (peuvent porter des PII —
    contenu de `data_write`, destinataire d'`email_send`) restent à la supervision
    PLATEFORME seule (`monitoring.py::_call`, chemin séparé, inchangé), pas à
    l'org_admin de l'org émettrice. Le modèle `Output=` ne filtre RIEN à l'exécution
    (`api/base.py::_json` sert le dict tel quel, `Output=` n'est que la doc OpenAPI) —
    retirer le champ de `CallDetail` seul aurait laissé la fuite réelle intacte."""
    row = db.get_tool_call(inp.call_id)
    if row is None or row.get("org_id") != inp.org_id:
        raise AuthzDenied(404, "unknown_call",
                          f"Aucun appel id={inp.call_id} dans cette org.")
    return {"call": {k: v for k, v in row.items() if k != "args"}}


def _connectors(ctx: ResolvedCtx, inp: OrgWindowInput) -> dict:
    return db.connector_failure_stats(since_days=inp.days, org_id=inp.org_id)


def _adoption(ctx: ResolvedCtx, inp: OrgDaysInput) -> dict:
    return db.org_adoption(inp.org_id, active_window_days=inp.days)


def _runs(ctx: ResolvedCtx, inp: OrgRunsInput) -> dict:
    return {"runs": deprecations.lignes_avec_les_deux_noms(
        db.list_runs(inp.limit, org_id=inp.org_id))}


def _run(ctx: ResolvedCtx, inp: OrgRunInput) -> dict:
    """Timeline d'un déroulé. Un run_id d'une autre org rend une timeline vide côté
    db → 404 ici (même raisonnement que `_call`, sur une clé opaque cette fois).

    ⚠️ `args` retiré de CHAQUE appel de la timeline, même raison que `_call`
    (oto-backend#563) — même ligne de journal, mêmes arguments, même restriction."""
    calls = db.get_run(inp.run_id, org_id=inp.org_id)
    if not calls:
        raise AuthzDenied(404, "unknown_run",
                          f"Aucun déroulé `{inp.run_id}` dans cette org.")
    calls = [{k: v for k, v in c.items() if k != "args"} for c in calls]
    return {"run_id": inp.run_id, "calls": calls,
            "content_archived": db.run_content_archived(inp.run_id, org_id=inp.org_id)}


def _gaps(ctx: ResolvedCtx, inp: OrgDaysInput) -> dict:
    return {"gaps": db.aggregate_gaps(inp.days, org_id=inp.org_id)}


class OrgSignalRow(BaseModel):
    """Un signal tel qu'un responsable d'ORG le voit. `resolved_by` est absent : qui a
    tranché chez nous est notre conduite interne, pas la sienne. La NOTE, elle, est là
    — c'est la réponse qu'on lui doit."""
    id: int
    created_at: Optional[str] = None
    sub: Optional[str] = None
    email: Optional[str] = None
    name: Optional[str] = None
    org_id: Optional[int] = None
    signal: Optional[str] = None       # tool_feedback | gap
    kind: Optional[str] = None
    target: Optional[str] = None
    body: Optional[str] = None         # LA prose — ce que les compteurs ne disent pas
    session_id: Optional[str] = None
    source: Optional[str] = None
    # Domaine fermé par le CHECK `usage_signals_status` (#437).
    status: Optional[Literal["open", "acknowledged", "declined", "resolved"]] = None
    resolved_at: Optional[str] = None
    resolution: Optional[str] = None
    notified_at: Optional[str] = None


class OrgSignals(BaseModel):
    org_id: int
    signals: list[OrgSignalRow]
    count: int


def _signals(ctx: ResolvedCtx, inp: "OrgMonitoringInput") -> dict:
    """Les signaux BRUTS de cette org — le CORPS, pas seulement le compte.

    **C'est ce qui manquait, et le manque a coûté cinq jours à cinq clients.** Les
    lentilles `gaps` et `tool_quality` rendent l'intitulé et le nombre ; la cause est
    dans la prose. « le projet de destination a été archivé le 21/08 » ne se déduit
    d'aucun compteur — un responsable voyait donc « 8 manques » sans jamais pouvoir
    savoir lesquels, et l'écran d'org le disait explicitement : « pas de drill-down,
    le corps est servi par une capacité PLATEFORME ».

    Le corps est de la prose libre écrite par un agent SOUS un compte de cette org.
    Le rendre à l'`org_admin` suit exactement la règle du journal d'audit : ce qui a
    été émis sous l'org appartient à l'org. Le scope est `usage_signals.org_id`,
    JAMAIS l'appartenance du rapporteur — un prestataire qui travaille pour trois
    clients ne verse pas ses retours dans les trois.

    Ce que ça ne rend pas : l'arbitrage plateforme (`resolved_by`), qui est notre
    conduite interne, pas la leur."""
    lignes = db.list_usage_signals(
        signal=inp.signal, target=inp.tool, limit=inp.limit or 200,
        status=inp.status, org_id=inp.org_id)
    for s in lignes:
        s.pop("resolved_by", None)
    return {"org_id": inp.org_id, "signals": lignes, "count": len(lignes)}


def _tool_quality(ctx: ResolvedCtx, inp: OrgDaysInput) -> dict:
    return {"tools": db.aggregate_tool_feedback(inp.days, org_id=inp.org_id)}


# ── console MCP consolidée `oto_org_monitoring(op=…)` (pattern ADR 0047) ─────

class OrgMonitoringInput(BaseModel):
    op: Literal["summary", "calls", "call", "connectors", "adoption",
                "runs", "run", "gaps", "tool_quality", "signals", "export",
                "agents_en_echec"]
    org_id: int
    days: Optional[int] = None            # fenêtre (défaut 7 ; adoption/gaps/tool_quality : 30)
    limit: Optional[int] = None           # calls (200) / runs (100) / export (1000)
    sub: Optional[str] = None             # summary/calls : filtre membre (email ou sub)
    tool: Optional[str] = None            # calls : filtre outil exact
    errors: bool = False                  # calls : erreurs seulement
    run_id: Optional[str] = None          # run (requis) / calls (filtre)
    session_id: Optional[str] = None      # calls : tous les appels d'une conversation
    min_duration_ms: Optional[int] = None  # calls : appels lents
    error_contains: Optional[str] = None  # calls : recherche dans le message d'erreur
    call_id: Optional[int] = None         # call (requis)
    signal: Optional[str] = None          # signals : tool_feedback | gap
    status: Optional[str] = None          # signals : open|acknowledged|declined|resolved|pending
    since: Optional[str] = None           # export : borne basse ISO
    until: Optional[str] = None           # export : borne haute ISO

    # Console op-aware : plafond du plus large de ses ops (`export`, 1000) — borner
    # plus bas écrêterait un export légitime. Écrête au lieu de refuser (#300).
    @field_validator("limit")
    @classmethod
    def _cap_limit(cls, v):
        return cap_limit(v, 1000) if v is not None else None


def _need(val, code: str, msg: str):
    if val is None or (isinstance(val, str) and not val.strip()):
        raise AuthzDenied(400, code, msg)
    return val


def _console(ctx: ResolvedCtx, inp: OrgMonitoringInput) -> dict:
    oid = inp.org_id
    if inp.op == "summary":
        return _summary(ctx, OrgSummaryInput(org_id=oid, days=inp.days or 7, sub=inp.sub))
    if inp.op == "calls":
        return _calls(ctx, OrgCallsInput(
            org_id=oid, limit=inp.limit or 200, sub=inp.sub, tool=inp.tool,
            errors=inp.errors, days=inp.days, run_id=inp.run_id,
            session_id=inp.session_id, min_duration_ms=inp.min_duration_ms,
            error_contains=inp.error_contains))
    if inp.op == "call":
        return _call(ctx, OrgCallInput(org_id=oid, call_id=_need(
            inp.call_id, "missing_call_id", "`call_id` requis pour call.")))
    if inp.op == "connectors":
        return _connectors(ctx, OrgWindowInput(org_id=oid, days=inp.days or 7))
    if inp.op == "adoption":
        return _adoption(ctx, OrgDaysInput(org_id=oid, days=inp.days or 30))
    if inp.op == "runs":
        return _runs(ctx, OrgRunsInput(org_id=oid, limit=inp.limit or 100))
    if inp.op == "run":
        return _run(ctx, OrgRunInput(org_id=oid, run_id=_need(
            inp.run_id, "missing_run_id", "`run_id` requis pour run.")))
    if inp.op == "agents_en_echec":
        # 1 jour : une alerte, pas une histoire — cf. la lentille plateforme.
        return {"agents": db.agents_en_echec(inp.days or 1, org_id=oid)}
    if inp.op == "gaps":
        return _gaps(ctx, OrgDaysInput(org_id=oid, days=inp.days or 30))
    if inp.op == "tool_quality":
        return _tool_quality(ctx, OrgDaysInput(org_id=oid, days=inp.days or 30))
    if inp.op == "signals":
        # Passe l'Input consolidé tel quel : cette lentille lit `signal`/`status`,
        # deux champs qui n'existent sur aucun des Input par-op. En fabriquer un
        # quatrième pour deux champs ajouterait une forme à garder d'accord.
        return _signals(ctx, inp)
    # export : le journal d'audit org existe déjà (#67) — même autz, même scope,
    # on le REBRANCHE plutôt que d'en écrire un second.
    return audit_log._export(ctx, audit_log.AuditExportInput(
        org_id=oid, since=inp.since, until=inp.until, limit=inp.limit or 1000))


# ── relevé de facturation, lentille MEMBRE ──────────────────────────────────
#
# Pourquoi une capacité SÉPARÉE et non `org.monitoring.calls` ouverte aux membres :
# celle-ci rend `sub`, `email`, `name` et le texte d'`error` de chaque appel.
# L'ouvrir aux membres donnerait à n'importe qui le moyen d'énumérer l'activité et
# les échecs de tous ses collègues — un changement de confidentialité que personne
# n'a demandé, et qui arriverait ici en effet de bord d'une page de facturation.
#
# Celle-ci ne rend QUE ce qu'un métrage doit sommer : l'id de l'appel (clé de
# déduplication), l'outil, le nombre d'items, le mode de clé, la date. Aucune
# identité, aucun message d'erreur. Le membre voit ce que SON ORG consomme, pas
# qui a fait quoi.

class BillableCallRow(BaseModel):
    """Un appel tel qu'un consommateur de FACTURATION en a besoin — et rien de plus."""
    call_id: int
    # L'identifiant d'appel stable (`tool_calls.call_uid`) : celui que portent les
    # mouvements de crédits d'un appel (`call_uid`, et `call_id` de leur `meta`). C'est
    # par lui qu'un grand livre apparie un mouvement à l'appel qui l'a consommé. `None`
    # = ligne antérieure à la colonne.
    call_uid: Optional[str] = None
    tool: Optional[str] = None
    created_at: Optional[str] = None
    # Items réellement traités. `None` = ce tool ne trace pas de compte, à lire
    # comme 1 — JAMAIS comme 0 (cf. le commentaire DDL de `tool_calls.quantity`).
    quantity: Optional[int] = None
    # Les crédits que le FOURNISSEUR a déduits pour l'appel, dans son unité (crédits
    # Forager…) : ce que le fournisseur a pris, là où `quantity` dit ce qui a été traité.
    # `None` = non tracé par cet outil — ni 1 ni 0 (cf. le commentaire DDL de
    # `tool_calls.provider_credits`).
    provider_credits: Optional[int] = None
    # `user|group|org|tenant|platform`. `None` = aucun credential résolu ou ligne
    # antérieure à la colonne : non attribuable, donc à NE PAS facturer.
    key_mode: Optional[str] = None
    # L'identité du JOB fournisseur que l'appel relève, quand l'outil en porte un
    # (`fullenrich_result` → son `enrichment_id`). Elle ne sert qu'à une chose : un
    # job relevé plusieurs fois trace plusieurs fois le même coût, et c'est au
    # consommateur de ne le compter qu'une fois. Lue dans les args journalisés pour
    # une liste FERMÉE de noms (`db.BILLABLE_JOB_ARGS`) — jamais un autre argument :
    # ceux d'un enrichissement portent des personnes. `None` = l'outil n'en a pas.
    job_id: Optional[str] = None
    # Ce qu'un relevé de job terminé a TROUVÉ, en CONTACTS par sorte :
    # `{"work_emails": n, "personal_emails": n, "phones": n}` (un contact à deux
    # valeurs d'une sorte compte une fois). Lu dans les args journalisés pour la
    # liste fermée `db.usage.BILLABLE_FOUND_ARGS`. `None` = rien de tel n'a été tracé
    # (autre outil, job non terminé, ligne antérieure).
    found: Optional[dict[str, int]] = None
    # Le run (`run_start`) dont l'appel relève, quand il en porte un. Un agent hébergé
    # le pose sur CHAQUE appel (`_run_id`, oto-runner) — c'est ce qui permet de dire ce
    # qu'UN run a consommé. Un identifiant de travail, pas une personne. `None` =
    # appel hors run.
    run_id: Optional[str] = None


class OrgBillableCalls(BaseModel):
    """Une PAGE d'une fenêtre CLOSE — même contrat que l'export d'audit (#770).

    `total` = la population de la fenêtre `[since, until_effectif]`, indépendante
    de la page : c'est ce qui permet au consommateur de VÉRIFIER qu'il a tout lu
    (somme des pages == total) au lieu de le supposer. `next` = la position de la
    ligne suivante quand il en reste, à renvoyer telle quelle en
    `before_at`/`before_id` avec le MÊME `until_effectif` — sans ce gel, un journal
    alimenté en continu et trié récent d'abord servirait deux vérités successives.
    ⚠️ Ne PAS rebâtir cette lentille sur `list_tool_calls` : il plafonne à 1000
    en silence et sans curseur, et une page tronquée y a l'air complète."""
    calls: list[BillableCallRow]
    total: int
    # La borne basse APPLIQUÉE : celle demandée, ou `until_effectif` moins la fenêtre
    # maximale quand la requête n'en donne pas (#1145).
    since_effectif: Optional[str] = None
    until_effectif: str
    next_at: Optional[str] = None
    next_id: Optional[int] = None
    # Les `run_id` DEMANDÉS qui ne désignent aucun run de CETTE org — ni dans `runs`,
    # ni dans son journal. Sans lui, un run inconnu (faute de frappe, id tronqué) rend
    # `total: 0`, indiscernable d'un run réel qui n'a rien consommé : un relevé de
    # facturation qui se trompe EN SILENCE. Le run d'une AUTRE org y figure aussi, au
    # même titre qu'un id inexistant — la lentille ne dit pas qu'il existe ailleurs.
    # `[]` sans filtre par run.
    unknown_run_ids: list[str] = Field(default_factory=list)


# La même ligne pour la SEULE face de service du commerce : plus la personne qui a fait
# l'appel. Le commerce tient les limites de dépense du jour par personne et par équipe
# (`docs/limites-de-depense.md`) ; la lentille membre, elle, reste sans identité.
class ServiceBillableCallRow(BillableCallRow):
    # `tool_calls.sub` : la personne au nom de qui l'appel a tourné — l'auteur d'un
    # agent hébergé pour ses appels. `None` = ligne sans identité.
    sub: Optional[str] = None


class ServiceBillableCalls(OrgBillableCalls):
    calls: list[ServiceBillableCallRow]


# Une page de runs côté app en montre 50 ; la borne laisse de la marge sans ouvrir
# une lecture de tout le journal par une liste de runs interminable.
MAX_BILLABLE_RUNS = 100


class OrgBillableCallsInput(BaseModel):
    org_id: int
    # Le relevé se lit outil par outil. Le volume facturable d'une org est petit,
    # son volume total non — l'outil est ce qui borne la fenêtre.
    tool: Optional[str] = None
    # …ou run par run : ce qu'UN run a consommé, tous outils confondus. Un run est
    # borné par nature (quelques dizaines d'appels), il borne la fenêtre aussi bien.
    # Les deux se combinent. Toujours sous `org_id` : un membre ne lit que les appels
    # émis sous SON org, quel que soit le run qu'il nomme.
    # PLUSIEURS runs en une lecture (`?run_id=a&run_id=b`, ou `a,b`) : la liste des
    # runs d'un agent en affiche 50, et 50 requêtes par rafraîchissement n'en sont
    # pas une. `| str` = la forme réelle d'une query string (cf. `ProjectReadInput`).
    run_id: Optional[list[str] | str] = None
    since: Optional[str] = None
    until: Optional[str] = None
    limit: Optional[int] = None
    before_at: Optional[str] = None
    before_id: Optional[int] = None

    @field_validator("run_id", mode="after")
    @classmethod
    def _runs_en_liste(cls, v):
        return _liste_de_query(v)


def _liste_de_query(v) -> Optional[list[str]]:
    """Une liste de query string, dédoublonnée dans l'ordre : `?x=a&x=b`, `?x=a,b`, ou
    les deux mêlés. Chaque élément se redécoupe sur la virgule, liste comprise : les
    deux formes se MÉLANGENT dans une vraie URL (`?run_id=a,b&run_id=c`), et un élément
    `"a,b"` laissé entier ne désignerait rien — `total: 0` en silence.
    NORMALISE seulement : les refus (liste trop longue…) se lèvent dans le handler.
    Levés ici, ils deviendraient une `ValidationError`, que l'adaptateur REST rend en
    `400 invalid_input` NU — sans la phrase qui dit quoi corriger."""
    if v is None:
        return None
    brut = v if isinstance(v, list) else [v]
    elements = list(dict.fromkeys(
        m for x in brut for m in (p.strip() for p in str(x).split(",")) if m))
    return elements or None


# Les refus de la FENÊTRE et de la PAGE d'un relevé (#1145), communs aux deux
# lentilles : une fenêtre rognée ou une page tronquée en silence rendraient un total
# qui n'est pas celui demandé.
_REFUS_FENETRE = DeclaredError(
    400, "window_too_large",
    f"fenêtre `since`..`until` de plus de {db.RELEVE_FENETRE_MAX_JOURS} jours")
_REFUS_LIMITE = DeclaredError(
    400, "limit_too_large", f"`limit` hors de 1..{db.RELEVE_LIMITE_MAX}")


def _refus_de_fenetre(e: "db.FenetreDeReleveRefusee") -> AuthzDenied:
    return AuthzDenied(400, "window_too_large", str(e))


def _billable_calls(ctx: ResolvedCtx, inp: OrgBillableCallsInput) -> dict:
    return _releve(inp, avec_personne=False)


def _billable_calls_service(ctx: ResolvedCtx, inp: OrgBillableCallsInput) -> dict:
    return _releve(inp, avec_personne=True)


def _releve(inp: OrgBillableCallsInput, *, avec_personne: bool) -> dict:
    # Refus NOMMÉS (`AuthzDenied` : message rendu tel quel par les deux faces), même
    # code que l'adaptateur pour une entrée invalide — le contrat ne change pas, le
    # client apprend enfin pourquoi.
    if not inp.tool and not inp.run_id:
        # Ni l'un ni l'autre = tout le journal de l'org : ce que la lentille refuse.
        raise AuthzDenied(400, "invalid_input",
                          "`tool` ou `run_id` est requis : le relevé se lit par outil ou "
                          "par run, jamais sur tout le journal.")
    if inp.run_id and len(inp.run_id) > MAX_BILLABLE_RUNS:
        raise AuthzDenied(400, "invalid_input",
                          f"au plus {MAX_BILLABLE_RUNS} runs par lecture "
                          f"({len(inp.run_id)} demandés).")
    limit = 1000 if inp.limit is None else inp.limit
    if not 1 <= limit <= db.RELEVE_LIMITE_MAX:
        raise AuthzDenied(400, "limit_too_large",
                          f"`limit` doit valoir de 1 à {db.RELEVE_LIMITE_MAX} ({limit} "
                          "demandé) : lire la suite par le curseur `next_at`/`next_id`.")
    before = ((inp.before_at, inp.before_id)
              if inp.before_at and inp.before_id is not None else None)
    try:
        page = db.list_billable_calls_for_org(
            inp.org_id, inp.tool, run_ids=inp.run_id, since=inp.since, until=inp.until,
            limit=limit, before=before, avec_personne=avec_personne)
    except db.FenetreDeReleveRefusee as e:
        raise _refus_de_fenetre(e) from e
    nxt = page["next"]
    return {
        # Projection EXPLICITE, pas un `**row` : la lentille reste étroite même
        # si la requête gagne des colonnes demain.
        "calls": [{"call_id": r["id"], "call_uid": r.get("call_uid"), "tool": r["tool"],
                   "created_at": r["created_at"],
                   "quantity": r.get("quantity"),
                   "provider_credits": r.get("provider_credits"),
                   "key_mode": r.get("key_mode"),
                   "job_id": r.get("job_id"), "found": r.get("found"),
                   "run_id": r.get("run_id"),
                   **({"sub": r.get("sub")} if avec_personne else {})}
                  for r in page["calls"]],
        "total": page["total"],
        "since_effectif": page["since_effectif"],
        "until_effectif": page["until_effectif"],
        "next_at": nxt[0] if nxt else None,
        "next_id": nxt[1] if nxt else None,
        "unknown_run_ids": page["unknown_run_ids"],
    }


# ── relevé AGRÉGÉ par outil, lentille MEMBRE (#1145) ────────────────────────
#
# Ce que `org.usage.calls` rend appel par appel, sommé par outil et par mode de clé,
# en UNE lecture : le consommateur qui relisait chaque outil à chaque rafraîchissement
# (une requête par outil, toutes dans la même seconde) lit ici la fenêtre entière. Même
# étroitesse, mêmes contrôles d'accès, mêmes appels comptés : la somme des `calls` d'un
# outil égale le `total` de sa lentille sur la même fenêtre. Le détail d'un appel (job,
# trouvé, run) reste à `org.usage.calls`.

#: Le plus d'outils nommés en une lecture — même ordre de grandeur que les runs.
MAX_BILLABLE_TOOLS = 100


class BillableToolRow(BaseModel):
    """Un outil × un mode de clé, sur la fenêtre."""
    tool: str
    # `user|group|org|tenant|platform` ; `None` = non attribuable, à NE PAS facturer.
    key_mode: Optional[str] = None
    # Appels réussis.
    calls: int
    # Somme des quantités, un appel sans compte valant 1 — jamais 0.
    quantity: int
    # Somme des crédits déduits par le fournisseur, dans son unité : un appel non tracé
    # n'y compte pour rien (ni 1 ni 0). `None` = aucun appel du groupe ne l'a tracée.
    provider_credits: Optional[int] = None
    # Jobs fournisseur DISTINCTS relevés (un job relevé deux fois compte une fois) ;
    # 0 pour un outil sans job.
    jobs: int


class OrgBillableTools(BaseModel):
    """Le relevé d'une fenêtre CLOSE `[since_effectif, until_effectif]`, par outil puis
    par mode de clé. Une fenêtre sans `since` reçoit la fenêtre maximale."""
    since_effectif: str
    until_effectif: str
    tools: list[BillableToolRow]


class OrgBillableToolsInput(BaseModel):
    org_id: int
    # Les outils à relever (`?tool=a&tool=b`, ou `a,b`) ; absent = tous ceux de l'org.
    tool: Optional[list[str] | str] = None
    since: Optional[str] = None
    until: Optional[str] = None

    @field_validator("tool", mode="after")
    @classmethod
    def _outils_en_liste(cls, v):
        return _liste_de_query(v)


def _billable_tools(ctx: ResolvedCtx, inp: OrgBillableToolsInput) -> dict:
    if inp.tool and len(inp.tool) > MAX_BILLABLE_TOOLS:
        raise AuthzDenied(400, "invalid_input",
                          f"au plus {MAX_BILLABLE_TOOLS} outils par lecture "
                          f"({len(inp.tool)} demandés).")
    try:
        return db.billable_usage_by_tool_for_org(
            inp.org_id, tools=inp.tool, since=inp.since, until=inp.until)
    except db.FenetreDeReleveRefusee as e:
        raise _refus_de_fenetre(e) from e


# ── connexions de messagerie d'une org, lentille MEMBRE ─────────────────────
#
# Les comptes de messagerie hébergés (Unipile) d'UNE org et leurs dates. Aucune règle
# de facturation ici — ni prix, ni tri « facturable » : `platform_seat` dit sur quelle
# clé vit chaque connexion, et le relevé qui la lit (celui d'un tenant) décide de ce
# qu'il compte. Même règle d'étroitesse que `org.usage.calls` : ni `sub`, ni email, ni
# `account_id` Unipile — une clé opaque suffit à dédupliquer, et un membre voit ce que
# SON ORG a connecté, pas qui a connecté quoi.

class OrgConnectionRow(BaseModel):
    """Une connexion (un canal d'un membre dans cette org)."""
    # Stable d'une reconnexion à l'autre, ne nomme personne (`db.unipile_connection_key`).
    connection_key: str
    provider: str
    # True = un siège de la clé PLATEFORME ; False = une clé propre (BYO).
    platform_seat: bool
    connected_at: str
    # Posée = soft-déconnectée. Rendue seulement si elle est postérieure à `since`.
    disconnected_at: Optional[str] = None


class OrgConnections(BaseModel):
    """⚠️ `connected_at` repart à zéro à chaque reconnexion et aucun historique n'est
    gardé : le consommateur retient ce qu'il a déjà compté."""
    connections: list[OrgConnectionRow]


class OrgConnectionsInput(BaseModel):
    org_id: int
    # Borne basse ISO 8601 : rend aussi les connexions déconnectées depuis. Absente =
    # les vivantes seules.
    since: Optional[str] = None

    @field_validator("since")
    @classmethod
    def _since_iso(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        from datetime import datetime
        try:
            datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("`since` doit être une date ISO 8601") from None
        return v


def _connections(ctx: ResolvedCtx, inp: OrgConnectionsInput) -> dict:
    rows = db.list_org_unipile_connections(inp.org_id, since=inp.since)
    return {"connections": [
        # Projection EXPLICITE : la lentille reste étroite même si la requête s'élargit.
        {"connection_key": r["connection_key"], "provider": r["provider"],
         "platform_seat": bool(r["platform_seat"]),
         "connected_at": r["connected_at"], "disconnected_at": r.get("disconnected_at")}
        for r in rows]}


# ── jetons déclarés par les travaux d'une org, lentille ADMIN ───────────────
#
# Combien de jetons les agents hébergés ont consommé, jour par jour : toutes les voies
# de paiement — la clé de l'org, la clé de la plateforme ET l'abonnement Claude —
# parce que les jetons d'un abonnement ne coûtent aucun crédit mais restent de la
# consommation. Ce que le grand livre de crédits ne voit pas. Aucun prix ici : des
# jetons, comme le worker les a déclarés (`runner_jobs.result`).
#
# Admin seulement, comme la page Usage qui la lit : c'est la consommation de toute
# l'org, agents privés compris (le consommateur nomme ce qu'il connaît, le reste reste
# un identifiant).

class OrgTokenRow(BaseModel):
    """Une case : un jour UTC × un agent × un payeur × un modèle."""
    day: str
    # None = travail lancé à la main (pas de déclencheur).
    trigger_id: Optional[int] = None
    # None = worker d'avant le champ.
    paid_by: Optional[Literal["cle_org", "cle_plateforme", "abonnement"]] = None
    model: Optional[str] = None
    jobs: int
    # Les nombres que les travaux ont DÉCLARÉS. Un poste manquant n'est pas compté
    # comme zéro dans `unmeasured` : voir ci-dessous.
    input: int
    output: int
    cache_read: int
    cache_write: int
    # Travaux de la case dont l'entrée ou la sortie n'est pas déclarée : leurs jetons
    # connus sont dans les sommes, mais le total est un minorant.
    unmeasured: int
    # Le temps que les travaux de la case ont tourné, en secondes : de l'ouverture
    # du run d'un `start` à sa conclusion, 3 h au plus par travail. Un `continue`
    # (il reprend un run ancien) n'est pas chronométré : il compte dans `untimed`,
    # et `seconds` est alors un minorant.
    seconds: int = 0
    untimed: int = 0


class OrgTokens(BaseModel):
    days: int
    rows: list[OrgTokenRow]


class OrgTokensInput(BaseModel):
    org_id: int
    days: int = Field(30, ge=1, le=90)


def _tokens(ctx: ResolvedCtx, inp: OrgTokensInput) -> dict:
    rows = db.jetons_des_travaux(inp.org_id, inp.days)
    return {"days": inp.days, "rows": [
        {"day": r["jour"], "trigger_id": r["trigger_id"], "paid_by": r["paye_par"],
         "model": r["modele"], "jobs": r["travaux"], "input": r["entree"],
         "output": r["sortie"], "cache_read": r["lecture_cache"],
         "cache_write": r["ecriture_cache"], "unmeasured": r["non_mesures"],
         "seconds": r["secondes"], "untimed": r["non_chronometres"]}
        for r in rows]}


_MEMBER_OF = ORG_MEMBER_OF("org_id")

class ViewAsWriteRow(BaseModel):
    """Une écriture faite AU NOM d'un membre par un opérateur plateforme : « saisi
    par `operator_email` en tant que `target_email` ». Jamais d'argument ni de secret."""
    id: int
    called_at: Optional[str] = None
    route: Optional[str] = None
    ok: Optional[bool] = None
    error: Optional[str] = None
    operator_sub: Optional[str] = None
    operator_email: Optional[str] = None
    target_sub: Optional[str] = None
    target_email: Optional[str] = None


class OrgViewAsWrites(BaseModel):
    """Les écritures faites en « voir en tant que » sous cette org, plus récentes
    d'abord (fenêtre `days`, défaut 30). Vide = aucune sur la fenêtre."""
    writes: list[ViewAsWriteRow]


class OrgViewAsWritesInput(BaseModel):
    org_id: int
    days: Optional[int] = None
    limit: int = 200

    @field_validator("limit")
    @classmethod
    def _cap_limit(cls, v):
        return cap_limit(v, 200)


def _view_as_writes(ctx: ResolvedCtx, inp: OrgViewAsWritesInput) -> dict:
    rows = db.list_view_as_writes(inp.org_id, days=inp.days, limit=inp.limit)
    return {"writes": [{**r, "called_at": (str(r["called_at"])
                                            if r.get("called_at") is not None else None)}
                       for r in rows]}


_ADMIN_OF = ORG_ADMIN_OF("org_id")

CAPABILITIES += [
    Capability(key="org.monitoring.summary", handler=bornee(_summary), Input=OrgSummaryInput,
               authz=_ADMIN_OF, mcp=None, Output=OrgMonitoringSummary,
               rest=RestBinding("GET", "/api/orgs/{id}/monitoring/summary", _ID)),
    Capability(key="org.monitoring.calls", handler=bornee(_calls), Input=OrgCallsInput,
               authz=_ADMIN_OF, mcp=None, Output=OrgCalls,
               rest=RestBinding("GET", "/api/orgs/{id}/monitoring/calls", _ID)),
    # Lentille MEMBRE, volontairement distincte de celle du dessus (voir le bloc
    # de commentaire au-dessus de `BillableCallRow`) : le relevé de consommation
    # est une page que tout membre doit pouvoir lire, l'activité nominative de
    # ses collègues non. `mcp=None` : c'est un tuyau de facturation, pas un outil
    # d'agent.
    Capability(key="org.usage.calls", handler=bornee(_billable_calls),
               Input=OrgBillableCallsInput, authz=_MEMBER_OF, mcp=None,
               Output=OrgBillableCalls, errors=(_REFUS_FENETRE, _REFUS_LIMITE),
               rest=RestBinding("GET", "/api/orgs/{id}/usage/calls", _ID)),
    # Le même relevé pour le COMMERCE, avec la personne de chaque appel : c'est ce qui
    # lui permet d'appliquer une limite de dépense par personne ou par équipe. REST
    # seule, sous `/api/service/`, comme toute sa face.
    Capability(key="service.org.usage.calls", handler=bornee(_billable_calls_service),
               Input=OrgBillableCallsInput, authz=COMMERCE_SERVICE, mcp=None,
               Output=ServiceBillableCalls, errors=(_REFUS_FENETRE, _REFUS_LIMITE),
               description="[service commerce] The billable calls of an org, as "
                           "`org.usage.calls` serves them to members, plus `sub`: the "
                           "person each call ran for. Same window, page and cursor "
                           "contract.",
               rest=RestBinding("GET", "/api/service/orgs/{id}/usage/calls", _ID)),
    # Le même relevé, sommé par outil en une lecture (#1145) : remplace une requête
    # `usage/calls` par outil. Même lentille membre, `mcp=None` pour la même raison.
    Capability(key="org.usage.tools", handler=bornee(_billable_tools),
               Input=OrgBillableToolsInput, authz=_MEMBER_OF, mcp=None,
               Output=OrgBillableTools, errors=(_REFUS_FENETRE,),
               rest=RestBinding("GET", "/api/orgs/{id}/usage/tools", _ID)),
    # Même lentille membre, pour les connexions de messagerie d'une org (voir le bloc
    # au-dessus de `OrgConnectionRow`). `mcp=None` : un tuyau de relevé, pas un outil
    # d'agent.
    Capability(key="org.usage.connections", handler=_connections,
               Input=OrgConnectionsInput, authz=_MEMBER_OF, mcp=None,
               Output=OrgConnections,
               rest=RestBinding("GET", "/api/orgs/{id}/usage/connections", _ID)),
    Capability(key="org.usage.tokens", handler=bornee(_tokens), Input=OrgTokensInput,
               authz=_ADMIN_OF, mcp=None, Output=OrgTokens,
               rest=RestBinding("GET", "/api/orgs/{id}/usage/tokens", _ID)),
    Capability(key="org.monitoring.call", handler=_call, Input=OrgCallInput,
               authz=_ADMIN_OF, mcp=None, Output=OrgCall,
               rest=RestBinding("GET", "/api/orgs/{id}/monitoring/calls/{call_id}", _ID)),
    Capability(key="org.monitoring.signals", handler=_signals, Input=OrgMonitoringInput,
               authz=_ADMIN_OF, mcp=None, Output=OrgSignals,
               description="Raw usage signals REPORTED UNDER this org — the BODY, not "
                           "just the count. Filters: `signal` (tool_feedback|gap), "
                           "`tool` (target), `status`. Org admin only.",
               rest=RestBinding("GET", "/api/orgs/{id}/monitoring/signals", _ID)),
    Capability(key="org.monitoring.view_as_writes", handler=bornee(_view_as_writes),
               Input=OrgViewAsWritesInput, authz=_ADMIN_OF, mcp=None,
               Output=OrgViewAsWrites,
               description="Writes made ON BEHALF of a member of this org by a platform "
                           "operator (view-as with explicit write acceptance): who "
                           "(operator), as whom (target), which route, when, outcome. "
                           "Never arguments nor secrets. Org admin only.",
               rest=RestBinding("GET", "/api/orgs/{id}/monitoring/view-as", _ID)),
    Capability(key="org.monitoring.connectors", handler=bornee(_connectors), Input=OrgWindowInput,
               authz=_ADMIN_OF, mcp=None, Output=OrgConnectorHealth,
               rest=RestBinding("GET", "/api/orgs/{id}/monitoring/connectors", _ID)),
    Capability(key="org.monitoring.adoption", handler=bornee(_adoption), Input=OrgDaysInput,
               authz=_ADMIN_OF, mcp=None, Output=OrgAdoption,
               rest=RestBinding("GET", "/api/orgs/{id}/monitoring/adoption", _ID)),
    Capability(key="org.monitoring.runs", handler=bornee(_runs), Input=OrgRunsInput,
               authz=_ADMIN_OF, mcp=None, Output=OrgRuns,
               rest=RestBinding("GET", "/api/orgs/{id}/monitoring/runs", _ID)),
    Capability(key="org.monitoring.run", handler=_run, Input=OrgRunInput,
               authz=_ADMIN_OF, mcp=None, Output=OrgRun,
               rest=RestBinding("GET", "/api/orgs/{id}/monitoring/runs/{run_id}", _ID)),
    Capability(key="org.monitoring.gaps", handler=_gaps, Input=OrgDaysInput,
               authz=_ADMIN_OF, mcp=None, Output=OrgGaps,
               rest=RestBinding("GET", "/api/orgs/{id}/monitoring/gaps", _ID)),
    Capability(key="org.monitoring.tool_quality", handler=_tool_quality, Input=OrgDaysInput,
               authz=_ADMIN_OF, mcp=None, Output=OrgToolQuality,
               rest=RestBinding("GET", "/api/orgs/{id}/monitoring/tool-quality", _ID)),
    Capability(
        key="org.monitoring.console", handler=bornee(_console), Input=OrgMonitoringInput,
        authz=_ADMIN_OF,
        description=(
            "Observability of YOUR org (org admin) — `org_id` required. op=summary "
            "(aggregates over the org: totals, by tool w/ avg+p95 latency, by member, by "
            "day; `days`, optional `sub` email|sub) / adoption (member by member: who "
            "actually uses oto, who never did, who is blocked by a connector — the org's "
            "answer to 'is my team on board') / calls (call log of the org, newest first; "
            "filters `sub`, `tool`, `errors`, `days`, `run_id`, `session_id`, "
            "`min_duration_ms`, `error_contains`) / call (`call_id`) / runs · run "
            "(`run_id` → timeline; `content_archived` = its body went to cold archive, "
            "only its bounds remain) / connectors (which connector fails to resolve for "
            "your members) / gaps · tool_quality (what YOUR members reported missing or "
            "broken, AGGREGATED) / signals (the same reports RAW, with their body — the "
            "counts say how many, only the body says why; filters `signal` "
            "tool_feedback|gap, `tool`, `status`) / export (audit log, `since`/`until` "
            "ISO — compliance evidence) / agents_en_echec (your hosted agents whose "
            "JOBS are dying, grouped by agent AND by reason — `days`, default 1). "
            "⚠️ A triggered agent breaks SILENTLY: its queue empties because dead jobs "
            "stop waiting, the screen says `active`, and the last run can be green — "
            "this is the lens that says otherwise. Grouped by reason because three "
            "times the same fault is a bug, three different ones is a misconfigured "
            "agent. "
            "Everything is scoped to calls EMITTED UNDER this org, never to membership. "
            "Platform-wide investigation is oto_admin_monitoring (platform admin)."),
        mcp="oto_org_monitoring",
    ),
]

