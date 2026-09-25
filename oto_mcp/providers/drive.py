"""Déclaration de registre du connecteur `drive` — Google Drive, sur le compte Google.

Domicile unique de son entrée : `providers/__init__.py` l'AGRÈGE. La forme commune
aux six services Google vit chez le porteur du compte (`providers/google.service`) —
ici, ce qui distingue CELUI-CI (split du 2026-09-26).
"""
from __future__ import annotations

from .google import service

# Google Drive : la personne autorise CE service sur son compte Google, depuis cette
# carte, avec ses seuls scopes — le compte (la ligne du coffre, le refresh token) est
# celui du connecteur `google`, partagé avec les cinq autres services.
CONNECTOR = service(
    "drive",
    label="Google Drive",
    help="tes fichiers Drive — lister, lire, ranger, partager, supprimer ; scope `drive`, accordé sur ton compte Google",
    href="https://drive.google.com",
)

CATEGORY = "Comms"
LOGO_DOMAIN = "google.com"
DESCRIPTION = (
    "Ton Google Drive, sur ton compte Google : lister et lire les fichiers et dossiers, les déplacer ou les supprimer, régler qui y accède. Un consentement qui ne demande que le scope Drive."
)
