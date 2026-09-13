"""Déclaration de registre du connecteur `monid`.

Domicile unique de son entrée : `providers/__init__.py` l'AGRÈGE (il ne la
décrit pas). Cf. `providers/_model.py` pour le contrat de `Connector`.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# monid : une PASSERELLE payante — quelque 2 000 endpoints d'environ 70 fournisseurs
# (recherche web, scraping, enrichissement, réseaux sociaux…) derrière une seule clé,
# chaque appel débité du portefeuille prépayé du workspace Monid. Le client vit dans
# oto-core (`oto.tools.monid`), les outils dans `tools/monid.py`.
# keyed api_key, même régime qu'`apify` : BYO par défaut (le portefeuille est celui du
# compte connecté) ; clé plateforme GRANT-ONLY, parce qu'un appel y dépense de l'argent.
CONNECTOR = _c(
    "monid", ["monid"], auth_modes={"byo_user", "byo_org", "platform"}, keyed=True,
    default_quota=0, platform_key_open=False,  # clé plateforme sur grant explicite (appels facturés au portefeuille)
    secret_kind="api_key",
    label="Monid",
    # `help` part dans la carte des namespaces servie à TOUTES les sessions : court,
    # il dit qu'on y paie des fournisseurs tiers, et pour quels besoins.
    help="passerelle payante vers ~2 000 endpoints de données (recherche web, scraping, "
         "enrichissement, réseaux sociaux…), facturés à l'appel",
    href="https://monid.ai",
    credential_fields=(
        CredentialField(
            "key", "Clé d'API Monid", secret=True,
            help="monid.ai → tableau de bord → API keys ; elle commence par `monid_`"),
    ),
)

CATEGORY = "Prospection"
PUBLISHER = "Monid"
LOGO_DOMAIN = "monid.ai"

DESCRIPTION = (
    "Les endpoints d'environ 70 fournisseurs de données (recherche web, scraping, "
    "enrichissement, réseaux sociaux…) derrière une seule clé : trouver l'endpoint, "
    "lire son prix et son schéma d'entrée, le lancer. Chaque appel est débité du "
    "portefeuille Monid du compte connecté ; accès plateforme réservé (grant explicite)."
)
