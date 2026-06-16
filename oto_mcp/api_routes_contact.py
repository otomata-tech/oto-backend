"""Endpoint public du formulaire de contact d'otomata.tech.

`POST /api/contact` — non authentifié (vitrine publique). Envoie le message au
studio via otomata-mailer (`email.send_contact_email`). Anti-spam : honeypot
`website` (champ piège masqué en CSS côté front, jamais rempli par un humain) +
bornes de longueur. Best-effort comme tout l'envoi mailer : si le bearer n'est
pas configuré, on renvoie 503 actionnable plutôt que de mentir un 200.
"""
from __future__ import annotations

import re
from typing import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import email

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MAX_NAME = 120
_MAX_EMAIL = 200
_MAX_MESSAGE = 5000


def make_routes(
    json_response: Callable[..., JSONResponse],
    json_error: Callable[..., JSONResponse],
    options_handler: Callable[[Request], Awaitable[Response]],
) -> list[Route]:

    async def contact(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return json_error(request, 400, "invalid_json")
        if not isinstance(body, dict):
            return json_error(request, 400, "invalid_json")

        # Honeypot : un bot remplit tous les champs → on absorbe en 200 silencieux.
        if (body.get("website") or "").strip():
            return json_response(request, {"ok": True})

        name = (body.get("name") or "").strip()
        sender = (body.get("email") or "").strip()
        message = (body.get("message") or "").strip()

        if not name or not sender or not message:
            return json_error(request, 400, "missing_field")
        if len(name) > _MAX_NAME or len(sender) > _MAX_EMAIL or len(message) > _MAX_MESSAGE:
            return json_error(request, 400, "too_long")
        if not _EMAIL_RE.match(sender):
            return json_error(request, 400, "invalid_email")

        if not email.send_contact_email(name, sender, message):
            # bearer mailer absent ou échec réseau → actionnable, pas de faux succès.
            return json_error(request, 503, "send_failed")
        return json_response(request, {"ok": True})

    return [
        Route("/api/contact", contact, methods=["POST"]),
        Route("/api/contact", options_handler, methods=["OPTIONS"]),
    ]
