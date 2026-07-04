"""Normalisation des erreurs Google Chat (oto-backend#110) : un HttpError brut
devient un message actionnable, jamais un stacktrace."""
from oto_mcp.tools import chat as C


class _Resp:
    def __init__(self, status):
        self.status = status


class _FakeHttpError(Exception):
    def __init__(self, status, message, reason="Error"):
        self.resp = _Resp(status)
        self.content = ('{"error":{"message":%r}}' % message).replace("'", '"').encode()
        self.reason = reason


def _msg(err):
    return _err.error.message if (_err := C._http_error(err)) else ""


def test_chat_api_not_enabled_404_is_actionable():
    e = _FakeHttpError(404, "Google Chat app not found. To create a Chat app, you must turn on the Chat API.")
    m = _msg(e)
    assert "API Google Chat doit être" in m and "404" not in m  # message métier, pas le code brut
    assert "HttpError" not in m and "<" not in m                # pas de repr d'exception


def test_generic_http_error_surfaces_status_and_reason():
    e = _FakeHttpError(403, "Caller does not have permission")
    m = _msg(e)
    assert "HTTP 403" in m and "permission" in m
    assert "HttpError" not in m
