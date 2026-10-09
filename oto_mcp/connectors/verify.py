"""Per-connector credential probe — "test the connection" (generic framework).

A keyed/multi-field credential (Zoho, Silae…) can be SET without authenticating
(wrong data center, stale refresh token…). `credentials_store.credential_status`
only says "set / not set" (the vault row exists), never "it authenticates".
Each connector can register a **probe**: a call with NO side effect that, from
the decrypted fields, checks that the credential really authenticates and RAISES
on failure (the exception message = the error returned to the UI).

Same pattern as `browser_session.register` / `connector_identities.register`: the
logic lives in the connector's `tools/<name>.py` module (which calls `register()`
at load) ; the SURFACE (MCP+REST capability) is declared once in
`capabilities/connectors/verify.py`.
"""
from __future__ import annotations

import asyncio
import inspect
from typing import Awaitable, Callable, Optional, Union

from .. import providers

# probe(fields, config) -> None : raises an exception on authentication failure (its
# message is rendered to the client). Sync OR async (the capability awaits if needed).
# `fields` = DECRYPTED credential fields (client_id/secret/refresh_token/data_center
# for zoho); `config` = NON-secret satellites paired with the winning key (public
# meta: unipile dsn…). A probe that talks to an endpoint whose host depends on the
# key (unipile, BYO tenant) MUST read `config`, otherwise it tests the key against
# the wrong tenant.
Probe = Callable[[dict, dict], Union[None, dict, Awaitable[Union[None, dict]]]]

#: What a probe may RETURN, besides raising on failure: a dict of measurements.
#: The balance, for `auth+quota` probes — `{"quota": {...}}` ; and `identity`, WHO
#: the key authenticates as at the provider (today Slack).
#:
#: ⚠️ `identity` answers a question no other surface asks: "is it still the same
#: application as yesterday?". A key replaced by one from ANOTHER application of the
#: same provider authenticates perfectly, yet loses everything the previous one had
#: acquired — channel memberships, for Slack. The vault only sees a healthy key; the
#: probe holds the only response body where the provider NAMES the application.
#: Throwing it away made the change undetectable: six days of unreadability on four
#: client channels (signals 802/814, 08/09/2026), and nobody to say why.
#:
#: ⚠️ Returning is OPTIONAL and will stay so: probes that only measure an
#: authentication return `None`, as before. Requiring a return from all of them would
#: have forced inventing an empty shape for the dozen-odd that have nothing to say —
#: and an empty shape ends up being read as a measurement of zero.
#:
#: `scopes` — per API family, what the token is allowed to read (HubSpot grants its
#: scopes object by object). It is a MEASUREMENT, never the verdict: a token missing
#: the tickets scope still authenticates, and `ok` stays true (oto#69, third rule).
_CLES_DE_MESURE = ("quota", "identity", "scopes")


def _mesures(rendu) -> dict:
    """What the probe measured, reduced to the known keys. A return that is not a
    dict — the case of all `auth` probes — means "nothing measured", never an
    error: that is the old contract, and it must keep passing."""
    if not isinstance(rendu, dict):
        return {}
    return {k: rendu[k] for k in _CLES_DE_MESURE if k in rendu}

#: What a probe COVERS. Declared, never guessed (oto#57).
#:
#: Two neighbouring probes did different things and nothing distinguished them from
#: the outside: the `theirstack` one reads the BALANCE (the free authenticated call),
#: the `origami` one lists objects — which answers perfectly on a dry account.
#: Measured on 04/09/2026: an all-green preflight, then 402 after four spaces, four
#: tables and 28 rows were created.
#:
#: ⚠️ **The preflight had not lied**: it had reported a green that did not mean what
#: we thought. That nuance decides the remedy — we do not need more probes, we need a
#: probe to SAY WHAT IT COVERS, so that a green says what it is worth and a caller
#: knows what it does not know.
#: The VERDICTS of a probe. Three states that call for OPPOSITE courses of action,
#: where a boolean distinguished none (oto#57):
#:   `ok`           — it works.
#:   `unauthorized` — the key does not authorize: replace it or widen its scope.
#:                    Setting one more will change nothing.
#:   `no_quota`     — the key is good, the balance is empty: top up.
#:   `unknown`      — the probe failed without our being able to classify it. ⚠️ Read
#:                    as "I don't know", NEVER "nothing serious": it is the most
#:                    frequent value today, and confusing it with a diagnosis would
#:                    do exactly the harm this batch repairs.
OK = "ok"
UNAUTHORIZED = "unauthorized"
NO_QUOTA = "no_quota"
UNKNOWN = "unknown"
VERDICTS = (OK, UNAUTHORIZED, NO_QUOTA, UNKNOWN)


class SondeRefusee(Exception):
    """A probe that KNOWS why it fails says so by raising one of the two below.
    This is the explicit path, preferred over classifying by HTTP code: it
    survives an upstream that would answer 200 with an error body."""


class NonAutorise(SondeRefusee):
    """The key does not authorize — invalid, revoked, or insufficient scope."""


class QuotaEpuise(SondeRefusee):
    """The key authenticates, there is nothing left to spend."""


#: The upstream codes that classify a failure when the probe said nothing itself.
#: ⚠️ Read from `status_code` (`UpstreamHTTPError`), **never guessed from the text**
#: of a message: a classification built on words changes meaning at the first
#: upstream reformatting, and nobody notices.
_PAR_CODE = {401: UNAUTHORIZED, 403: UNAUTHORIZED, 402: NO_QUOTA, 429: NO_QUOTA}


def classer(erreur: BaseException) -> str:
    """The verdict of a probe failure. `unknown` when nothing allows a ruling —
    and that is a verdict in its own right, not a default."""
    if isinstance(erreur, QuotaEpuise):
        return NO_QUOTA
    if isinstance(erreur, NonAutorise):
        return UNAUTHORIZED
    code = getattr(erreur, "status_code", None)
    if isinstance(code, int):
        return _PAR_CODE.get(code, UNKNOWN)
    return UNKNOWN


#: What to do, per verdict. A diagnosis that does not say what to do sends people
#: searching — and that is how one person retried a valid connection six times.
CONDUITE = {
    UNAUTHORIZED: ("the key does not authorize this call — replace it, or widen its "
                   "scope at the provider. Setting one MORE will change "
                   "nothing."),
    NO_QUOTA: ("the key is good: it is the balance that is empty. Top up the account "
               "at the provider — no need to reconnect anything."),
    UNKNOWN: ("the test failed without saying why. Read `error` as is: it comes "
              "from the provider, and it is the only thing we know."),
}

AUTH = "auth"                 # the key authenticates. Says NOTHING about the balance.
AUTH_QUOTA = "auth+quota"     # the key authenticates AND there is enough left to work with.
AUTH_SCOPES = "auth+scopes"   # the key authenticates, AND which API families it may read.
COUVERTURES = (AUTH, AUTH_QUOTA, AUTH_SCOPES)

_REGISTRY: dict[str, Probe] = {}
_COUVERTURE: dict[str, str] = {}


def register(connector: str, probe: Probe, couvre: str = AUTH) -> None:
    """Declare a connector's verification probe (called at module load).

    `couvre` DEFAULTS to `auth` — the prudent default: a probe only proves what it
    measured, and declaring `auth+quota` without reading a balance would produce
    exactly the misleading green we are trying to eliminate. Only raise it if the
    probe really reads a balance or a quota."""
    if couvre not in COUVERTURES:
        raise ValueError(f"unknown coverage {couvre!r} — expected {COUVERTURES}")
    _REGISTRY[connector] = probe
    _COUVERTURE[connector] = couvre


def couverture(connector: str) -> Optional[str]:
    """What this connector's probe covers, or `None` if it has none.

    ⚠️ `None` reads as "no probe", never "covers nothing" — the two call for
    different courses of action: in one case we cannot measure, in the other we
    measured authentication alone."""
    return _COUVERTURE.get(_porteur(connector))


def _porteur(connector: str) -> str:
    """The connector that CARRIES the key (`Connector.credential_of` delegation), same
    normalization as the cascade walker (`access/cascade.py::walk_cascade`).

    Six Unipile channels (`linkedin_unipile`, `whatsapp`…) have no probe OF THEIR
    OWN — they borrow the `unipile` one, registered under THAT name. Without this
    normalized read, each answered `verify_unavailable` despite a probe that tests
    exactly their key (oto#69): six holes that were not one, held by the same bug
    the cascade fixes for credential resolution."""
    return providers.credential_provider(connector)


def supports(connector: str) -> bool:
    return _porteur(connector) in _REGISTRY


def probe_for(connector: str) -> Optional[Probe]:
    return _REGISTRY.get(_porteur(connector))


# Time limit for ONE probe, aligned with that of the "test" button in
# `capabilities/tools_me.py`. Probes have very uneven timeouts —
# 20 s here, 120 s read at Unipile — and none has any reason to make a human
# wait longer than that.
_BORNE_S = 45.0


async def executer(probe: Probe, fields: dict, config: Optional[dict] = None,
                   instance: Optional[tuple] = None) -> None:
    """Run ONE probe OUTSIDE the event loop, under a time limit.

    Single execution point for the 34 probes (oto-backend#867, batch 2). Almost all
    are synchronous and do HTTP: called bare from an `async def` handler, they block
    the whole process — MCP, REST and watch probes — for as long as the upstream
    takes to answer. The capabilities seam only protects `def` handlers; through
    the `async` door it protects nothing, and that is where these three entries
    pass.

    The rule was written twice, here and in the capability, with the same signature
    resolution: both called the probe their own way. It now lives in a single
    place, otherwise fixing one leaves the other.

    Returns the probe's MEASUREMENTS (today `{"quota": …}`), or `{}` if it has
    none — which is the case of all `auth` probes.

    ⚠️ The limit frees the LOOP, it does not interrupt the thread: a synchronous
    HTTP client is not cancellable, and the thread lives until its own timeout
    expires. What matters is held — the process responds, and the caller gets a
    named error instead of waiting.
    """
    kwargs = {}
    # `instance` = (entity_type, entity_id, account) of the key ACTUALLY probed.
    # Essential as soon as a probe has a side effect on the credential: under
    # rotation (Salesforce RTR), probing CONSUMES the token, and the replacement must
    # be rewritten to the right row. Without this information, the probe can only
    # guess via the cascade — which designates the nearest key, not the one being
    # tested. A `verify level=org` thus killed the org token by refreshing it:
    # `ok:true`, then dead. Seen 03/08. Passed ONLY to probes that declare it: the
    # ~15 others keep their two-argument signature.
    if instance is not None and "instance" in inspect.signature(probe).parameters:
        kwargs["instance"] = instance

    async def _joue():
        if inspect.iscoroutinefunction(probe):
            return await probe(fields, config or {}, **kwargs)
        # A sync probe goes to the thread. A probe not declared `async
        # def` but returning an awaitable (callable, partial) also goes through:
        # creating it in a thread does not run it, we await it here afterwards.
        res = await asyncio.to_thread(probe, fields, config or {}, **kwargs)
        if inspect.isawaitable(res):
            return await res
        return res

    try:
        return _mesures(await asyncio.wait_for(_joue(), timeout=_BORNE_S))
    except asyncio.TimeoutError as e:
        raise TimeoutError(
            f"the connection test did not respond within {int(_BORNE_S)} s — "
            "the remote service is slow or unreachable. The credential is not "
            "necessarily invalid: try again later.") from e


async def run(connector: str, fields: dict, config: Optional[dict] = None,
              instance: Optional[tuple] = None) -> None:
    """Run the connector's probe if it exists (await if async); RAISES
    the probe's exception on authentication failure, no-op if no probe is
    registered. Helper shared between the `connectors.verify` capability (which
    translates the exception into `{ok:false}`) and the verify-before-persist of
    `api_key_save` (#106, which translates it into 400 and does not write the
    credential)."""
    probe = probe_for(connector)
    if probe is None:
        return
    return await executer(probe, fields, config, instance)
