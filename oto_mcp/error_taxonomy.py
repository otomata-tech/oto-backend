"""Taxonomie d'erreurs de tools — classification + scrub partagés (D2, oto-backend#124).

Point unique qui CLASSE une exception de tool remontée par fastmcp (catégorie machine
`code` + `retryable`) et SCRUBBE son message pour l'agent. Réutilisé par :

- `sentry_setup` : décider si une erreur est un bug backend (report) ou gérée (drop) —
  les prédicats `_is_*` ci-dessous ;
- `ErrorEnvelopeMiddleware` (`middleware.py`) : rendre à l'agent une erreur au **contrat
  uniforme** `{code, retryable, hint}`, sans stacktrace / route interne / id technique
  (`classify` + `scrub`).

fastmcp emballe l'erreur d'un tool dans un `ToolError` → tous les prédicats **remontent
la chaîne** `__cause__`/`__context__` jusqu'à l'exception d'origine.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Iterator, Optional

from mcp.shared.exceptions import McpError
from mcp.types import INTERNAL_ERROR, INVALID_PARAMS, INVALID_REQUEST
from pydantic import ValidationError

# Codes JSON-RPC d'erreur d'ENTRÉE/CONFIG côté user (pendant natif d'un 4xx amont) :
# « pose ta clé », « connecte ton compte », param/org invalide. Levés
# intentionnellement par les tools/capacités, pas des bugs backend.
_USER_INPUT_CODES = {INVALID_PARAMS, INVALID_REQUEST}


def _chain(exc) -> Iterator[BaseException]:
    """L'exception et sa chaîne de causes (`__cause__` puis `__context__`), sans cycle."""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def _upstream_status(exc) -> Optional[int]:
    """Code HTTP amont porté par UNE exception, sinon None.

    Couvre `UpstreamHTTPError` (oto-core, `.status_code`), `httpx`/`requests`
    HTTPError (`.response.status_code`) et les erreurs connecteur typées maison
    (`.status`, ex. `NinjaError`).
    """
    for attr in ("status_code", "status"):
        v = getattr(exc, attr, None)
        if isinstance(v, int):
            return v
    v = getattr(getattr(exc, "response", None), "status_code", None)
    return v if isinstance(v, int) else None


def upstream_status_in_chain(exc) -> Optional[int]:
    """Premier code HTTP amont trouvé en remontant la chaîne, sinon None."""
    for e in _chain(exc):
        sc = _upstream_status(e)
        if sc is not None:
            return sc
    return None


def _is_managed_connector_error(exc) -> bool:
    """True si la chaîne porte un refus client amont (4xx) — erreur de connecteur
    gérée, pas un bug backend."""
    for e in _chain(exc):
        sc = _upstream_status(e)
        if sc is not None and 400 <= sc < 500:
            return True
    return False


def _is_user_input_error(exc) -> bool:
    """True si la chaîne porte une `McpError` de code d'entrée/config user
    (INVALID_PARAMS / INVALID_REQUEST) — refus explicite, pas un bug backend."""
    for e in _chain(exc):
        if isinstance(e, McpError) and getattr(e.error, "code", None) in _USER_INPUT_CODES:
            return True
    return False


def _is_arg_validation_error(exc) -> bool:
    """True si la chaîne porte une `ValidationError` pydantic (args rejetés)."""
    for e in _chain(exc):
        if isinstance(e, ValidationError):
            return True
    return False


def _is_expected_error(exc) -> bool:
    """Erreur gérée, à NE PAS reporter à Sentry : 4xx amont OU refus d'entrée/config
    user OU args rejetés. Les vraies exceptions code (5xx, KeyError, InvalidTag…)
    restent reportées."""
    return (_is_managed_connector_error(exc)
            or _is_user_input_error(exc)
            or _is_arg_validation_error(exc))


# --- Enveloppe d'erreur rendue à l'agent (D2) --------------------------------

@dataclass
class ErrorInfo:
    """Erreur normalisée présentée à l'agent. `code` = catégorie machine ;
    `retryable` = l'agent peut réessayer tel quel ; `message` scrubbé (zéro
    stacktrace/route/id) ; `hint` = quoi faire, quand dérivable."""
    code: str
    retryable: bool
    message: str
    hint: Optional[str] = None


# net::ERR_* (erreurs Chromium crues) — remplacent tout le message (aucune info utile).
_NET_ERR = re.compile(r"net::ERR_[A-Z_]+")
# Routes internes (« Cannot GET /api/v1/… », chemins d'API) — fuite de topologie serveur.
_ROUTE = re.compile(r"(?:Cannot\s+(?:GET|POST|PUT|DELETE|PATCH)\s+)?/(?:api|v\d)[\w/.\-]*", re.I)
# Jetons techniques longs (account_id, uuid) ≥ 20 chars — fuite d'identifiants internes.
_LONG_ID = re.compile(r"\b[A-Za-z0-9][A-Za-z0-9_\-]{19,}\b")
_TIMEOUT_MARKERS = ("timeout", "timed out", "délai d'attente", "read timed out")


def scrub(message: str) -> str:
    """Retire d'un message d'erreur les fuites internes (net::ERR_*, routes, ids
    techniques). Best-effort — appliqué aux messages amont, jamais aux `McpError`
    qu'on a nous-mêmes curées."""
    if not message:
        return ""
    if _NET_ERR.search(message):
        return "Échec réseau amont (hôte non résolu ou service injoignable)."
    message = _ROUTE.sub("[route interne]", message)
    message = _LONG_ID.sub("[id]", message)
    return message.strip()


def _first_upstream_message(exc) -> str:
    """Str de la 1ʳᵉ exception de la chaîne portant un statut amont (pour scrub)."""
    for e in _chain(exc):
        if _upstream_status(e) is not None:
            return str(e)
    return str(exc)


def _looks_like_timeout(exc) -> bool:
    for e in _chain(exc):
        if isinstance(e, (asyncio.TimeoutError, TimeoutError)):
            return True
        if any(m in str(e).lower() for m in _TIMEOUT_MARKERS):
            return True
    return False


def classify(exc) -> ErrorInfo:
    """Classe une exception de tool en `ErrorInfo` au contrat uniforme.

    Ordre : (1) `McpError` qu'on a levée (message curé conservé) ; (2) args pydantic
    rejetés ; (3) statut HTTP amont (timeout/rate-limit/not-found/authz/4xx/5xx) ;
    (4) timeout non typé ; (5) reste = interne — **aucun écho du `str(exc)`** (anti-fuite).
    """
    # (1) McpError curée par un tool/capacité : message déjà agent-facing.
    for e in _chain(exc):
        if isinstance(e, McpError):
            jcode = getattr(e.error, "code", None)
            msg = (getattr(e.error, "message", None) or "").strip()
            if jcode in _USER_INPUT_CODES:
                return ErrorInfo("invalid_input", False, msg or "Requête invalide.")
            # McpError levée avec un autre code (rare) : on garde le texte curé,
            # traité comme interne non-retryable.
            return ErrorInfo("internal", False, msg or "Erreur interne du serveur.")

    # (2) Arguments rejetés (le LLM a passé de mauvais paramètres).
    if _is_arg_validation_error(exc):
        return ErrorInfo("invalid_input", False,
                         "Arguments invalides — vérifie les paramètres de l'outil.")

    # (3) Statut HTTP amont.
    sc = upstream_status_in_chain(exc)
    if sc is not None:
        raw = scrub(_first_upstream_message(exc))
        if sc in (408, 504):
            return ErrorInfo("upstream_timeout", True,
                             "Délai d'attente dépassé côté service amont.",
                             "réessaie dans un instant")
        if sc == 429:
            return ErrorInfo("rate_limited", True,
                             "Trop de requêtes côté service amont.",
                             "réessaie après une courte pause")
        if sc == 404:
            return ErrorInfo("not_found", False,
                             raw or "Ressource introuvable côté service amont.")
        if sc in (401, 403):
            return ErrorInfo("not_authorized", False,
                             raw or "Accès refusé par le service amont.",
                             "vérifie que le connecteur est connecté et autorisé")
        if 400 <= sc < 500:
            return ErrorInfo("upstream_4xx", False,
                             raw or f"Requête refusée par le service amont ({sc}).")
        if 500 <= sc < 600:
            return ErrorInfo("upstream_5xx", True,
                             f"Le service amont a rencontré une erreur ({sc}).",
                             "réessaie plus tard")

    # (4) Timeout non porté par un statut.
    if _looks_like_timeout(exc):
        return ErrorInfo("upstream_timeout", True,
                         "Délai d'attente dépassé.", "réessaie dans un instant")

    # (5) Reste = bug/erreur interne : PAS d'écho de str(exc) (anti-fuite).
    return ErrorInfo("internal", False, "Erreur interne du serveur.")


def jsonrpc_code(info: ErrorInfo) -> int:
    """Code JSON-RPC de la `McpError` rendue : INVALID_PARAMS pour un refus d'entrée,
    INTERNAL_ERROR sinon (le discriminant fin vit dans `data.oto.code`)."""
    return INVALID_PARAMS if info.code == "invalid_input" else INTERNAL_ERROR
