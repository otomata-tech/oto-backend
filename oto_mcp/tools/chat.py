"""Google Chat — surface oto-core (ChatClient) exposée par-utilisateur, multi-compte.

Lister les espaces (rooms + DM), lire les messages, poster (dans un espace ou en
DM à un user). Scopes **restricted** `chat.spaces.readonly` + `chat.messages`.
Compte par défaut ou ciblé par `account`. Per-user via OAuth.

**Surface consolidée (ADR 0047 §Amendement, appliqué au produit Google Chat)** : un
tool par OBJET métier, le verbe en paramètre `op` — `chat_message` (list/send, le
message d'un espace ou d'un DM). `chat_spaces` reste SEUL : c'est la DÉCOUVERTE qui
PRODUIT le `space` que l'autre consomme, son paramètre de filtre (`space_type`) n'a
aucun sens sur une op de message, et il ne prend pas de `space` — ses params ne
recouvrent pas ceux de son voisin (même cas que `zoho_modules`). 3 tools → 2.

⚠️ `chat_message(op="send")` POSTE pour de bon dans un espace Google Chat réel, sous
l'identité de l'utilisateur (pas un bot). D'où : le défaut est `op="list"` (une
LECTURE), aucun argument manquant ne retombe sur un envoi, et une destination
ambiguë (`space` ET `user`, ou ni l'un ni l'autre) est refusée au lieu d'être devinée.
"""
from __future__ import annotations

import asyncio
from typing import Literal, Optional

from fastmcp import FastMCP
from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access
from ..auth import google as google_oauth


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _need(value, name: str, op: str):
    """Argument obligatoire pour CET op — erreur actionnable, jamais de fallback."""
    if value is None:
        raise _bad(f"op='{op}' requiert {name}")
    return value


def _http_error(e) -> McpError:
    """Normalise un `googleapiclient.errors.HttpError` (stacktrace brut illisible)
    en message actionnable, aligné sur la famille messagerie (oto-backend#110).
    Le cas courant = l'API Google Chat non activée / le compte sans accès Chat →
    l'ancien retour était un `<HttpError 404 …>` opaque."""
    status = getattr(getattr(e, "resp", None), "status", None) or getattr(e, "status_code", None)
    detail = ""
    try:
        import json
        payload = json.loads(e.content.decode()) if getattr(e, "content", None) else {}
        detail = (payload.get("error") or {}).get("message") or ""
    # noqa: SILENT — corps d'erreur non-JSON : le message brut reste rendu
    except Exception:  # noqa: BLE001
        pass
    detail = detail or (getattr(e, "reason", None) or "").strip() or "erreur inconnue"
    low = detail.lower()
    if status == 404 and ("app not found" in low or "chat api" in low or "turn on" in low):
        msg = ("Google Chat n'est pas disponible pour ce compte : l'API Google Chat doit "
               "être activée côté Google (ou le compte n'a pas accès à Chat). "
               f"Détail : {detail}")
    else:
        msg = f"Google Chat a refusé la requête (HTTP {status}) : {detail}"
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


async def _call(fn, *args):
    """Exécute un appel client Chat hors boucle + traduit tout `HttpError` en erreur
    propre (jamais de stacktrace brut renvoyé à l'agent)."""
    from googleapiclient.errors import HttpError
    try:
        return await asyncio.to_thread(fn, *args)
    except HttpError as e:
        raise _http_error(e)


def _client_for_user(account: Optional[str] = None):
    sub = access.current_user_sub_or_raise()
    try:
        creds = google_oauth.credentials_for(sub, account=account, service="chat")
    except RuntimeError as e:
        raise _bad(str(e))
    from oto.tools.google.chat.lib.chat_client import ChatClient
    return ChatClient(credentials=creds)


_GOOGLE_CLIENT_TIMEOUT_S = 20
# oto-backend#867 lot 2 — voir gmail.py::_client_for_user_async pour la
# justification (même mécanisme de rafraîchissement de jeton, même méthode).
async def _client_for_user_async(account: Optional[str] = None):
    try:
        return await asyncio.wait_for(asyncio.to_thread(_client_for_user, account),
                                      timeout=_GOOGLE_CLIENT_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise _bad(f"Google n'a pas répondu dans les {_GOOGLE_CLIENT_TIMEOUT_S}s "
                   "(rafraîchissement de jeton) — réessaie.")


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def chat_spaces(
        space_type: Optional[str] = None, max_results: int = 100, account: Optional[str] = None,
    ) -> dict:
        """List the Google Chat spaces (rooms + DMs) the user belongs to.

        Returns {spaces: [{name, type, displayName, ...}], count}. Use a `name`
        ('spaces/XXXX') as the `space` argument of `chat_message`.

        Args:
            space_type: optional filter — "SPACE" (rooms) or "DIRECT_MESSAGE" (DMs).
            max_results: cap on spaces returned.
            account: email of the Google account to use (default if omitted).
        """
        client = await _client_for_user_async(account)
        filter_ = f'spaceType = "{space_type}"' if space_type else None
        spaces = await _call(client.list_spaces, filter_, max_results)
        return {"spaces": spaces, "count": len(spaces)}

    @mcp.tool()
    async def chat_message(
        op: Literal["list", "send"] = "list",
        space: Optional[str] = None,
        text: Optional[str] = None,
        user: Optional[str] = None,
        max_results: int = 20,
        account: Optional[str] = None,
    ) -> dict:
        """The messages of a Google Chat space — read them, or post one.

        `op`:
        - **"list"** (default): list recent messages in a space (most recent first).
          `space` = 'spaces/XXXX'.
        - **"send"**: post a Google Chat message — either into a space or as a DM to
          a user. Provide EITHER `space` OR `user`, not both.
          ⚠️ This WRITES: the message is really posted, under the user's own
          identity (not a bot), and cannot be unsent from here.

        Args:
            op: list (default) | send.
            space: target space resource name ('spaces/XXXX'), as returned by
                `chat_spaces` — required for op="list", and for op="send" into a
                room/space.
            text: op="send" — message text (basic formatting: *bold*, _italic_).
            user: op="send" — recipient email, sends a direct message (resolves the
                DM space). The DM space must ALREADY exist: Google Chat does not let
                a user create a brand-new DM space through the API — open the
                conversation once in Chat, then retry.
            max_results: op="list" — cap on messages returned.
            account: email of the Google account to use (default if omitted).
        """
        client = await _client_for_user_async(account)

        if op == "list":
            if space is None:
                raise _bad("op='list' requiert space ('spaces/XXXX', cf. chat_spaces) "
                           "— `user` ne vaut que pour op='send'.")
            messages = await _call(client.list_messages, space, max_results)
            return {"messages": messages, "count": len(messages)}

        if op == "send":
            if bool(space) == bool(user):
                raise _bad("op='send' requiert soit `space` (message dans un espace) "
                           "soit `user` (DM), pas les deux ni aucun.")
            _need(text, "text", op)
            if user:
                return await _call(client.send_dm, user, text)
            return await _call(client.send, space, text)

        raise _bad("op doit être 'list' ou 'send'")
