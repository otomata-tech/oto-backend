"""Déclaration de registre du connecteur `tasks` — Google Tasks, sur le compte Google.

Domicile unique de son entrée : `providers/__init__.py` l'AGRÈGE. La forme commune
aux six services Google vit chez le porteur du compte (`providers/google.service`) —
ici, ce qui distingue CELUI-CI (split du 2026-09-26).
"""
from __future__ import annotations

from .google import service

# Google Tasks : la personne autorise CE service sur son compte Google, depuis cette
# carte, avec ses seuls scopes — le compte (la ligne du coffre, le refresh token) est
# celui du connecteur `google`, partagé avec les cinq autres services.
CONNECTOR = service(
    "tasks",
    label="Google Tasks",
    help="tes tâches — listes, création, mise à jour, achèvement ; scope `tasks`, accordé sur ton compte Google",
    href="https://tasks.google.com",
)

CATEGORY = "Comms"
LOGO_DOMAIN = "google.com"
DESCRIPTION = (
    "Google Tasks, sur ton compte Google : lister tes listes de tâches, créer, mettre à jour et achever une tâche. Un consentement qui ne demande que le scope Tasks."
)
