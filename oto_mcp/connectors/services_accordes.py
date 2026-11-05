"""The services a Google or Microsoft account has AUTHORIZED, read on its vault row.

A Google or Microsoft account authorizes its services one by one (incremental consent):
the scopes it granted live in the row's `meta.scopes`, and each auth module says which
services they cover (`auth/google.services_granted`, `auth/microsoft.services_granted`).
Read by the account rules: a rule on a Google or Microsoft account may cover a list of
services, each one a service the account authorized (`capabilities/connectors/sharing`).
"""
from __future__ import annotations

from .. import providers
from ..access import PORTEURS_A_SERVICES


def services_du_porteur(porteur: str) -> list[str]:
    """The services that borrow `porteur`'s account (`credential_of`), in name order."""
    return sorted(c.name for c in providers.REGISTRY.values() if c.credential_of == porteur)


def services_autorises(porteur: str, meta: dict) -> list[str]:
    """The services the account of this row has authorized, in the auth module's order.
    Raises for a carrier that does not authorize services one by one: nothing here
    guesses a list for it."""
    if porteur not in PORTEURS_A_SERVICES:
        raise ValueError(f"`{porteur}` does not authorize services one by one.")
    if porteur == "google":
        from ..auth import google as module
    else:
        from ..auth import microsoft as module
    return list(module.services_granted((meta or {}).get("scopes")))
