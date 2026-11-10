"""Registry declaration of the `browser` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). Cf. `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# browser: GENERIC connector for reading behind a login (oto-private#79). The
# three previous ones are hardcoded for ONE private API that we exploit in
# depth; this one serves the opposite need — reading N sites (paid media,
# intranet, back-office without API) without a dev cycle per site. **Multi-account**:
# one vault account = one site (host), hence one Browserbase Context per site
# (isolated sessions, cf. `cardinality`). byo_user: a logged-in session
# is physiologically personal. Outside the base, installable from the library;
# `browser_eval` (arbitrary JS) stays hidden by default (DEFAULT_HIDDEN_TOOLS).
CONNECTOR = _c(
    "browser", ["browser"], auth_modes={"byo_user"}, personal_session=True,
    # Cookie session ⟹ the derivation would say mono; but here an account is a SITE
    # (one Browserbase Context per host), and by definition there are several.
    cardinality="multi", account_axis_static=True,
    secret_kind="cookie", label="Signed-in browser", account_noun="site",
    help="read a site that requires being signed in — one account per site, the session "
         "runs at Browserbase",
)

CATEGORY = "Web search & scraping"

# Publisher: the connector is OURS — we wrote it, and we are the ones receiving
# the call (the site's cookie lives in our vault; the session runs on our Browserbase
# account, an infrastructure, not a gateway that would hold the person's account).
# DECLARED, and not derived from a default: since 2026-09-02 there is no default, and
# an omission must not be readable as a choice (`Connector.publisher_name`).
PUBLISHER = "Otomata"
SANS_LOGO_DE_MARQUE = True

DESCRIPTION = (
    "Read a site that requires being signed in — an intranet, paid media, a "
    "back-office without an API — without writing dedicated code for that site. One vault "
    "account = one site; the session connects once through a hosted "
    "browser and persists afterwards."
)
