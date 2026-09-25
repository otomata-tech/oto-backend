"""Déclaration de registre du connecteur `calendar` — Google Calendar, sur le compte Google.

Domicile unique de son entrée : `providers/__init__.py` l'AGRÈGE. La forme commune
aux six services Google vit chez le porteur du compte (`providers/google.service`) —
ici, ce qui distingue CELUI-CI (split du 2026-09-26).
"""
from __future__ import annotations

from .google import service

# Google Calendar : la personne autorise CE service sur son compte Google, depuis cette
# carte, avec ses seuls scopes — le compte (la ligne du coffre, le refresh token) est
# celui du connecteur `google`, partagé avec les cinq autres services.
CONNECTOR = service(
    "calendar",
    label="Google Calendar",
    help="ton agenda — lister les calendriers, lire, créer, modifier, supprimer un événement ; scope `calendar`, accordé sur ton compte Google",
    href="https://calendar.google.com",
)

CATEGORY = "Comms"
LOGO_DOMAIN = "google.com"
DESCRIPTION = (
    "Google Calendar, sur ton compte Google : lister tes calendriers, lire et créer des événements, les modifier ou les supprimer. Un consentement qui ne demande que le scope Calendar."
)
