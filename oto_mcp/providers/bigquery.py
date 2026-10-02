"""Déclaration de registre du connecteur `bigquery` — Google BigQuery, sur le compte Google.

Domicile unique de son entrée : `providers/__init__.py` l'AGRÈGE. La forme commune
aux services Google vit chez le porteur du compte (`providers/google.service`) —
ici, ce qui distingue CELUI-CI (septième service, 2026-10-02).
"""
from __future__ import annotations

from .google import service

# Google BigQuery : la personne autorise CE service sur son compte Google, depuis cette
# carte (scope `bigquery`) ; les requêtes voient exactement ce que SES droits IAM
# voient, et sont facturées au projet qu'elle désigne. Lecture seule tenue par les
# tools (dry run SELECT exigé, plafond d'octets facturés sur chaque requête).
CONNECTOR = service(
    "bigquery",
    label="Google BigQuery",
    help="ton entrepôt BigQuery — explorer projets, datasets et tables, lancer des "
         "requêtes SQL en lecture ; scope `bigquery`, accordé sur ton compte Google",
    href="https://console.cloud.google.com/bigquery",
)

CATEGORY = "Dev"
LOGO_DOMAIN = "cloud.google.com"
DESCRIPTION = (
    "Google BigQuery, sur ton compte Google : parcourir projets, datasets et schémas, "
    "aperçu gratuit d'une table, requêtes SQL en lecture seule (SELECT) avec estimation "
    "du coût et plafond d'octets facturés. Tes droits IAM BigQuery s'appliquent."
)
