"""Registry declaration of the `lucca` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it doesn't
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# lucca: FR HR (directory, absences, expense claims, organization), API v3
# "legacy". Auth = static API key in a HEADER (NOT OAuth2, NOT Bearer —
# "Authorization: lucca application={key}") + a tenant domain, two
# secrets → generic multi-field model (ADR 0011), same family as silae.
# NOT keyed (byo-only: the credential IS the grant, no platform key or
# quota — each firm/employer has their own Lucca account). Outside the
# base set → installable on demand (activation gate per org).
CONNECTOR = _c(
    "lucca", ["lucca"], auth_modes={"byo_user"}, secret_kind="fields",
    label="Lucca", help="FR HR (read) — directory, absences, expenses, organization",
    href="https://www.lucca.fr", credential_fields=(
        CredentialField(
            "api_key", "API key", secret=True,
            help="Lucca account → Settings → API → generate an application key."),
        CredentialField(
            "domain", "Subdomain", secret=False,
            help="ONLY the subdomain of your Lucca instance — e.g. \"acme\" for "
                 "acme.ilucca.net (not the full URL)."),
    ),
)

CATEGORY = "HR & recruiting"
PUBLISHER = "Lucca"
LOGO_DOMAIN = "lucca.fr"

DESCRIPTION = (
    "A company's directory, absences, expense claims and organization "
    "in Lucca (read). API key to generate on the Lucca admin side, "
    "specific to the instance's subdomain."
)
