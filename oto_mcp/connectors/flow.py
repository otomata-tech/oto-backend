"""The "connect" gesture of a connector — declared by its module, derived everywhere.

**The problem this closes.** Some connectors cannot be obtained by pasting fields:
they need a gesture outside the form (OAuth consent, browser session…).
Nothing DECLARED it, so each surface compensated in its own way — and always by the
connector NAME. The dashboard mounted the consent widget behind a
`['zoho','zohodesk','zohoanalytics'].includes(name)`; Salesforce, which has exactly
the same shape on the backend side (start capability, callback, the two
`status_hints` hooks, the `oauth_flow` factory), simply was not in it — so no button,
and a customer could not finish their connection. Adding one more name would have
worked for five minutes and grown the one thing that had to be removed.

**What the seam guarantees.** A connector declares its flow HERE, in its own module
(pattern `connector_verify` / `status_hints`). The catalog derives a descriptor of
SHAPE from it — which parameters the user must provide, what the gesture is called —
and the front renders a generic form + a button, without ever knowing a name.

**What the descriptor deliberately does NOT carry**: no URL, no capability key, no
tool name. `/api/connectors` is served without authentication; a descriptor that
published its internal paths would make the attack surface a side effect of the
documentation. The path is FIXED and known to the client
(`POST /api/me/connectors/{name}/connect`), the name travels as a path parameter.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FlowParam:
    """A value the user must provide to start the flow.

    `options` non-empty ⟹ closed list (the front renders a select). This is the SINGLE
    HOME of these values: the Zoho region was previously copied four times, including
    a wrong version in the registry label (an `sa` that the code rejects)."""
    name: str
    label: str
    options: tuple[tuple[str, str], ...] = ()      # (value, label)
    default: str = ""
    required: bool = True
    help: str = ""

    def describe(self) -> dict:
        return {
            "name": self.name, "label": self.label, "required": self.required,
            "default": self.default, "help": self.help,
            "options": [{"value": v, "label": lbl} for v, lbl in self.options],
        }


@dataclass(frozen=True)
class FlowStart:
    """What a flow returns — the SAME shape for all, whatever the connector.

    `me.connector_connect` is the seam that lets the front plug in a connector
    without knowing which one. Its output did not follow: Zoho echoed `{auth_url,
    connector}`, Salesforce `{auth_url, scope}`, and the common guarantee was only a
    type comment (`-> {"auth_url": …}`) that nothing enforced — a third flow
    would have invented its third key. The published contract had to be declared open
    with two optional fields: it documented the inconsistency.

    **The first level is CLOSED, and it is the only one the caller may write**:
    `auth_url` to open in a browser, nothing else. Whatever a connector wants to
    echo in addition goes down into `details`, a NAMED field whose content is its
    own property — same rule as on input (a connector's specificity lives in its
    module) and same choice as `ResolvedCredential.config`. Freezing the union of
    keys would have made the common contract grow with every flow added; here it no
    longer moves.

    `details` is NEVER required to act: a client that reads it accepts knowing the
    connector it plugs in, which the seam does not ask of it."""
    auth_url: str
    details: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"auth_url": self.auth_url, "details": dict(self.details)}


@dataclass(frozen=True)
class Flow:
    connector: str
    start: Callable[..., FlowStart]  # (ctx, values) -> FlowStart
    params: tuple[FlowParam, ...] = field(default_factory=tuple)
    label: str = "Connect"
    # Path of the consent return. The FULL URL is derived from it on read
    # (`callback_url`), never hard-coded: it depends on the environment, and a prose
    # URL in a doc lies as soon as it is read from preprod.
    callback_path: str = ""
    # "Is an OAuth app already available to this user?" — their own,
    # their org's, or the PUBLISHER's (oto). Without this answer, the front can
    # only promise the worst case: it asked "first set the application's
    # credentials", even of someone with nothing left to set. `None` = the connector
    # does not declare the question, the front then promises nothing.
    app_ready: Optional[Callable[[str], bool]] = None
    # The same question PER LEVEL, for a connector whose app depends on the level the
    # user connects at (HubSpot: a user-level app for "personal", an account-level one
    # for org/team): the levels (`member`, `org`, `group`) that can consent now. The
    # card offers "Connect" on those tabs only. `None` = not declared: `app_ready`
    # answers for every tab.
    app_ready_scopes: Optional[Callable[[str], list]] = None
    # Parameters the requester's applicable app already BINDS (`{name: value}`): the
    # Zoho region of the app set by their team. The front shows them instead of asking —
    # the start refuses any other value. `None` = nothing ever bound.
    fixed: Optional[Callable[[str], dict]] = None
    # Named codes the card shows about the requester's applicable app, e.g.
    # `zoho_org_unverified`: the app names no Zoho organization, any account can consent.
    warnings: Optional[Callable[[str], list]] = None


_FLOWS: dict[str, Flow] = {}


def declare(connector: str, *, start: Callable[..., FlowStart],
            params: tuple[FlowParam, ...] = (), label: str = "Connect",
            callback_path: str = "",
            app_ready: Optional[Callable[[str], bool]] = None,
            app_ready_scopes: Optional[Callable[[str], list]] = None,
            fixed: Optional[Callable[[str], dict]] = None,
            warnings: Optional[Callable[[str], list]] = None) -> None:
    """Declare this connector's connection flow. Called at MODULE level (like
    `status_hints.register_state`): it is a pure declaration, it must be readable
    at import, without waiting for the FastMCP mount."""
    for p in params:
        if not p.options and p.required and not p.default:
            # A closed choice without options cannot be started on the front side: it
            # would render an empty select. Better to refuse it at declaration than at click.
            raise ValueError(
                f"{connector}.{p.name}: required parameter without options or default.")
    _FLOWS[connector] = Flow(connector=connector, start=start,
                             params=tuple(params), label=label,
                             callback_path=callback_path, app_ready=app_ready,
                             app_ready_scopes=app_ready_scopes,
                             fixed=fixed, warnings=warnings)


def supports(connector: str) -> bool:
    return connector in _FLOWS


def entries() -> dict[str, Flow]:
    return dict(_FLOWS)


def describe(connector: str) -> Optional[dict]:
    """The `connect` field of the catalog: the SHAPE of the gesture, nothing else.

    `None` for the ~56 connectors that have no flow — the front then reads its usual
    field form, as before."""
    f = _FLOWS.get(connector)
    if f is None:
        return None
    from . import flow_status  # same package, no import-time side effect
    return {"label": f.label, "params": [p.describe() for p in f.params],
            "disconnect": flow_status.supports(connector)}


async def start(connector: str, ctx, values: dict) -> FlowStart:
    """Start the declared flow and return the common shape.

    The return type is checked HERE, at the single point of passage: a Python
    annotation does not enforce itself, and it is precisely because the guarantee
    lived only in a comment that two flows could diverge without anything
    protesting. A flow that returns anything else breaks on the first call, not on
    the first front that relies on it."""
    # A connector CUT for the caller (`connector_availability`, any tier) opens no
    # consent: refused here, before the flow builds anything — one guard for every
    # declared flow, the same reading of the cut as the call guard.
    # A flow writes a PERSONAL key unless it says `scope` org|group (Google, Salesforce,
    # WordPress): the org may forbid those for this connector (#1239).
    from .activation_gate import exiger_connectable_capacite
    await asyncio.to_thread(exiger_connectable_capacite, connector,
                            getattr(ctx, "sub", None),
                            perso=((values or {}).get("scope") or "member") == "member")
    fabrique = _FLOWS[connector].start
    if inspect.iscoroutinefunction(fabrique):
        # A flow can be ASYNCHRONOUS — that of a hosted messaging service queries the
        # provider before returning its link. The server is single-loop: this network
        # path must be awaited, never run in a blocking way.
        out = await fabrique(ctx, values or {})
    else:
        # A SYNCHRONOUS flow is not harmless either: two of them
        # dynamically register an OAuth client at the provider, over blocking HTTP
        # (cold path, the first time only). Called bare from this `async def`, they
        # froze the whole process for the duration of the response
        # (oto-backend#867). Handling them HERE covers the five flows at once —
        # none needs to know it, and neither will the next.
        out = await asyncio.to_thread(fabrique, ctx, values or {})
        if inspect.isawaitable(out):
            out = await out
    if not isinstance(out, FlowStart):
        raise TypeError(
            f"flow \"{connector}\" must return a FlowStart (got {type(out).__name__}): "
            "the shape returned to the caller is common to all connectors, what is "
            "specific to you goes in `details`.")
    return out


def callback_url(connector: str, *, host: Optional[str] = None) -> Optional[str]:
    """Return URL to register at the provider, DERIVED from the environment.

    `host`: the one set on a TENANT's host (see `oauth_flow.redirect_uri`) —
    what an admin must declare at Google when the publisher app they set is the
    tenant's, not ours (`platform.editor_app.set`).

    It is NOT in `describe()`: that descriptor goes out in `/api/connectors`,
    served without authentication. This one is only added on the authenticated
    projection — it is a value the client must know to configure its
    app, not a piece of public catalog data.

    Derived, and that is the point: until now it lived in PROSE in the connector's
    doc, with the prod domain written by hand. A preprod user therefore
    read a URL their backend does not use — and consent failed with an
    incomprehensible `redirect_uri_mismatch`."""
    f = _FLOWS.get(connector)
    if not f or not f.callback_path:
        return None
    from ..auth import flow as oauth_flow
    return oauth_flow.redirect_uri(f.callback_path, host=host)


def app_ready(connector: str, sub: str) -> Optional[bool]:
    """Does this user already have an OAuth app available for this connector?

    `None` = question not declared (or out of service): the front must then stay
    silent rather than assert. Like `callback_url`, this enters ONLY the authenticated
    projection — the answer depends on who asks.

    Deliberately fail-open: a read failure must not turn a connection screen into an
    error screen; at worst the user sees the long instruction."""
    f = _FLOWS.get(connector)
    if not f or f.app_ready is None or not sub:
        return None
    try:
        return bool(f.app_ready(sub))
    except Exception:  # noqa: BLE001
        logger.debug("app_ready failed for %s", connector, exc_info=True)
        return None


_LEVELS = ("member", "org", "group")


def readiness(connector: str, sub: str) -> tuple[Optional[bool], Optional[list[str]]]:
    """`(app_ready, app_ready_scopes)` in ONE read when the connector answers per level
    (`app_ready` is then "any level ready"), else `(app_ready(...), None)`. Same rules as
    `app_ready`: authenticated projection only, fail-open to "nothing promised"."""
    f = _FLOWS.get(connector)
    if not f or f.app_ready_scopes is None or not sub:
        return app_ready(connector, sub), None
    try:
        levels = [lv for lv in (f.app_ready_scopes(sub) or []) if lv in _LEVELS]
    except Exception:  # noqa: BLE001
        logger.debug("app_ready_scopes failed for %s", connector, exc_info=True)
        return None, None
    return bool(levels), levels


def fixed(connector: str, sub: str) -> dict:
    """The parameters this user's applicable app binds — `{}` if none. Like `app_ready`,
    ONLY on the authenticated projection (the answer depends on who asks).

    Fail-open like `app_ready`, and for the same reason: the start re-reads the app and
    refuses a mismatching value, so an unreadable answer costs a select, never a wrong
    consent. A value outside the declared options is not served: the front would show a
    region it cannot name, the start says what is wrong."""
    f = _FLOWS.get(connector)
    if not f or f.fixed is None or not sub:
        return {}
    try:
        valeurs = f.fixed(sub) or {}
    except Exception:  # noqa: BLE001
        logger.warning("fixed failed for %s", connector, exc_info=True)
        return {}
    permis = {p.name: {v for v, _ in p.options} for p in f.params}
    return {k: v for k, v in valeurs.items()
            if k in permis and (not permis[k] or v in permis[k])}


def warnings(connector: str, sub: str) -> list[str]:
    """Named codes about this user's applicable app — `[]` if none. Authenticated
    projection only. Fail-open, LOGGED: a warning that cannot be read must not turn the
    connection screen into an error screen; the consent still checks what it checks."""
    f = _FLOWS.get(connector)
    if not f or f.warnings is None or not sub:
        return []
    try:
        return [str(w) for w in (f.warnings(sub) or [])]
    except Exception:  # noqa: BLE001
        logger.warning("warnings failed for %s", connector, exc_info=True)
        return []
