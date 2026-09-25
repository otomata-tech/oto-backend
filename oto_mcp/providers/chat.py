"""Déclaration de registre du connecteur `chat` — Google Chat, sur le compte Google.

Domicile unique de son entrée : `providers/__init__.py` l'AGRÈGE. La forme commune
aux six services Google vit chez le porteur du compte (`providers/google.service`) —
ici, ce qui distingue CELUI-CI (split du 2026-09-26).
"""
from __future__ import annotations

from .google import service

# Google Chat : la personne autorise CE service sur son compte Google, depuis cette
# carte, avec ses seuls scopes — le compte (la ligne du coffre, le refresh token) est
# celui du connecteur `google`, partagé avec les cinq autres services.
CONNECTOR = service(
    "chat",
    label="Google Chat",
    help="tes espaces Google Chat — lire les espaces et les messages, poster ; scopes `chat.spaces.readonly` + `chat.messages`, accordés sur ton compte Google",
    href="https://chat.google.com",
)

CATEGORY = "Comms"
LOGO_DOMAIN = "google.com"
DESCRIPTION = (
    "Google Chat, sur ton compte Google : lister les espaces (salons et DM) dont tu fais partie, lire leurs messages et en poster. Un consentement qui ne demande que les scopes Chat."
)
