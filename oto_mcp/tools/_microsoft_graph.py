"""What the Microsoft 365 SERVICE tools share — SharePoint & OneDrive, Outlook, Outlook
Calendar, Teams, OneNote, the directory: the caller's token for ONE service, Graph's 4xx turned
into a named refusal, the argument checks, the markdown body.

A helper, not a connector: no `register()` (`tests/test_capabilities_drift.py`). Each
service module keeps its own `_client() -> <Client>` — the version-skew probe
(`tests/test_tools_client_methods_exist.py`) reads the concrete class there.

The token: `auth/microsoft.access_token_for(sub, service)` resolves the account the call
designates (`_account=`, the project's pin, the only one, the default) and refuses an
account that has not authorized THIS service, naming its card — before any Graph call.
"""
from __future__ import annotations

from typing import Callable, NoReturn, Optional, TypeVar

from mcp.types import ErrorData, INVALID_PARAMS

from .. import access
from ..mcp_errors import McpError

T = TypeVar("T")


def bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def need(value, name: str, op: str):
    """Required argument for THIS op — an empty value counts as absent (an empty id
    list would pass for a success while nothing was asked)."""
    if value is None or (isinstance(value, (str, list)) and not value) or (
            isinstance(value, str) and not value.strip()):
        raise bad(f"op='{op}' requires `{name}`.")
    return value


def refuse_ignored(op: str, **provided) -> None:
    """An argument provided that THIS op does not use is an intent error."""
    for name, value in provided.items():
        if value is not None:
            raise bad(f"op='{op}' does not use `{name}`.")


def raw(objet: dict) -> dict:
    """`full=True`: the Graph object as upstream delivers it."""
    return objet


def token(service: str, *, renew: bool = False) -> str:
    """The caller's access token for `service`, on the account the call designates.
    `renew=True` asks Entra for a new one whatever the cache holds (a permission granted
    since the cached token was issued — `access_token_for`)."""
    from oto.tools.microsoft import MicrosoftAuthError

    from ..auth import microsoft as ms_auth

    sub = access.current_user_sub_or_raise()
    try:
        if renew:
            return ms_auth.access_token_for(sub, service, renew=True)
        return ms_auth.access_token_for(sub, service)
    except (RuntimeError, MicrosoftAuthError) as e:
        raise bad(str(e))


def upstream_message(e, label: str, conflict: Optional[str] = None) -> str:
    """Graph's refusal, said for the agent: what it means, and the gesture."""
    status = e.status_code
    body = e.body if isinstance(e.body, dict) else {}
    detail = str((body.get("error") or {}).get("message") or e.body or "")[:400]
    if status == 401:
        return (f"Microsoft Graph rejects the token (HTTP 401): reconnect from "
                f"your connectors, \"{label}\". {detail}").strip()
    if status == 403:
        return (f"Microsoft Graph denies access (HTTP 403): this Microsoft account does "
                f"not have rights on this item, or its organization blocks oto. "
                f"{detail}").strip()
    if status == 404:
        return f"Microsoft Graph: not found (HTTP 404). {detail}".strip()
    if status == 409 and conflict:
        return f"Microsoft Graph: {conflict} (HTTP 409). {detail}".strip()
    return f"Microsoft Graph rejected the request (HTTP {status}): {detail}"


def raise_refusal(e, label: str, conflict: Optional[str] = None) -> NoReturn:
    """Raises what a Graph error becomes for the agent: a 4xx → named refusal; 429 and
    5xx stay what they are — the error taxonomy classifies them as retryable."""
    if 400 <= e.status_code < 500 and e.status_code != 429:
        raise bad(upstream_message(e, label, conflict)) from e
    raise e


def run(fn: Callable[[], T], label: str, conflict: Optional[str] = None) -> T:
    """`fn()`, a Graph error turned into its `refusal`; a `ValueError` of the lib (an
    argument it refuses before any write) → its message."""
    from oto.tools.common.errors import UpstreamHTTPError

    try:
        return fn()
    except UpstreamHTTPError as e:
        raise_refusal(e, label, conflict)
    except ValueError as e:
        raise bad(str(e))


def markdown_html(text: str, *, cid: bool = False) -> str:
    """A body written in markdown, as HTML — the very rendering of `gmail` (op=draft)
    and `email_send` (`markdown_assaini.rendu_markdown_assaini`: tables, fenced code,
    lists, `http(s)` images), so that an agent writes the same way everywhere. `cid=True`
    also keeps a `cid:` image — a message's inline attachment (`outlook`)."""
    from ..markdown_assaini import rendu_markdown_assaini

    return rendu_markdown_assaini(text, images=True, cid=cid)
