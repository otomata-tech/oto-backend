"""Le bac à sable est plein : 429 `sandbox_busy`, avec `Retry-After` (03/11/2026).

`functions/places.py` borne les exécutions simultanées du bac à sable — au plus
`OTO_FUNCTIONS_MAX_SIMULTANEES` par processus, une par compte. Au-delà, rien n'est
lancé et le refus dit quand réessayer. Partagé par `oto_function` (test, publish, run)
et `oto_recipe` (recette-code : test, publish, run), qui le déclarent tous deux."""
from __future__ import annotations

from ..functions import places
from ._types import AuthzDenied, DeclaredError

CODE = "sandbox_busy"

REFUS = DeclaredError(
    429, CODE,
    "the sandbox already runs its maximum of simultaneous executions on this server, or "
    "this account already has one running: nothing was started; retry after the "
    "`Retry-After` delay (seconds)")


def refus(e: places.BacASableOccupe) -> AuthzDenied:
    """Le refus servi pour `e` : l'appelant écrit `raise refus(e)`."""
    return AuthzDenied(429, "sandbox_busy", str(e),
                       details={"retryable": True, "retry_after_seconds": e.retry_after},
                       retry_after=e.retry_after)
