"""Registry declaration of the `recruitee` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

CONNECTOR = _c(
    "recruitee", ["recruitee"], auth_modes={"byo_user"}, secret_kind="fields",
    label="Recruitee",
    help="ATS — candidats, offers (postes), notes",
    href="https://www.recruitee.com", credential_fields=(
        CredentialField("api_token", "API token", secret=True),
        CredentialField("company_id", "Company ID", secret=False),
    ),
)

CATEGORY = "HR & recruiting"
PUBLISHER = "Recruitee"
LOGO_DOMAIN = "recruitee.com"

DESCRIPTION = (
    "Le recrutement suivi dans Recruitee (ATS) : candidats, offres d'emploi "
    "(offers) et notes."
)
