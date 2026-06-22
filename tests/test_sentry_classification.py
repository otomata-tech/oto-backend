"""Politique de bruit Sentry : un refus client amont (4xx) n'est pas un bug backend.

Garde-fou de la classification PAR TYPE de `sentry_setup` (ADR : Sentry = défauts du
code ; les 4xx d'API tierces vivent dans le backlog `tool_calls`, pas dans Sentry).
"""
from oto_mcp.sentry_setup import (
    _before_send,
    _is_managed_connector_error,
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


def test_real_bugs_and_5xx_still_reported():
    assert not _is_managed_connector_error(_Upstream(503))   # amont cassé, ops-worthy
    assert not _is_managed_connector_error(KeyError("sub"))  # vrai bug code
    # plus de match par string : un Exception nu sans statut n'est PAS classé géré
    assert not _is_managed_connector_error(Exception("HTTP 422: x"))


def test_before_send_drops_only_4xx():
    assert _before_send({}, {"exc_info": (_Upstream, _Upstream(422), None)}) is None
    ev = {"k": "v"}
    assert _before_send(ev, {"exc_info": (_Upstream, _Upstream(500), None)}) is ev
    assert _before_send(ev, {"exc_info": (KeyError, KeyError("x"), None)}) is ev
    assert _before_send(ev, None) is ev  # event REST sans exc_info → conservé
