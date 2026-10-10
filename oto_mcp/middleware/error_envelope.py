"""`ErrorEnvelopeMiddleware` — le contrat d'erreur uniforme rendu à l'agent."""
from __future__ import annotations

from fastmcp.server.middleware import Middleware
from ..mcp_errors import McpError
from mcp.types import ErrorData
from starlette.concurrency import run_in_threadpool

from .. import error_taxonomy, session_org
from ..auth.hooks import current_user_sub_from_token
from ..connectors import health as connector_health


def _reachable_suffix(connector: str) -> str:
    """Suffixe « des clés existent à portée » pour l'enveloppe d'erreur. Sync (DB) —
    à appeler via `run_in_threadpool`. Réutilise le seam d'`access` (aucune règle
    d'accès recopiée ici) ; chaîne vide si rien à portée ou hors contexte."""
    from .. import access
    sub = current_user_sub_from_token()
    if not sub:
        return ""
    return access._reachable_hint(sub, access.current_org(sub), connector)


async def _parametres_de(context, exc) -> list:
    """Les paramètres de l'outil dont la SIGNATURE a refusé l'appel (oto#135) — ce qui
    permet au refus de dire le paramètre le plus proche d'une clé inconnue.

    L'outil est celui que NOMME l'erreur (`call[data_write]`), pas celui de la requête :
    derrière `oto_call`, c'est l'outil appelé qui a refusé. Lu dans le catalogue BRUT
    (outils masqués compris, même raison que `oto_tool_schema`), et seulement sur ce
    chemin d'erreur."""
    from fastmcp.server.providers.base import Provider
    nom = error_taxonomy.outil_de_signature(exc)
    ctx = getattr(context, "fastmcp_context", None)
    if not nom or ctx is None:
        return []
    for t in await Provider.list_tools(ctx.fastmcp):
        if t.name == nom:
            return list(((t.parameters or {}).get("properties") or {}))
    return []


def _detail(exc) -> dict:
    """Le premier `oto_detail` (dict) porté par la chaîne d'exceptions, sinon {}.

    Attribut DÉDIÉ, posé exprès par un outil sur son propre refus : jamais le
    `data` d'une `McpError` quelconque (une enveloppe déjà posée en porte un, et le
    recopier l'imbriquerait), jamais rien d'amont (anti-fuite)."""
    for e in error_taxonomy._chain(exc):
        d = getattr(e, "oto_detail", None)
        if isinstance(d, dict) and d:
            return d
    return {}


class ErrorEnvelopeMiddleware(Middleware):
    """Contrat d'erreur uniforme rendu à l'agent (D2, oto-backend#124).

    Toute exception d'un tool est réécrite en `McpError` **scrubbée** (pas de
    stacktrace / route interne / id technique) portant `data.oto = {code, retryable,
    hint}` — l'agent peut alors DÉCIDER (retry / abandon / corriger l'input) au lieu
    de deviner sur un message brut. Les tools qui lèvent déjà une `McpError` curée
    voient leur message conservé (cf. `error_taxonomy.classify`).

    **Outermost** (ajouté AVANT `SentryToolErrorMiddleware`) : la chaîne s'exécute de
    l'extérieur vers l'intérieur, donc Sentry (plus interne) attrape l'exception
    d'ORIGINE en premier (vrai traceback capturé), la re-raise, et cette enveloppe la
    normalise EN DERNIER avant qu'elle ne quitte le serveur. Placer l'enveloppe plus
    interne masquerait le vrai traceback à Sentry.
    """

    async def on_call_tool(self, context, call_next):
        try:
            result = await call_next(context)
        except Exception as e:
            parametres: list = []
            if error_taxonomy._is_arg_validation_error(e):
                try:
                    parametres = await _parametres_de(context, e)
                # noqa: SILENT — un indice de plus : échouer à lire le catalogue ne doit jamais masquer le refus d'origine
                except Exception:  # noqa: BLE001
                    parametres = []
            info = error_taxonomy.classify(e, parametres)
            data = {"code": info.code, "retryable": info.retryable}
            hint = info.hint
            # Outil non monté = le PREMIER mur. Sans ça l'agent installe le
            # connecteur, rappelle, et se prend un SECOND mur (« aucun credential »)
            # qui seul portait l'info utile — alors qu'une clé existe peut-être à
            # portée (équipe dont il est membre, autre org). On remonte les deux
            # d'un coup. DB SYNC → threadpool obligatoire (serveur mono-loop), et
            # best-effort : un hoquet ici ne doit jamais masquer l'erreur d'origine.
            if info.connector:
                try:
                    hint = (hint or "") + await run_in_threadpool(
                        _reachable_suffix, info.connector)
                # noqa: SILENT — dette déclarée : le hint « une clé existe à portée » disparaît (#424, verdict C)
                except Exception:  # noqa: BLE001
                    pass
            # Crédits épuisés : la clé qui a servi CET appel passe au rouge sur sa
            # fiche (« recharge »), sauf clé plateforme/tenant — garde de portée de
            # `connectors.health`. Le relevé est encore posé ici : `CallContext`
            # (plus externe) ne le défait qu'après nous.
            if info.code == "quota_exhausted":
                trace = session_org.current_call_trace()
                fournisseur = (trace or {}).get("resolved_connector")
                if fournisseur:
                    hint = f"{hint} — chez `{fournisseur}`"
                await connector_health.suivre_appel(trace, info.message)
            # Clé refusée par l'amont (401, ou refus que son connecteur déclare comme
            # celui de la clé) : même marque au rouge, verdict « repose la clé ».
            elif error_taxonomy.credential_rejected_in_chain(e):
                await connector_health.suivre_appel(
                    session_org.current_call_trace(), None, rejet=info.message)
            if hint:
                data["hint"] = hint
            # Faits STRUCTURÉS qu'un outil a posés sur son refus (`oto_detail`, un
            # dict à nous — ex. hubspot : le scope à ajouter). Sans ce relais, ils
            # mouraient ici : l'enveloppe réécrit tout et ne gardait que le message.
            detail = _detail(e)
            if detail:
                data["detail"] = detail
            raise McpError(ErrorData(
                code=error_taxonomy.jsonrpc_code(info),
                message=info.message,
                data={"oto": data},
            )) from e
        # Succès : une marque « crédits épuisés » de la clé servie est levée (une seule
        # écriture conditionnelle par clé et par process — cf. `connectors.health`).
        await connector_health.suivre_appel(session_org.current_call_trace(), None)
        return result
