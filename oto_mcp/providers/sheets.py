"""Déclaration de registre du connecteur `sheets` — Google Sheets, sur le compte Google.

Domicile unique de son entrée : `providers/__init__.py` l'AGRÈGE. La forme commune
aux six services Google vit chez le porteur du compte (`providers/google.service`) —
ici, ce qui distingue CELUI-CI (split du 2026-09-26).
"""
from __future__ import annotations

from .google import service

# Google Sheets : la personne autorise CE service sur son compte Google, depuis cette
# carte, avec ses seuls scopes — le compte (la ligne du coffre, le refresh token) est
# celui du connecteur `google`, partagé avec les cinq autres services.
CONNECTOR = service(
    "sheets",
    label="Google Sheets",
    help="tes feuilles de calcul — lire, écrire, créer ; scope `spreadsheets`, accordé sur ton compte Google",
    href="https://docs.google.com/spreadsheets",
)

CATEGORY = "Comms"
LOGO_DOMAIN = "google.com"
DESCRIPTION = (
    "Google Sheets, sur ton compte Google : lire et écrire les cellules d'un tableur, en créer un vide. Un consentement qui ne demande que le scope Sheets."
)
