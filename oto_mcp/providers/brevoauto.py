"""Registry declaration of the `brevoauto` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# brevoauto: automations (marketing workflows) via the vendor's PRIVATE API
# (`workflow-apis.brevo.com/v1`). Connector SEPARATE from the keyed `brevo` (public
# v3 API, further down) because the credential differs — browser session here, API key there;
# same vendor, two disjoint surfaces (the v3 key doesn't open automation
# authoring). Same split as pennylane / pennylaneged.
# Execution = **Browserbase** (hosted remote Chrome): the user logs in once via
# Live View (`brevoauto_connect_start`), their session persists in a Context = the
# per-user credential (vault). No browser on the box, no cookie export.
# personal_session (a session that is inherently per-user). Experimental (undocumented
# API): outside the base set, installable from the library.
CONNECTOR = _c(
    "brevoauto", ["brevoauto"], auth_modes={"byo_user"}, personal_session=True,
    secret_kind="cookie",
    label="Brevo (automation)", help="marketing automations (Browserbase session)",
    publisher="Brevo", href="https://app.brevo.com/automation/automations",
)

CATEGORY = "Marketing & SEO"
LOGO_DOMAIN = "brevo.com"

DESCRIPTION = (
    "Brevo's automations (marketing workflows), driven through your Brevo "
    "session connected via a hosted browser — not the v3 API key (separate "
    "`brevo` connector, which gives no access to this automation editor). "
    "Experimental: the API is not publicly documented."
)
