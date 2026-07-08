"""Contrat d'erreur uniforme rendu à l'agent (D2, oto-backend#124).

Vérifie la taxonomie `error_taxonomy.classify`/`scrub` (code machine + retryable +
message scrubbé) et le `ErrorEnvelopeMiddleware` qui réécrit toute exception de tool
en `McpError` structurée sans fuite (stacktrace / route interne / id technique).
"""
import asyncio

import pytest
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INTERNAL_ERROR, INVALID_PARAMS, INVALID_REQUEST

from oto_mcp.error_taxonomy import ErrorInfo, classify, jsonrpc_code, scrub
from oto_mcp.middleware import ErrorEnvelopeMiddleware


class _Upstream(Exception):
    """Mime UpstreamHTTPError (porte .status_code)."""

    def __init__(self, status_code, msg=""):
        super().__init__(msg)
        self.status_code = status_code


def _chained(top, cause):
    try:
        try:
            raise cause
        except Exception as c:
            raise top from c
    except Exception as e:
        return e


# --- scrub : anti-fuite -------------------------------------------------------

def test_scrub_net_err_replaced_whole():
    out = scrub("Error: net::ERR_NAME_NOT_RESOLVED at linkedin.com")
    assert "net::ERR" not in out
    assert "réseau amont" in out


def test_scrub_strips_internal_route():
    out = scrub("Cannot GET /api/v1/linkedin/inmail/balance")
    assert "/api/v1" not in out
    assert "[route interne]" in out


def test_scrub_strips_long_technical_id():
    out = scrub("account gPqR8s2K_uV3wX4yZ5aB6cD not found")
    assert "gPqR8s2K_uV3wX4yZ5aB6cD" not in out
    assert "[id]" in out


def test_scrub_keeps_human_part():
    assert scrub("Numéro de téléphone invalide.") == "Numéro de téléphone invalide."


# --- classify : catégorie + retryable ----------------------------------------

def test_mcperror_user_input_kept_and_typed():
    e = McpError(ErrorData(code=INVALID_PARAMS, message="Compte LinkedIn non connecté."))
    info = classify(e)
    assert info.code == "invalid_input"
    assert info.retryable is False
    assert info.message == "Compte LinkedIn non connecté."  # message curé préservé


def test_mcperror_invalid_request_is_input():
    info = classify(McpError(ErrorData(code=INVALID_REQUEST, message="Org inconnue.")))
    assert info.code == "invalid_input"


def test_mcperror_internal_kept_but_typed_internal():
    info = classify(McpError(ErrorData(code=INTERNAL_ERROR, message="boom curé")))
    assert info.code == "internal"
    assert info.retryable is False


def test_upstream_404_is_not_found_and_scrubbed():
    info = classify(_Upstream(404, "Unipile 404: Cannot GET /api/v1/linkedin/inmail/balance"))
    assert info.code == "not_found"
    assert info.retryable is False
    assert "/api/v1" not in info.message  # route interne scrubbée


def test_upstream_401_is_not_authorized():
    info = classify(_Upstream(401, "unauthorized"))
    assert info.code == "not_authorized"
    assert info.retryable is False
    assert info.hint


def test_upstream_429_is_rate_limited_retryable():
    info = classify(_Upstream(429, "too many requests"))
    assert info.code == "rate_limited"
    assert info.retryable is True


def test_upstream_422_is_4xx_non_retryable():
    info = classify(_Upstream(422, "phone invalide"))
    assert info.code == "upstream_4xx"
    assert info.retryable is False


def test_upstream_503_is_5xx_retryable():
    info = classify(_Upstream(503, "service down"))
    assert info.code == "upstream_5xx"
    assert info.retryable is True


def test_upstream_504_is_timeout_retryable():
    info = classify(_Upstream(504))
    assert info.code == "upstream_timeout"
    assert info.retryable is True


def test_asyncio_timeout_is_typed_retryable():
    info = classify(asyncio.TimeoutError())
    assert info.code == "upstream_timeout"
    assert info.retryable is True


def test_timeout_by_message():
    info = classify(RuntimeError("Unipile: erreur réseau (Read timed out)."))
    assert info.code == "upstream_timeout"
    assert info.retryable is True


def test_raw_exception_is_internal_and_does_not_echo():
    # Une exception brute (net::ERR Chromium, HttpError…) ne DOIT PAS fuiter son str.
    info = classify(RuntimeError("net::ERR_NAME_NOT_RESOLVED at https://internal.host/x"))
    assert info.code == "internal"
    assert "net::ERR" not in info.message
    assert "internal.host" not in info.message


def test_classify_walks_chain():
    # fastmcp emballe dans un ToolError → on remonte jusqu'à l'amont.
    info = classify(_chained(Exception("Error calling tool 'unipile_inmail_balance'"),
                             _Upstream(404, "Cannot GET /api/v1/x")))
    assert info.code == "not_found"


def test_jsonrpc_code_mapping():
    assert jsonrpc_code(ErrorInfo("invalid_input", False, "x")) == INVALID_PARAMS
    assert jsonrpc_code(ErrorInfo("upstream_5xx", True, "x")) == INTERNAL_ERROR
    assert jsonrpc_code(ErrorInfo("internal", False, "x")) == INTERNAL_ERROR


# --- middleware : réécriture en McpError structurée ---------------------------

class _Ctx:
    class _Msg:
        name = "unipile_inmail_balance"
    message = _Msg()


async def _run(exc):
    mw = ErrorEnvelopeMiddleware()

    async def call_next(_ctx):
        raise exc

    with pytest.raises(McpError) as ei:
        await mw.on_call_tool(_Ctx(), call_next)
    return ei.value


def test_middleware_wraps_raw_into_structured_mcperror():
    err = asyncio.run(_run(RuntimeError("net::ERR_NAME_NOT_RESOLVED"))).error
    assert err.code == INTERNAL_ERROR
    assert "net::ERR" not in err.message
    assert err.data["oto"]["code"] == "internal"
    assert err.data["oto"]["retryable"] is False


def test_middleware_preserves_curated_mcperror_message():
    src = McpError(ErrorData(code=INVALID_PARAMS, message="Compte non connecté."))
    err = asyncio.run(_run(src)).error
    assert err.code == INVALID_PARAMS
    assert err.message == "Compte non connecté."
    assert err.data["oto"] == {"code": "invalid_input", "retryable": False}


def test_middleware_upstream_carries_hint_and_retryable():
    err = asyncio.run(_run(_Upstream(429, "slow down"))).error
    assert err.data["oto"]["code"] == "rate_limited"
    assert err.data["oto"]["retryable"] is True
    assert err.data["oto"]["hint"]
