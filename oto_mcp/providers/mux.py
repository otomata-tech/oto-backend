"""Registry declaration of the `mux` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# mux: video hosting and streaming (Mux Video) — the assets of an environment,
# direct uploads, assets made from a file address.
#
# **Basic** auth with TWO fields (`token_id` + `token_secret`), hence
# `secret_kind="fields"` and resolution via `access.resolve_credential_fields`. A
# token belongs to ONE Mux environment and sees only its assets.
#
# `token_id` is declared NOT secret: it identifies the token without exposing the
# secret key that goes with it (same split as `aircall`).
#
# Strict BYOK (`byo_user` + `byo_org`, no platform mode): the customer's own videos,
# billed to the customer's Mux account.
CONNECTOR = _c(
    "mux", ["mux"], auth_modes={"byo_user", "byo_org"},
    secret_kind="fields",
    credential_fields=(
        CredentialField(
            "token_id", "Access Token ID", secret=False,
            help="Mux dashboard → Settings → Access Tokens → Generate new token, in "
                 "the environment that holds the videos, with Mux Video read and write."),
        CredentialField(
            "token_secret", "Secret Key", secret=True,
            help="Shown only ONCE when the token is generated: Mux does not keep it "
                 "in clear text."),
    ),
    label="Mux",
    help="video hosting and streaming: list and read videos, upload addresses, "
         "videos from a file address, playback links",
    href="https://www.mux.com",
)

CATEGORY = "Marketing & SEO"
PUBLISHER = "Mux"
LOGO_DOMAIN = "mux.com"

DESCRIPTION = (
    "Host and stream videos on your Mux account: get a one-time upload address for "
    "a video file, or have Mux fetch it from a link, then follow it until it is "
    "ready and read its streaming address. Also lists your videos with their "
    "status, length and playback links. Your own access token."
)
