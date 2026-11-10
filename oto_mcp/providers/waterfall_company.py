"""Registry declaration of the `waterfall_company` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# A COMPOSITE (`oto_mcp/waterfall/`, shared by the six waterfall connectors): each step
# is an ordinary call of another connector's tool, which resolves ITS key, passes ITS
# activation gate and bills on ITS row. The only thing this card stores is a SETTING —
# the ordered list of steps — never a secret: `secret=False`, rendered as-is, set by an
# org admin (`byo_org`, the org default) or by a member for themselves (`byo_user`, an
# override), resolved by the same cascade as a key. Nothing set = the default order —
# hence `secret_kind="settings"`: the card works with nothing set.
CONNECTOR = _c(
    "waterfall_company", ["waterfall_company"], secret_kind="settings", auth_modes={"byo_user", "byo_org"},
    cardinality="mono", modules=("waterfall", "waterfall_lignes"),
    credential_fields=(CredentialField(
        "steps", "Steps, in order", secret=False, required=False,
        help="Provider keys, comma-separated, in the order to try. Empty = default order. Available: fr, lemlist, aiark, pipecorn, apollo, theirstack, cognism, lemlist_linkedin, linkedin."),),
    label="Company waterfall",
    help="a company's firmographics, across your connected providers one after the other, stopping at the "
         "first hit",
)

# Publisher: ours — the cascade runs on our server; the calls it makes reach the
# providers under their own connectors, each of which names its own publisher.
PUBLISHER = "Otomata"
SANS_LOGO_DE_MARQUE = True
CATEGORY = "Lead & company data"

DESCRIPTION = (
    "Finds a company's legal name, SIREN, size, industry and revenue — the free French registry first, then AI Ark, Pipecorn, Apollo, TheirStack, Cognism — stopping at the first match."
)
