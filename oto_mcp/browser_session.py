"""Connexion par **session navigateur hébergée** (Browserbase) — seam partagé.

Pour un connecteur d'API privée cookie-bound (brevo, crunchbase…), la connexion = un
**login interactif** dans une Live View Browserbase : l'utilisateur se logue une fois
(SSO/captcha/2FA), sa session naît native dans un **Context** persistant qui devient le
credential du coffre. PAS de capture de cookie, PAS d'export, PAS de MCP requis.

Ce module factorise le flux pour qu'il soit servi par DEUX surfaces avec un seul corps
de logique (derive-don't-duplicate) :
- **REST** (dashboard) — bouton « Connecter » → Live View affichée en iframe (la voie
  produit) ;
- **MCP** (`<name>_connect_start`/`_connect_status`) — même flux depuis Claude.

`start()` est générique (aucune donnée par connecteur). Seule la **vérification du login**
diffère (cookie attendu vs sonde d'API) → chaque connecteur enregistre son `verify`.
Le substrat Browserbase lui-même vit dans `browserbase.py` (seam à sens unique, ADR 0004).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

from . import browserbase, db

logger = logging.getLogger(__name__)

# verify(session_id) -> bool : True si la session est bel et bien loguée (vérifié sur
# la session VIVANTE, jamais sur un export de cookie — cf. leçons ADR 0026).
Verify = Callable[[str], Awaitable[bool]]

_REGISTRY: dict[str, Verify] = {}

# URL de login par connecteur : la page vers laquelle amener la session dès l'ouverture
# de la Live View (sinon `about:blank`, l'utilisateur ne sait pas où se loguer). Optionnel
# — un connecteur sans URL enregistrée ouvre une page vierge (comportement historique).
_LOGIN_URLS: dict[str, str] = {}

# Sessions ÉMISES par `start()`, liées au `sub` qui les a demandées : `finalize` n'accepte
# qu'un (context_id, session_id) qu'IL a émis pour CE user (anti-IDOR : empêche de
# persister le Context — donc la session loguée — d'un tiers). In-memory : le serveur est
# mono-worker (cf. CLAUDE.md) et start→finalize vivent dans le même process à quelques
# minutes d'intervalle ; un restart entre les deux = re-cliquer « Connecter » (rare).
_PENDING: dict[tuple[str, str, str], float] = {}
_PENDING_TTL = 1000.0  # > keep-alive de session Browserbase (900 s)


class SessionError(RuntimeError):
    """Erreur actionnable du flux de connexion (Browserbase indispo, vérif KO…).
    Son message est rendu au client → ne JAMAIS y interpoler une exception brute
    (peut contenir l'URL CDP avec `?apiKey=…`) : logguer le détail, message propre."""


def _prune(now: float) -> None:
    for k, exp in list(_PENDING.items()):
        if exp < now:
            _PENDING.pop(k, None)


def register(connector: str, verify: Verify, *, login_url: str | None = None) -> None:
    """Déclare un connecteur à session navigateur + sa vérification de login. `login_url`
    = page de login vers laquelle ouvrir la Live View (recommandé — évite l'`about:blank`)."""
    _REGISTRY[connector] = verify
    if login_url:
        _LOGIN_URLS[connector] = login_url


def is_session_connector(connector: str) -> bool:
    return connector in _REGISTRY


def start(sub: str, connector: str | None = None) -> dict:
    """Ouvre un Context + une session keep-alive pour `sub` et renvoie la Live View
    interactive. Si `connector` a une `login_url` enregistrée, la session est amenée sur
    cette page avant l'affichage (sinon `about:blank`). La session émise est LIÉE à `sub`
    (consommée par `finalize`). BLOQUANT (HTTP Browserbase synchrone) → appeler via
    `asyncio.to_thread` depuis une route async. Lève `SessionError` si Browserbase n'est
    pas configuré côté plateforme."""
    if not browserbase.is_configured():
        raise SessionError("Browserbase non configuré côté plateforme "
                           "(BROWSERBASE_API_KEY / BROWSERBASE_PROJECT_ID).")
    try:
        context_id = browserbase.create_context()
        sess = browserbase.start_session(context_id, keep_alive=True, timeout=900)
        login_url = _LOGIN_URLS.get(connector or "")
        if login_url:
            # Best-effort : on amène la session sur la page de login. Un échec (nav lente,
            # CDP indispo) ne doit pas rater l'ouverture — l'user peut taper l'URL.
            try:
                asyncio.run(browserbase.navigate(sess["id"], login_url))
            except Exception:  # noqa: BLE001 — détail loggué, jamais renvoyé
                logger.warning("browserbase navigate to login failed for %s", connector)
        live = browserbase.live_view_url(sess["id"])
    except browserbase.BrowserbaseError as e:
        logger.warning("browserbase start failed: %s", e)
        raise SessionError("connexion au navigateur distant impossible — réessaie.")
    now = time.monotonic()
    _prune(now)
    _PENDING[(sub, context_id, sess["id"])] = now + _PENDING_TTL
    return {"live_view_url": live, "context_id": context_id, "session_id": sess["id"]}


def _persist(sub: str, connector: str, context_id: str, session_id: str) -> None:
    browserbase.release_session(session_id)        # libère → persiste le Context
    # Scope MEMBRE (ADR 0033) : la session navigateur est un credential comme un
    # autre — posée dans l'org de contexte, elle ne suit pas l'user dans ses autres
    # orgs. Import lazy (access importe db comme ce module — pas de cycle, mais on
    # reste hors du top-level par symétrie avec les autres seams).
    from . import access
    org_id = access.current_org(sub)
    if org_id is None:
        raise SessionError("aucune org de contexte — reconnecte-toi et réessaie.")
    db.set_member_api_key(sub, org_id, connector, context_id)


async def finalize(sub: str, connector: str, context_id: str, session_id: str) -> bool:
    """Vérifie le login sur la session vivante ; si OK, persiste le Context (= credential)
    et renvoie True. False = pas encore logué (l'appelant invite à réessayer)."""
    verify = _REGISTRY.get(connector)
    if verify is None:
        raise SessionError(f"{connector} n'est pas un connecteur à session navigateur.")
    # La session DOIT avoir été émise par `start()` pour CE sub (anti-IDOR) : on ne
    # persiste jamais un Context tiers passé à la main.
    key = (sub, context_id, session_id)
    if _PENDING.get(key, 0.0) < time.monotonic():
        _PENDING.pop(key, None)
        raise SessionError("session de connexion inconnue ou expirée — relance « Connecter ».")
    try:
        ok = await verify(session_id)
    except Exception:  # noqa: BLE001 — détail loggué, jamais renvoyé (peut porter l'apiKey)
        logger.exception("session verify failed for %s", connector)
        raise SessionError("vérification de la session impossible — réessaie.")
    if not ok:
        return False
    await asyncio.to_thread(_persist, sub, connector, context_id, session_id)
    _PENDING.pop(key, None)
    return True
