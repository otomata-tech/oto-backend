"""Déclaration de registre du connecteur `yousign`.

Domicile unique de son entrée : `providers/__init__.py` l'AGRÈGE (il ne la
décrit pas). Cf. `providers/_model.py` pour le contrat de `Connector`.
"""
from __future__ import annotations

from ._model import _c

# keyed BYO (user OU org), résolu via resolve_api_key comme gocardless/pennylane.
# self_serve : chacun connecte SA propre clé Yousign (sandbox ou prod) — PAS de
# clé plateforme partagée. Écrit réellement (envoie une demande de signature à
# des tiers) : à la différence de gocardless (lecture seule), l'activation d'une
# demande de signature notifie des personnes réelles — jamais silencieux.
CONNECTOR = _c(
    "yousign", ["yousign"], availability="self_serve",
    auth_modes={"byo_user", "byo_org"}, keyed=True, secret_kind="api_key",
    label="Yousign", help="signature électronique (demandes, statut, document signé)",
)

CATEGORY = "Documents & signature"
PUBLISHER = "Yousign"
LOGO_DOMAIN = "yousign.com"

DESCRIPTION = (
    "Signature électronique : créer une demande de signature à partir d'un "
    "PDF avec ses signataires, l'activer (envoie les invitations), suivre son "
    "statut et récupérer le document signé. Chacun connecte sa propre clé "
    "Yousign (sandbox ou production) — pas de clé plateforme partagée."
)
