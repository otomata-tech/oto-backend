"""Déclaration de registre du connecteur `gocardless`.

Domicile unique de son entrée : `providers/__init__.py` l'AGRÈGE (il ne la
décrit pas). Cf. `providers/_model.py` pour le contrat de `Connector`.
"""
from __future__ import annotations

from ._model import _c

# keyed BYO (user OU org), résolu via resolve_api_key comme pennylane/attio.
# self_serve : chacun connecte SON propre compte GoCardless (sandbox ou prod) —
# PAS de clé plateforme partagée, donc rien de sensible à gater par grant. Reste
# hors socle → opt-in, pas imposé. Une org cliente
# y pose le token de son compte de service pour le POC avoirs (guide d'une org client).
CONNECTOR = _c(
    "gocardless", ["gocardless"], availability="self_serve",
    auth_modes={"byo_user", "byo_org"}, keyed=True, secret_kind="api_key",
    label="GoCardless", help="prélèvements SEPA (lecture)",
)

CATEGORY = "Finance"
PUBLISHER = "GoCardless"
LOGO_DOMAIN = "gocardless.com"

DESCRIPTION = (
    "Les prélèvements SEPA d'un compte GoCardless, en lecture. Chacun connecte "
    "son propre compte (sandbox ou production) — pas de clé plateforme "
    "partagée."
)
