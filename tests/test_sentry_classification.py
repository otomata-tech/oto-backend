"""Politique de bruit Sentry : un refus client amont (4xx) n'est pas un bug backend.

Garde-fou de la classification PAR TYPE de `sentry_setup` (ADR : Sentry = défauts du
code ; les 4xx d'API tierces vivent dans le backlog `tool_calls`, pas dans Sentry).
"""
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INTERNAL_ERROR, INVALID_PARAMS, INVALID_REQUEST

from oto_mcp.sentry_setup import (
    _before_send,
    _is_expected_error,
    _is_managed_connector_error,
    _is_user_input_error,
    _upstream_status,
)


class _Upstream(Exception):
    """Mime oto.tools.common.UpstreamHTTPError (porte .status_code)."""

    def __init__(self, status_code):
        self.status_code = status_code


class _Resp:
    def __init__(self, status_code):
        self.status_code = status_code


class _HTTPStatusError(Exception):
    """Mime httpx.HTTPStatusError / requests.exceptions.HTTPError (.response)."""

    def __init__(self, status_code):
        self.response = _Resp(status_code)


class _NinjaError(RuntimeError):
    """Erreur connecteur typée maison (.status)."""

    def __init__(self, status):
        self.status = status


def _chained(top, cause):
    try:
        try:
            raise cause
        except Exception as c:
            raise top from c
    except Exception as e:
        return e


def test_upstream_status_extraction():
    assert _upstream_status(_Upstream(422)) == 422
    assert _upstream_status(_HTTPStatusError(404)) == 404
    assert _upstream_status(_NinjaError(400)) == 400
    assert _upstream_status(KeyError("x")) is None


def test_4xx_are_managed_connector_errors():
    assert _is_managed_connector_error(_Upstream(422))
    assert _is_managed_connector_error(_Upstream(401))
    assert _is_managed_connector_error(_HTTPStatusError(403))
    assert _is_managed_connector_error(_NinjaError(400))
    # fastmcp emballe dans un ToolError : on remonte la chaîne jusqu'à l'amont
    assert _is_managed_connector_error(
        _chained(Exception("Error calling tool 'folk_update'"), _Upstream(422))
    )


def test_real_oto_core_connector_errors_are_classified():
    # Seam oto-core (>=1.14.0) : UnipileError porte status_code aux sites de
    # raise HTTP ; ZohoAuthError (refus OAuth en HTTP 200) = 401 synthétique.
    from oto.tools.unipile.client import UnipileError
    from oto.tools.zoho import ZohoAuthError
    assert _is_managed_connector_error(UnipileError("Unipile 422: x", status_code=422))
    assert _is_managed_connector_error(ZohoAuthError("Zoho OAuth error: invalid_client"))
    assert not _is_managed_connector_error(UnipileError("Unipile 500: x", status_code=500))
    assert not _is_managed_connector_error(UnipileError("Unipile: erreur réseau (Timeout)."))


def test_real_bugs_and_5xx_still_reported():
    assert not _is_managed_connector_error(_Upstream(503))   # amont cassé, ops-worthy
    assert not _is_managed_connector_error(KeyError("sub"))  # vrai bug code
    # plus de match par string : un Exception nu sans statut n'est PAS classé géré
    assert not _is_managed_connector_error(Exception("HTTP 422: x"))


def test_mcp_input_errors_are_user_config_noise():
    # « pose ta clé », « connecte ton compte », param/org invalide → refus user
    assert _is_user_input_error(McpError(ErrorData(code=INVALID_PARAMS, message="Aucune clé `hunter`")))
    assert _is_user_input_error(McpError(ErrorData(code=INVALID_REQUEST, message="x")))
    # remonte la chaîne : ToolError fastmcp emballant la McpError de config
    assert _is_user_input_error(
        _chained(Exception("Error calling tool 'gmail_get'"),
                 McpError(ErrorData(code=INVALID_PARAMS, message="Aucun compte Google connecté")))
    )


def test_mcp_internal_and_real_bugs_still_reported():
    # INTERNAL_ERROR = vrai défaut serveur, pas un refus user
    assert not _is_user_input_error(McpError(ErrorData(code=INTERNAL_ERROR, message="boom")))
    # corruption coffre (clé périmée) = Runtimeable, on VEUT la voir
    assert not _is_expected_error(RuntimeError("credential indéchiffrable (clé périmée)"))
    assert not _is_expected_error(AttributeError("module 'oto_mcp.db' has no attribute 'has_option_comp'"))


def test_expected_error_unifies_both_classes():
    assert _is_expected_error(_Upstream(422))                                  # 4xx amont
    assert _is_expected_error(McpError(ErrorData(code=INVALID_PARAMS, message="x")))  # config user
    assert not _is_expected_error(_Upstream(503))
    assert not _is_expected_error(KeyError("sub"))


def test_before_send_drops_managed_errors():
    assert _before_send({}, {"exc_info": (_Upstream, _Upstream(422), None)}) is None
    e = McpError(ErrorData(code=INVALID_PARAMS, message="connecte ton compte"))
    assert _before_send({}, {"exc_info": (McpError, e, None)}) is None
    ev = {"k": "v"}
    assert _before_send(ev, {"exc_info": (_Upstream, _Upstream(500), None)}) is ev
    assert _before_send(ev, {"exc_info": (KeyError, KeyError("x"), None)}) is ev
    assert _before_send(ev, None) is ev  # event REST sans exc_info → conservé


def _validation_error():
    """Construit une vraie ValidationError pydantic (args rejetés)."""
    from pydantic import BaseModel, ValidationError

    class _M(BaseModel):
        x: int

    try:
        _M(x="pas un entier")
    except ValidationError as e:
        return e


def test_pydantic_validation_error_is_expected():
    # args rejetés (le LLM a passé de mauvais paramètres) = refus d'entrée, pas un bug
    ve = _validation_error()
    assert _is_expected_error(ve)
    assert _before_send({"k": "v"}, {"exc_info": (type(ve), ve, None)}) is None
    # chaînée (fastmcp emballe dans un ToolError) → toujours reconnue
    wrapped = RuntimeError("Error calling tool")
    wrapped.__cause__ = ve
    assert _is_expected_error(wrapped)
    # une vraie exception code reste reportée
    assert not _is_expected_error(KeyError("sub"))
