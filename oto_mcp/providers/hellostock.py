"""Registry declaration of the `hellostock` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# hellostock: the ADMINISTRATION API of the HelloStock marketplace (requests,
# offers, members, positionings). The client lives in oto-core
# (`oto.tools.hellostock`), the tools in `tools/hellostock.py` (reads) and
# `tools/hellostock_ecritures.py` (the three actions that act on the
# production marketplace).
#
# **byo_user only, by nature**: the credential is a PERSONAL TOKEN created by
# each user in their HelloStock account, which carries THEIR rights — and these
# routes require an administrator account. An org or platform key would be
# a named account lent to others, traced under the name of someone who did not act
# (a request send is recorded under the name of the admin whose token it is).
#
# ⚠️ WRITES and SENDS: `hellostock_demande_send` goes out by email to real
# members (dry-run BY DEFAULT), `hellostock_demande_set_status` and
# `hellostock_offre_update` modify what the public marketplace displays.
CONNECTOR = _c(
    "hellostock", ["hellostock"], auth_modes={"byo_user"}, keyed=True,
    secret_kind="api_key",
    modules=("hellostock", "hellostock_ecritures"),
    label="HelloStock administration",
    # `help` goes into the namespace map served to ALL sessions (MCP
    # instructions), enabled or not: short, and it says who it is for.
    help="requests, offers, members and sends of the marketplace — reserved for its "
         "administrators",
    href="https://hellostock.fr",
    credential_fields=(
        CredentialField(
            "key", "HelloStock API token (hs_…)", secret=True,
            help="hellostock.fr → My account → Settings → API tokens (in the French UI: "
                 "Mon espace → Réglages → Jetons d'API) → create "
                 "a token; it is only shown once. It must be the token of an "
                 "ADMINISTRATOR account of the marketplace."),
    ),
)

CATEGORY = "Custom & industry apps"
PUBLISHER = "HelloStock"
LOGO_DOMAIN = "hellostock.fr"

DESCRIPTION = (
    "The administration of the HelloStock marketplace, from your assistant: review "
    "requests, offers, members and positionings, "
    "send a request to chosen suppliers, write an offer's status and "
    "keywords. Reserved for administrators: everyone sets their own "
    "API token, and what is done is done in their name."
)
