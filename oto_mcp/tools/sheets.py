"""Google Sheets — surface oto-core (SheetsClient) exposée par-utilisateur, multi-compte.

Édition de feuilles de calcul appartenant au user (différent du datastore, qui est
un spine PG natif — ADR 0016). Scope `spreadsheets`. Compte par défaut ou ciblé
par `account` (email). Accès strictement per-user via OAuth.

**Surface consolidée (ADR 0047 §Amendement, appliqué au produit `sheets` du connecteur
`google`)** : un tool par OBJET métier, le verbe en paramètre `op` — `sheets_spreadsheet`
(metadata/read/write/clear), tous scopés par le MÊME `spreadsheet_id`, et `range` sur
trois d'entre eux. Le namespace ne change pas (`sheets_*`), le credential non plus (un
seul OAuth Google pour tous ses produits).

`sheets_create` reste SEUL : c'est la seule op qui ne prend pas de `spreadsheet_id`
(elle en PRODUIT un) et la seule qui prend `title` — ses paramètres ne recouvrent aucun
de ceux de ses voisines. Une variante disjointe pèse au schéma ce que pesait le tool
séparé ; et la fusionner rendrait `spreadsheet_id` optionnel sur des ops qui l'exigent,
c'est-à-dire déplacerait dans le corps une garde que la signature tient aujourd'hui.

⚠️ **Ce module ÉCRIT sur les données de l'utilisateur** : `op="write"` écrase la plage
visée et `op="clear"` en efface les valeurs. Deux conséquences câblées ici, pas seulement
documentées : le défaut de `op` est une LECTURE (`metadata`), et `range` n'a de valeur par
défaut QUE pour `op="read"` — une écriture ou un effacement sans plage explicite est
refusé, jamais élargi au tableau entier.
"""
from __future__ import annotations

import asyncio
from typing import Any, Literal, Optional

from fastmcp import FastMCP
from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access
from ..auth import google as google_oauth

# Ops de `sheets_spreadsheet`. Vérifiées AVANT toute construction de client : une op
# inconnue ne doit atteindre aucune méthode du client, jamais retomber sur un défaut.
_SPREADSHEET_OPS = ("metadata", "read", "write", "clear")
_SPREADSHEET_OPS_HINT = "op doit être 'metadata', 'read', 'write' ou 'clear'"

# Plage par défaut de la LECTURE seule (contrat historique de `sheets_read`). Ni
# l'écriture ni l'effacement n'en héritent : « toute la feuille » est un défaut
# acceptable pour lire, jamais pour écraser ou vider.
_READ_DEFAULT_RANGE = "A:ZZ"


def _client_for_user(account: Optional[str] = None):
    sub = access.current_user_sub_or_raise()
    try:
        creds = google_oauth.credentials_for(sub, account=account, service="sheets")
    except RuntimeError as e:
        raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))
    from oto.tools.google.sheets.lib.sheets_client import SheetsClient
    return SheetsClient(credentials=creds)


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


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


def _need(value, name: str, op: str):
    """Argument obligatoire pour CET op — erreur actionnable, jamais de fallback.

    Sur une op d'écriture, un fallback serait un dégât : `op="clear"` sans plage
    tomberait sur la plage par défaut de la lecture, donc viderait la feuille entière.
    """
    if value is None:
        raise _bad(f"op='{op}' requiert {name}")
    return value


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def sheets_create(title: str, account: Optional[str] = None) -> dict:
        """Create a new empty Google spreadsheet. Returns {id, title, url}.

        Args:
            title: the new spreadsheet's title.
            account: Google account (email) to act as — default account if omitted.
        """
        client = await _client_for_user_async(account)
        return await asyncio.to_thread(client.create, title)

    @mcp.tool()
    async def sheets_spreadsheet(
        spreadsheet_id: str,
        op: Literal["metadata", "read", "write", "clear"] = "metadata",
        range: Optional[str] = None,
        values: Optional[list[list[Any]]] = None,
        formatted: bool = True,
        append: bool = False,
        account: Optional[str] = None,
    ) -> dict:
        """An existing spreadsheet and the cells it holds — describe, read, write, clear.

        `op`:
        - **"metadata"** (default): get a spreadsheet's metadata: title + the
          sheets/tabs it contains (id, title, rows, cols).
        - **"read"**: read values from a range (A1 notation, e.g. 'Sheet1!A1:D20'
          or 'A:ZZ'). `range` omitted = 'A:ZZ'. Returns {rows: [[...], ...], count}.
        - **"write"**: write a 2-D array of values to a range (A1 notation required).
          `append`: False (default) OVERWRITES the range ; True appends rows after
          the existing data (no overwrite).
        - **"clear"**: clear all values in a range (keeps formatting). Destructive —
          `range` is required, it has no default here.

        Args:
            spreadsheet_id: the spreadsheet to act on (every op).
            op: metadata (default) | read | write | clear.
            range: A1 notation, e.g. 'Sheet1!A1:D20' or 'A:ZZ'. op="read" — optional
                ('A:ZZ' if omitted) ; op="write"/"clear" — REQUIRED.
            values: op="write" — the 2-D array of values to write.
            formatted: op="read" — True = display strings (FORMATTED_VALUE) ;
                False = raw values.
            append: op="write" — False (default) OVERWRITES the range ; True appends
                rows after the existing data (no overwrite).
            account: Google account (email) to act as — default account if omitted.
        """
        if op not in _SPREADSHEET_OPS:
            raise _bad(_SPREADSHEET_OPS_HINT)

        client = await _client_for_user_async(account)

        if op == "metadata":
            return await asyncio.to_thread(client.get_metadata, spreadsheet_id)
        if op == "read":
            render = "FORMATTED_VALUE" if formatted else "UNFORMATTED_VALUE"
            rows = await asyncio.to_thread(
                client.read, spreadsheet_id, range or _READ_DEFAULT_RANGE, render)
            return {"rows": rows, "count": len(rows)}
        if op == "write":
            _need(range, "range", op)
            _need(values, "values", op)
            if append:
                return await asyncio.to_thread(
                    client.append, spreadsheet_id, range, values)
            return await asyncio.to_thread(client.write, spreadsheet_id, range, values)
        if op == "clear":
            return await asyncio.to_thread(
                client.clear, spreadsheet_id, _need(range, "range", op))
        # Inatteignable tant que `_SPREADSHEET_OPS` et les branches ci-dessus disent la
        # même chose — et c'est bien pourquoi la garde reste : une op ajoutée au tuple
        # sans sa branche tomberait sinon dans la DERNIÈRE, donc effacerait des cellules.
        raise _bad(_SPREADSHEET_OPS_HINT)
