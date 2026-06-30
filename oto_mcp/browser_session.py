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
from typing import Awaitable, Callable

from . import browserbase, db

# verify(session_id) -> bool : True si la session est bel et bien loguée (vérifié sur
# la session VIVANTE, jamais sur un export de cookie — cf. leçons ADR 0026).
Verify = Callable[[str], Awaitable[bool]]

_REGISTRY: dict[str, Verify] = {}


class SessionError(RuntimeError):
    """Erreur actionnable du flux de connexion (Browserbase indispo, vérif KO…)."""


def register(connector: str, verify: Verify) -> None:
    """Déclare un connecteur à session navigateur + sa vérification de login."""
    _REGISTRY[connector] = verify


def is_session_connector(connector: str) -> bool:
    return connector in _REGISTRY


def start() -> dict:
    """Ouvre un Context + une session keep-alive et renvoie la Live View interactive.
    BLOQUANT (HTTP Browserbase synchrone) → appeler via `asyncio.to_thread` depuis une
    route async. Lève `SessionError` si Browserbase n'est pas configuré côté plateforme."""
    if not browserbase.is_configured():
        raise SessionError("Browserbase non configuré côté plateforme "
                           "(BROWSERBASE_API_KEY / BROWSERBASE_PROJECT_ID).")
    try:
        context_id = browserbase.create_context()
        sess = browserbase.start_session(context_id, keep_alive=True, timeout=900)
        live = browserbase.live_view_url(sess["id"])
    except browserbase.BrowserbaseError as e:
        raise SessionError(f"Browserbase : {e}")
    return {"live_view_url": live, "context_id": context_id, "session_id": sess["id"]}


def _persist(sub: str, connector: str, context_id: str, session_id: str) -> None:
    browserbase.release_session(session_id)        # libère → persiste le Context
    db.set_user_api_key(sub, connector, context_id)


async def finalize(sub: str, connector: str, context_id: str, session_id: str) -> bool:
    """Vérifie le login sur la session vivante ; si OK, persiste le Context (= credential)
    et renvoie True. False = pas encore logué (l'appelant invite à réessayer)."""
    verify = _REGISTRY.get(connector)
    if verify is None:
        raise SessionError(f"{connector} n'est pas un connecteur à session navigateur.")
    try:
        ok = await verify(session_id)
    except Exception as e:  # noqa: BLE001 — toute panne de vérif = message actionnable
        raise SessionError(f"vérification de la session impossible ({e}).")
    if not ok:
        return False
    await asyncio.to_thread(_persist, sub, connector, context_id, session_id)
    return True
