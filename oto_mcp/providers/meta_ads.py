"""Déclaration de registre du connecteur `meta_ads` — les campagnes Facebook et
Instagram d'un compte publicitaire Meta, par la Marketing API. LECTURE SEULE.

Domicile unique de son entrée : `providers/__init__.py` l'AGRÈGE (il ne la
décrit pas). Cf. `providers/_model.py` pour le contrat de `Connector`.
"""
from __future__ import annotations

from ._model import _c

# Même famille qu'`instagram_meta` (application Meta de l'instance, coordonnées
# posées au palier plateforme, flux hébergé par oto), mais PAS le même produit :
# Facebook Login for Business sur graph.facebook.com, un `config_id` au lieu d'une
# liste de permissions, et un jeton d'utilisateur système (BISU) qui n'expire pas
# — donc aucune passe de renouvellement.
#
# ⚠️ Sans App Review (accès « Advanced »), l'application reste en accès
# « Standard » : elle fonctionne, mais fortement limitée en débit. La fiche le dit.
CONNECTOR = _c(
    "meta_ads", ["meta_ads"],
    auth_modes={"byo_user"},
    # Le consentement naît du compte Facebook de la personne, pas de son org.
    personal_session=True, secret_kind="oauth",
    label="Meta Ads",
    help="Your Facebook & Instagram ad accounts: campaigns, ad sets, ads and their "
         "performance (spend, reach, clicks, conversions). You authorize oto on "
         "Facebook and pick the ad accounts. Read-only.",
    href="https://www.facebook.com/business/ads",
)

CATEGORY = "Marketing"
# Qui reçoit l'appel : Meta — son API officielle, son dialogue de consentement.
PUBLISHER = "Meta"
LOGO_DOMAIN = "facebook.com"

DESCRIPTION = (
    "Facebook and Instagram advertising through Meta's official Marketing API: "
    "list the ad accounts you granted, browse campaigns, ad sets and ads, and pull "
    "performance insights (spend, impressions, reach, clicks, CPC, CTR, "
    "conversions) by day, placement, age, gender or country. Read-only — nothing "
    "is created, edited, paused or spent."
)
