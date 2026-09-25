"""Déclaration de registre du connecteur `gmail` — Gmail, sur le compte Google.

Domicile unique de son entrée : `providers/__init__.py` l'AGRÈGE. La forme commune
aux six services Google vit chez le porteur du compte (`providers/google.service`) —
ici, ce qui distingue CELUI-CI (split du 2026-09-26).
"""
from __future__ import annotations

from .google import service

# Gmail : la personne autorise CE service sur son compte Google, depuis cette
# carte, avec ses seuls scopes — le compte (la ligne du coffre, le refresh token) est
# celui du connecteur `google`, partagé avec les cinq autres services.
CONNECTOR = service(
    "gmail",
    label="Gmail",
    help="ta boîte Gmail — chercher, lire, rédiger, envoyer, archiver ; scope `gmail.modify`, accordé sur ton compte Google",
    href="https://mail.google.com",
)

CATEGORY = "Comms"
LOGO_DOMAIN = "gmail.com"
DESCRIPTION = (
    "Ta boîte Gmail, sur ton compte Google : chercher et lire les messages, rédiger un brouillon ou envoyer, archiver, mettre à la corbeille, lire une pièce jointe. Un consentement qui ne demande que le scope Gmail."
)
