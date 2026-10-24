"""Error tracking Sentry (SaaS) — init + capture des exceptions de tools MCP.

Deux surfaces d'erreur, deux mécanismes :
- **Routes REST `/api/*`** : l'intégration Starlette du SDK (auto-activée par
  `sentry-sdk[starlette]`) capture les 500 ASGI avec traceback complet. Rien à
  câbler ici — il suffit que `init_sentry()` tourne AVANT `mcp.http_app()`.
- **Tools MCP** : une exception de tool devient une erreur JSON-RPC en HTTP 200 →
  l'intégration Starlette ne la voit pas. `SentryToolErrorMiddleware` la capture là
  où l'exception est vivante (vrai traceback), puis re-raise (comportement inchangé).

Gaté par `OTO_SENTRY_DSN` : absent → `sentry_sdk` n'est jamais initialisé, tout
`capture_exception` est un no-op. Le serveur boote normalement sans Sentry.

RGPD : `send_default_pii=False` (pas d'IP/cookies/headers auto-collectés) et on
n'attache JAMAIS les arguments d'appel (emails, données entreprise) à l'event —
seulement le nom du tool + le `sub` Logto (id opaque pseudonyme, utile au debug).

Sentry = défauts du CODE. Un **refus client amont** (4xx d'une API tierce : input
rejeté, credential invalide, cible absente) n'en est pas un — c'est une *erreur de
connecteur gérée*, déjà tracée dans le backlog `tool_calls` (calllog) et renvoyée à
l'agent en `ToolError`. On la classe **par type** (`UpstreamHTTPError` d'oto-core,
ou `httpx`/`requests` HTTPError, ou erreur connecteur typée portant un statut) et on
ne la reporte pas. Deux chemins de capture à neutraliser : le middleware explicite
(qui ne capture pas) et la LoggingIntegration sur le `logger.error` de fastmcp (le
`before_send` la droppe). Les 5xx et les vraies exceptions code restent reportées.

**Panne amont** (5xx d'une API tierce, ou timeout RÉSEAU vers elle) : reportée,
mais en `warning` et regroupée par outil (`upstream_level`) — une issue par
fournisseur en panne, pas une alerte « nouvelle issue » par message, et l'alerte
`#bug-report` (niveau error) n'est plus réveillée par Apollo ou Serper qui
toussent. Un timeout de NOTRE code (base, boucle) reste une erreur.
"""
from __future__ import annotations

import logging
import os
from contextvars import ContextVar
from typing import Optional

import sentry_sdk
from fastmcp.server.middleware import Middleware
from sentry_sdk.integrations.logging import ignore_logger
from sentry_sdk.integrations.mcp import MCPIntegration
from starlette.concurrency import run_in_threadpool

from .auth.hooks import current_client_id_from_token, current_user_sub_from_token
# Classifieurs partagés (D2, #124) : source unique de la taxonomie d'exceptions,
# consommée aussi par `ErrorEnvelopeMiddleware`. Ré-exportés ici (les tests et le
# reste du module les référencent via `sentry_setup`).
from .error_taxonomy import (  # noqa: F401
    _USER_INPUT_CODES,
    _is_arg_validation_error,
    _is_client_disconnect,
    _is_expected_error,
    _is_managed_connector_error,
    _is_user_input_error,
    _upstream_status,
    _chain,
    upstream_status_in_chain,
)

logger = logging.getLogger("oto_mcp")

# Event Sentry capturé pour l'appel EN COURS (extension OTO-LOCALE) : posé par
# `SentryToolErrorMiddleware` (innermost), relu par le sink calllog (plus externe,
# même tâche → la ContextVar mutée ici lui est visible) qui le stampe sur la ligne
# `tool_calls`. Ferme le détour « erreur au journal → chercher à la main dans Sentry
# par user.id » : la ligne d'audit porte le lien vers le traceback.
_LAST_EVENT_ID: ContextVar[Optional[str]] = ContextVar("oto_sentry_event_id", default=None)


def current_tool_event_id() -> Optional[str]:
    """Event id Sentry de l'appel courant, ou None (nominal / Sentry off)."""
    try:
        return _LAST_EVENT_ID.get()
    # noqa: SILENT — hors contexte de requête : pas d'event_id à corréler
    except Exception:
        return None


# Timeouts RÉSEAU vers un tiers, reconnus par TYPE (nom + module) : un
# `TimeoutError` nu ou un timeout de base (psycopg) reste un défaut de code.
_NETWORK_TIMEOUT_MODULES = ("requests.", "httpx", "urllib3.", "httpcore")


def _is_network_timeout(exc) -> bool:
    for e in _chain(exc):
        mod = type(e).__module__ or ""
        if "Timeout" in type(e).__name__ and mod.startswith(_NETWORK_TIMEOUT_MODULES):
            return True
    return False


def upstream_level(exc) -> Optional[str]:
    """`upstream_5xx` / `upstream_timeout` quand l'erreur est une panne d'un
    fournisseur tiers (à reporter en warning, regroupée), sinon None."""
    sc = upstream_status_in_chain(exc)
    if sc is not None and 500 <= sc < 600:
        return "upstream_timeout" if sc == 504 else "upstream_5xx"
    if sc is None and _is_network_timeout(exc):
        return "upstream_timeout"
    return None


def _before_send(event, hint):
    """Droppe les erreurs gérées — couvre aussi la copie LoggingIntegration.

    Deux familles, deux raisons : l'erreur GÉRÉE (`_is_expected_error` : 4xx amont,
    refus d'entrée…) n'est pas un bug ; la DÉCONNEXION CLIENT n'est même pas une
    erreur — le client a raccroché pendant qu'on lui répondait. Cette seconde
    famille est traitée ici et pas dans `_is_expected_error` parce qu'elle ne
    concerne que Sentry : elle n'atteint aucun agent (cf. `_is_client_disconnect`).
    """
    exc_info = (hint or {}).get("exc_info")
    if exc_info and (_is_expected_error(exc_info[1]) or _is_client_disconnect(exc_info[1])):
        return None
    _redact_sensitive_query(event)
    _redact_hook_request(event)
    _redact_urls_in_messages(event)
    return event


def _redact_urls_in_messages(event) -> None:
    """Le message d'une exception (et d'une ligne de journal relayée) peut recopier
    une URL avec sa clé — Playwright y écrit la `connectUrl` Browserbase
    `?apiKey=…` (F-049). Même masque que `tool_calls.error`
    (`journal_secrets.masquer_urls`)."""
    from .journal_secrets import masquer_urls

    if not isinstance(event, dict):
        return
    for exc in ((event.get("exception") or {}).get("values") or []):
        if isinstance(exc, dict) and isinstance(exc.get("value"), str):
            exc["value"] = masquer_urls(exc["value"])
    logentry = event.get("logentry")
    if isinstance(logentry, dict):
        for champ in ("message", "formatted"):
            if isinstance(logentry.get(champ), str):
                logentry[champ] = masquer_urls(logentry[champ])
    if isinstance(event.get("message"), str):
        event["message"] = masquer_urls(event["message"])


def _before_send_transaction(event, hint):
    _redact_sensitive_query(event)
    _redact_hook_request(event)
    return event


def _redact_hook_request(event) -> None:
    """Une requête de webhook ne part jamais entière vers Sentry : son ADRESSE
    est le credential d'un agent sans preuve (`hook_auth=none`), et son CORPS
    porte le porteur d'un agent `body_secret` (le secret renvoyé par la source).
    Le chemin est masqué, le corps retiré — la pile suffit au diagnostic."""
    from urllib.parse import urlsplit

    req = (event or {}).get("request") if isinstance(event, dict) else None
    if not isinstance(req, dict):
        return
    url = str(req.get("url") or "")
    parts = urlsplit(url)
    if not parts.path.startswith("/api/hooks/"):
        return
    req["url"] = f"{parts.scheme}://{parts.netloc}/api/hooks/[redacted]" if parts.netloc \
        else "/api/hooks/[redacted]"
    req.pop("data", None)
    req.pop("query_string", None)


def _redact_sensitive_query(event) -> None:
    """Retire la query string des routes qui REÇOIVENT un secret en query
    (`journal_secrets.routes_a_requete_secrete`, la même liste que le journal
    d'accès). `send_default_pii=False` ne couvre pas la query d'une requête."""
    from urllib.parse import urlsplit

    from .journal_secrets import requete_secrete
    req = (event or {}).get("request") if isinstance(event, dict) else None
    if not isinstance(req, dict):
        return
    url = str(req.get("url") or "")
    if requete_secrete(urlsplit(url).path):
        req["url"] = url.split("?", 1)[0]
        req["query_string"] = "[redacted]"


def _release() -> Optional[str]:
    """`OTO_SENTRY_RELEASE` s'il est posé ; sinon l'étiquette de la version servie
    (`v1.2.3+6d5bf16b`, `version.etiquette`) — la même que `/api/version` et
    `X-Oto-Version`, pour qu'un événement Sentry se rapproche d'un déploiement sans
    que l'exploitant ait à recopier le tag dans une variable. Inconnue : None (pas
    de release), plutôt qu'une étiquette `unknown` qui regrouperait tout."""
    posee = (os.environ.get("OTO_SENTRY_RELEASE") or "").strip()
    if posee:
        return posee
    try:
        from . import version
        ref, commit, _at, _source = version._coordonnees()
        etiquette = version.etiquette(ref, commit)
    except Exception as e:  # noqa: SILENT — une release manquante ne doit pas empêcher Sentry de démarrer
        logger.warning("Sentry : version servie illisible, pas de release (%s)", type(e).__name__)
        return None
    return None if etiquette == version.INCONNU else etiquette


def init_sentry() -> bool:
    """Initialise Sentry si `OTO_SENTRY_DSN` est posé. Retourne True si actif."""
    dsn = os.environ.get("OTO_SENTRY_DSN", "").strip()
    if not dsn:
        logger.info("Sentry désactivé (OTO_SENTRY_DSN absent)")
        return False
    sentry_sdk.init(
        dsn=dsn,
        # oto-backend#968 : jamais deviné — un défaut "production" ici aurait pu
        # étiqueter un process preprod/tiers comme prod si l'exploitant oublie la
        # variable, faussant ses alertes. Cohérent avec config.py (même variable,
        # même défaut vide) ; `verifier_repli_identite_dev`-like : la cohérence
        # avec `OTO_ENV`, quand les deux sont posées, reste vérifiée ailleurs
        # (config.py:152-156).
        environment=os.environ.get("OTO_SENTRY_ENV", ""),
        release=_release(),
        # RGPD : pas d'IP / cookies / headers auto-collectés.
        send_default_pii=False,
        # ⚠️ NE PAS RETIRER — le défaut du SDK est `True` (vérifié sentry-sdk 2.63.0),
        # et `send_default_pii=False` ne couvre PAS le contenu des frames : chaque
        # exception repartait avec les variables locales de toute la pile, dont
        # celles du chemin de résolution de credential, qui tiennent le secret
        # DÉCHIFFRÉ (#564). Le réglage, pas une liste à scruber : une liste redevient
        # fausse au premier renommage. Ce qu'on perd au diagnostic est mince — type,
        # message et pile restent. Cliquet : `tests/test_journal_no_plaintext_secret.py`.
        include_local_variables=False,
        # Tracing de perf désactivé par défaut (on cible l'error tracking).
        traces_sample_rate=float(os.environ.get("OTO_SENTRY_TRACES_SAMPLE_RATE", "0") or "0"),
        # Ne pas reporter les erreurs gérées (4xx amont + refus d'entrée/config
        # user McpError) : pas des bugs backend.
        before_send=_before_send,
        # Une transaction (si le traçage est activé) porte aussi l'URL de la requête.
        before_send_transaction=_before_send_transaction,
        # oto-backend#869 — le SDK AUTO-ACTIVE `MCPIntegration` dès `mcp>=1.15.0`
        # (nous sommes en mcp 1.27.2) : elle capture la MÊME `McpError` que
        # `SentryToolErrorMiddleware` ci-dessous, SANS le tag `mcp.tool` ni
        # l'utilisateur, et sans passer par `_before_send` côté taxonomie — d'où
        # l'issue Sentry au titre trompeur « Erreur interne du serveur » et un
        # triplet d'événements par erreur (mesuré : 528/528/528, 293/293/293).
        # Coupée ici : zéro perte d'information, le middleware reste le SEUL
        # capteur, celui qui étiquette.
        disabled_integrations=[MCPIntegration()],
    )
    # oto-backend#869 — la 2ᵉ copie du triplet : la LoggingIntegration du SDK relaie
    # `logger.exception(f"Error calling tool {name!r}")` de fastmcp
    # (`fastmcp/server/server.py`, logger `fastmcp.server.server`) vers Sentry. Le
    # `before_send` la droppe déjà si l'erreur est gérée, mais sur une erreur RÉELLE
    # elle double `SentryToolErrorMiddleware` sans rien ajouter (pas de tag, pas
    # d'utilisateur) — l'événement du middleware suffit.
    ignore_logger("fastmcp.server.server")
    logger.info("Sentry actif (env=%s)", os.environ.get("OTO_SENTRY_ENV", "") or "(non déclaré)")
    return True


class SentryToolErrorMiddleware(Middleware):
    """Capture les exceptions des tools MCP vers Sentry, puis re-raise.

    No-op si Sentry n'est pas initialisé (`capture_exception` ne fait rien). Sur le
    chemin nominal, ce middleware ne fait que déléguer — aucun surcoût. Une **erreur
    gérée** (4xx amont OU refus d'entrée/config user) n'est PAS capturée (cf. module).

    ⚠️ **`capture_exception` hors boucle (2026-09-14/15)** : `on_call_tool` est
    `async def`, donc tout ce qu'il fait nûment tourne sur le thread mono-loop du
    serveur (docs/event-loop-perf.md, mode n°2). `capture_exception` construit
    l'event EN ENTIER (extraction de la pile, filtrage in-app, lecture du contexte
    source de chaque frame via `linecache` — de la lecture DISQUE, synchrone) avant
    de le mettre en file pour le transport (qui, lui, envoie déjà en tâche de fond,
    hors sujet ici). Diagnostiqué sur `linkedin_aiark_person` : 30 timeouts
    urllib3/requests en production, 30 `sentry_event_id` posés (donc 30 captures
    dans la boucle), mais seulement 5 ont mesuré un gel ≥1 s au même moment — le
    coût VARIE (mesuré : médiane ~5-9 ms, une capture « à froid » jusqu'à ~70 ms
    sur `tests/test_sentry_capture_hors_boucle.py`, premier appel après boot —
    `linecache` lit chaque fichier source une fois). Ce n'est à soi seul ni la
    cause unique ni systématique des gels ≥1 s observés (l'ordre de grandeur mesuré
    est trop petit pour ça isolément) — mais c'est bien de l'I/O sync nu dans la
    boucle, sur le chemin d'erreur de CHAQUE appel d'outil : la règle mono-loop
    s'applique, indépendamment du chiffre moyen.

    Seul `capture_exception` part en `run_in_threadpool` — la création du scope et
    la pose des tags/user restent dans le contexte ASYNC, `run_in_threadpool`
    (anyio) exécute sur une COPIE de ce contexte : le scope Sentry est porté par
    une ContextVar interne au SDK, donc les tags posés avant l'entrée dans le
    thread sont bien présents sur l'event capturé DEDANS (vérifié empiriquement,
    event réel à transport collecteur —
    `tests/test_sentry_capture_hors_boucle.py::test_le_tag_pose_avant_le_thread_est_sur_levent_capture_dans_le_thread`).
    `_LAST_EVENT_ID.set(...)` (la ContextVar OTO, pas celle du SDK) reste dans le
    contexte ASYNC, posée APRÈS le retour du thread — une écriture faite depuis le
    thread ne remonterait pas à l'appelant (même piège que documenté pour
    `session_visibility.py` le 14/09/2026)."""

    async def on_call_tool(self, context, call_next):
        # Remise à zéro par appel : sans elle, un event capturé plus tôt dans la même
        # tâche serait stampé sur la ligne d'un appel suivant, sain.
        _LAST_EVENT_ID.set(None)
        try:
            return await call_next(context)
        except Exception as e:
            if not _is_expected_error(e):
                try:
                    with sentry_sdk.new_scope() as scope:
                        scope.set_tag("mcp.tool", context.message.name)
                        try:
                            sub = current_user_sub_from_token()
                        # noqa: SILENT — sans sub, la trace reste anonyme plutôt que fausse
                        except Exception:
                            sub = None
                        if sub:
                            scope.set_user({"id": sub})
                        # Surface cliente (`azp` : claude.ai, Claude Code…) — où
                        # l'erreur se produit, pas qui l'a causée.
                        client = current_client_id_from_token()
                        if client:
                            scope.set_tag("mcp.client", client)
                        panne = upstream_level(e)
                        if panne:
                            scope.set_level("warning")
                            scope.set_tag("upstream", panne)
                            scope.fingerprint = ["upstream", context.message.name, panne]
                        # Hors boucle : construction de l'event (pile, in-app,
                        # linecache) sur un thread du pool. L'id retourné (None si
                        # Sentry est off ou l'event droppé par `before_send`)
                        # devient le lien journal → traceback.
                        event_id = await run_in_threadpool(sentry_sdk.capture_exception, e)
                        _LAST_EVENT_ID.set(event_id)
                # noqa: SILENT — la capture ne doit jamais masquer l'erreur d'origine
                except Exception:
                    # La capture ne doit jamais masquer l'erreur d'origine.
                    pass
            raise
