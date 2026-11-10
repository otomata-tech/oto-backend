"""Registry declaration of the `google_analytics` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# google_analytics: GA4 in READ mode, via a SERVICE ACCOUNT KEY — not via
# the OAuth of the `google` connector. A user's consent to the
# `analytics.readonly` scope is blocked by Google for our application (observed on
# 25/09/2026); a service account added as a Viewer of a GA4 property
# reads without anyone's Google account, and is cut off by removing its access.
#
# ONE secret field: the key's JSON file, whole (`secret_kind="fields"`,
# resolved by `access.resolve_credential_fields`). ⚠️ `whitespace_significant`:
# the PEM private key contains spaces ("BEGIN PRIVATE KEY") — the default cleanup,
# which strips ALL whitespace, would make it unreadable. Only the edges
# are stripped.
#
# byo_org first: the founding case is an org whose agents ALL read the
# same property, without copying the key to each (org instance). byo_user stays
# open for a person with their own service account. Outside the base set →
# installable on demand. Namespace `ga4`, the name the agent reads in its
# tools; the connector keeps the product name.
CONNECTOR = _c(
    "google_analytics", ["ga4"], auth_modes={"byo_org", "byo_user"},
    secret_kind="fields", label="Google Analytics 4",
    help="GA4 audience and events (read-only) — reports, realtime, "
         "dimensions and metrics, via a service account",
    href="https://analytics.google.com", credential_fields=(
        CredentialField(
            "service_account_json", "Service account JSON key", secret=True,
            whitespace_significant=True,
            help="Google Cloud → IAM → Service accounts → Keys → Add key → "
                 "JSON: paste the ENTIRE contents of the file. Then, in GA4, add "
                 "the service account's email as a Viewer of the property."),
    ),
)

CATEGORY = "Marketing & SEO"
PUBLISHER = "Google"
LOGO_DOMAIN = "analytics.google.com"

DESCRIPTION = (
    "The audience and events of a Google Analytics 4 property, "
    "read-only: reports over a period, realtime, catalogue of dimensions and "
    "metrics, key events. Access via a service account added as a "
    "Viewer in GA4 — no personal Google account is connected."
)
