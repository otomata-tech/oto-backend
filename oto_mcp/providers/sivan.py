"""Registry declaration of the `sivan` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# sivan: staff scheduling and workforce management — the group's companies and
# employees, the PLANNED schedule, clock-in/out punches (badging module), the
# absences and the worked-time aggregates. A neighbour of `payfit` and `lucca`
# by trade (HR / personnel), hence the HR category and the same BYO regime.
# The client lives in the connector library (`oto.tools.sivan.SivanClient`), the
# tools in `tools/sivan.py` (six read-only tools, one per endpoint).
#
# keyed `api_key`: a single key, sent in the `X-Api-Key` header.
# ⚠️ It is a GROUP key: it sees every company of the group, not only one.
#
# Strict BYOK (`byo_user` + `byo_org`, no platform mode): these are the
# customer's personnel data, there can be no shared oto key. Read-only.
CONNECTOR = _c(
    "sivan", ["sivan"], auth_modes={"byo_user", "byo_org"}, keyed=True,
    secret_kind="api_key", label="Sivan",
    help="staff schedule, clock-in punches, absences and worked-time aggregates",
    href="https://www.sivan.io",
    credential_fields=(
        CredentialField(
            "key", "Sivan API key", secret=True,
            help="an administrator generates a GROUP-level API key in Sivan; it is "
                 "sent as the `X-Api-Key` header. The key belongs to the group: it "
                 "reads every company of the group."),
    ),
)

CATEGORY = "HR & recruiting"
PUBLISHER = "Sivan"
LOGO_DOMAIN = "sivan.io"

DESCRIPTION = (
    "Staff scheduling and workforce management by Sivan: the group's companies "
    "and employees, the planned schedule, clock-in/out punches (badging module), "
    "absences and worked-time aggregates. Read-only. Group-level API key, created "
    "by an administrator."
)
