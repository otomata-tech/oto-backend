"""Google Calendar — surface oto-core (CalendarClient) exposée par-utilisateur, multi-compte.

Même substrat que Gmail/Tasks : chaque user connecte un ou plusieurs comptes
Google sur `https://app.oto.ninja/` (flow OAuth unifié, scope `calendar` inclus).
Les tools `calendar_*` agissent sur le compte par défaut, ou sur le compte ciblé
par le paramètre `account` (l'adresse email). Pas de clé plateforme : accès
strictement per-user via OAuth.

Surface regroupée (4 tools) : lister les agendas, lister/chercher des événements
sur une plage, lire un événement, en créer un. Les raccourcis « aujourd'hui /
prochains jours » se font en passant `time_min`/`time_max`.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, google_oauth


def _client_for_user(account: Optional[str] = None):
    """Instancie un CalendarClient oto-core avec les credentials du user.

    `account` (email) cible un compte précis ; None = compte par défaut.
    Lève une McpError actionnable si aucun compte Google n'est connecté.
    """
    sub = access.current_user_sub_or_raise()
    try:
        creds = google_oauth.credentials_for(sub, account=account)
    except RuntimeError as e:
        raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))
    from oto.tools.google.calendar.lib.calendar_client import CalendarClient
    return CalendarClient(credentials=creds)


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def calendar_calendars(account: Optional[str] = None) -> dict:
        """List the Google calendars the user can access.

        Args:
            account: email of the Google account to use (default if omitted).

        Returns {calendars: [{id, summary, primary, accessRole}]}. Use an `id`
        as the `calendar_id` argument of the other calendar_* tools; omit it for
        the user's main calendar ('primary').
        """
        client = _client_for_user(account)
        calendars = await asyncio.to_thread(client.list_calendars)
        return {"calendars": calendars, "count": len(calendars)}

    @mcp.tool()
    async def calendar_events(
        calendar_id: str = "primary",
        time_min: Optional[str] = None,
        time_max: Optional[str] = None,
        query: Optional[str] = None,
        max_results: int = 20,
        account: Optional[str] = None,
    ) -> dict:
        """List events from a calendar over a time range (ordered by start).

        Args:
            calendar_id: calendar id (default 'primary').
            time_min: lower bound, ISO 8601 (e.g. '2026-06-18T00:00:00Z'). For
                "today" pass today's 00:00; for "next 7 days" pass now.
            time_max: upper bound, ISO 8601. Omit either bound to leave it open.
            query: free-text search over event fields.
            max_results: max events to return (default 20).
            account: email of the Google account to use (default if omitted).

        Returns {events: [{id, summary, start, end, ...}], count}.
        """
        client = _client_for_user(account)
        events = await asyncio.to_thread(
            client.list_events, calendar_id, time_min, time_max, max_results, query,
        )
        return {"events": events, "count": len(events)}

    @mcp.tool()
    async def calendar_get_event(
        event_id: str, calendar_id: str = "primary", account: Optional[str] = None,
    ) -> dict:
        """Get a single calendar event by id (detailed).

        Args:
            event_id: the event id (from calendar_events).
            calendar_id: calendar id (default 'primary').
            account: email of the Google account to use (default if omitted).
        """
        client = _client_for_user(account)
        return await asyncio.to_thread(client.get_event, event_id, calendar_id)

    @mcp.tool()
    async def calendar_create_event(
        summary: str,
        start: str,
        end: Optional[str] = None,
        description: Optional[str] = None,
        location: Optional[str] = None,
        all_day: bool = False,
        calendar_id: str = "primary",
        account: Optional[str] = None,
    ) -> dict:
        """Create a calendar event.

        Args:
            summary: event title.
            start: start time — ISO 8601 datetime (timed) or 'YYYY-MM-DD' (all-day).
            end: end time. If omitted, defaults to start + 1h (timed) or same day (all-day).
            description: event description.
            location: event location.
            all_day: treat start/end as dates (YYYY-MM-DD).
            calendar_id: calendar id (default 'primary').
            account: email of the Google account to use (default if omitted).
        """
        client = _client_for_user(account)
        return await asyncio.to_thread(
            client.create_event, summary, start, end, description, location, all_day, calendar_id,
        )
