"""Return from Slack's install consent — hand-written route, like the Meta Ads and
WordPress returns, and for the same reason: Slack sends the user's BROWSER back here,
with no auth header (the identity comes from the signed `state`), and the response is
a 302 to the front end. Declared as an exception in
`tests/test_rest_modules_are_capabilities.py`.

In order: state read, state CONSUMED (single use), provider refusal, right to write at
the tier RE-CHECKED (`forbidden`), then exchange and storage. The `code` and the
tokens never enter a log line nor the return URL.

`POST /api/slack/events` — the app's Events API endpoint (`@Tulina`, DMs, uninstall),
hand-written for the same reason as `api/hooks.py`: a free JSON body, signed by Slack,
no JWT. It only adapts; `slack_events.recevoir` decides.
"""
from __future__ import annotations

import json
import logging
from typing import Awaitable, Callable

from fastmcp.server.auth.providers.jwt import JWTVerifier
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from .. import slack_events
from ..auth import flow as oauth_flow
from ..auth import slack as slack_auth
from .hooks import _lire_borne

logger = logging.getLogger(__name__)

EVENTS_PATH = "/api/slack/events"

AuthFn = Callable[..., Awaitable[tuple[str | None, JSONResponse | None]]]


def make_routes(
    verifier: JWTVerifier,
    authenticate: AuthFn,
    json_response: Callable[..., JSONResponse],
    json_error: Callable[..., JSONResponse],
    options_handler: Callable[[Request], Awaitable[Response]],
) -> list[Route]:

    def _retour(etat: str, parsed: dict | None) -> str:
        parsed = parsed or {}
        return oauth_flow.connector_return_url(
            parsed.get("app") or "", slack_auth.CONNECTOR, etat, org=parsed.get("org"))

    async def callback(request: Request) -> Response:
        q = request.query_params
        parsed = slack_auth.read_state(q.get("state"))
        if not parsed:
            logger.info("slack: consent return without a readable state")
            return RedirectResponse(_retour("error", None), status_code=302)
        if not await run_in_threadpool(oauth_flow.consume_state, slack_auth.AUD, parsed):
            logger.warning("slack: consent return with a replayed state (sub=%s)",
                           parsed["sub"])
            return RedirectResponse(_retour("error", parsed), status_code=302)
        erreur, code = q.get("error"), q.get("code")
        if erreur or not code:
            logger.info("slack: consent not completed (sub=%s, reason=%s)",
                        parsed["sub"], erreur or "code missing")
            return RedirectResponse(_retour(oauth_flow.etat_sans_code(erreur), parsed),
                                    status_code=302)
        if not await run_in_threadpool(slack_auth.still_allowed, parsed):
            logger.warning("slack: consent return refused: %s no longer has the right "
                           "to write at tier %s (org=%s group=%s)", parsed["sub"],
                           parsed["scope"], parsed["org"], parsed.get("group"))
            return RedirectResponse(_retour("forbidden", parsed), status_code=302)

        def _echanger_et_ranger() -> str:
            return slack_auth.finish(parsed, slack_auth.exchange(code))

        try:
            # Synchronous HTTP + DB off the loop (oto-backend#867).
            await run_in_threadpool(_echanger_et_ranger)
        except oauth_flow.OAuthExchangeRefused as e:
            logger.info("slack: exchange refused (sub=%s org=%s): %s",
                        parsed["sub"], parsed["org"], e)
            return RedirectResponse(_retour("error", parsed), status_code=302)
        except Exception:
            # The traceback, never the `code` nor a token.
            logger.exception("slack: consent return failed (sub=%s org=%s)",
                             parsed["sub"], parsed["org"])
            return RedirectResponse(_retour("error", parsed), status_code=302)
        return RedirectResponse(_retour("connected", parsed), status_code=302)

    async def events(request: Request) -> Response:
        """Slack's deliveries. 200 for everything Slack sent properly, refusals
        included (the person is told in Slack): a non-2xx makes Slack retry, and an
        endpoint that keeps failing gets the app's events switched off.
        `X-Slack-No-Retry` on our own refusals — a retry would not change them."""
        brut = await _lire_borne(request)
        if brut is None:
            return JSONResponse({"error": "payload_too_large"}, status_code=413,
                                headers={"X-Slack-No-Retry": "1"})
        try:
            corps = json.loads(brut) if brut.strip() else None
        except ValueError:
            corps = None
        try:
            rendu = await run_in_threadpool(slack_events.recevoir, brut,
                                            request.headers, corps)
        except slack_events.Refus as e:
            entetes = {} if e.statut == 503 else {"X-Slack-No-Retry": "1"}
            return JSONResponse({"error": e.code}, status_code=e.statut,
                                headers=entetes)
        except Exception:  # noqa: BLE001 — logged whole; Slack retries a 500
            logger.exception("slack events: delivery failed on our side")
            return JSONResponse({"error": "slack_event_failed"}, status_code=500)
        return JSONResponse(rendu, status_code=200)

    return [Route(slack_auth.CALLBACK_PATH, callback, methods=["GET"]),
            Route(EVENTS_PATH, events, methods=["POST"])]
