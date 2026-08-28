"""Schéma de sortie déclaré par connecteur — pour l'UI de transformations (ADR 0015).

`FieldFilter` (oto-core) matche par **nom de clé feuille**, récursivement et insensible
à la casse, dans les réponses d'un connecteur. Aujourd'hui l'org_admin tape ces noms à
l'aveugle ; ce registre déclare, par connecteur, les **champs notables** qu'il peut émettre
pour que le dashboard les montre (onglet « transformations » de la carte connecteur) au lieu
de les deviner.

Curé, pas dérivé : il n'existe aucune source de vérité du schéma de sortie d'un connecteur
(les clients renvoient des dicts libres). On déclare donc explicitement les feuilles utiles à
redacter. Schéma incomplet/absent = acceptable : l'UI garde une saisie de champ libre puisque
`FieldFilter` matche n'importe quel nom.

Forme par champ :
    {"name": <clé feuille>, "label": <libellé UI>, "type": <hint>, "sensitive": <bool>}

La rédaction est appliquée à la frontière des tools (`middleware.field_redaction.FieldRedactionMiddleware`)
pour TOUS les connecteurs ; ce registre n'a donc plus à suivre un câblage client. À
étendre quand un connecteur émet des champs notables à proposer au dashboard.
"""
from __future__ import annotations

# Champs candidat (use-case recrutement) — partagés par unipile + les ATS. Les noms
# couvrent les variantes de casse/format ; `FieldFilter` matche la clé feuille.
_CANDIDATE_FIELDS: list[dict] = [
    {"name": "first_name", "label": "prénom", "type": "string", "sensitive": True},
    {"name": "last_name", "label": "nom", "type": "string", "sensitive": True},
    {"name": "name", "label": "nom complet", "type": "string", "sensitive": True},
    {"name": "email", "label": "email", "type": "string", "sensitive": True},
    {"name": "phone", "label": "téléphone", "type": "string", "sensitive": True},
    {"name": "photo_url", "label": "photo", "type": "string", "sensitive": True},
    {"name": "public_profile_url", "label": "URL profil public", "type": "string", "sensitive": True},
    {"name": "headline", "label": "titre/accroche", "type": "string", "sensitive": False},
    {"name": "location", "label": "localisation", "type": "string", "sensitive": False},
]

CONNECTOR_FIELD_SCHEMA: dict[str, list[dict]] = {
    # Silae (paie FR). Plancher PII = coordonnées bancaires (cf. field_filter_defaults).
    "silae": [
        {"name": "iban", "label": "IBAN", "type": "string", "sensitive": True},
        {"name": "bic", "label": "BIC", "type": "string", "sensitive": True},
        {"name": "rib", "label": "RIB", "type": "string", "sensitive": True},
        {"name": "salaire", "label": "salaire", "type": "number", "sensitive": True},
        {"name": "numeroSecu", "label": "n° sécurité sociale", "type": "string", "sensitive": True},
        {"name": "dateNaissance", "label": "date de naissance", "type": "date", "sensitive": True},
        {"name": "nom", "label": "nom", "type": "string", "sensitive": True},
        {"name": "prenom", "label": "prénom", "type": "string", "sensitive": True},
    ],
    # Folk (CRM Otomata). Contacts : identité + coordonnées.
    "folk": [
        {"name": "firstName", "label": "prénom", "type": "string", "sensitive": True},
        {"name": "lastName", "label": "nom", "type": "string", "sensitive": True},
        {"name": "name", "label": "nom (société/personne)", "type": "string", "sensitive": True},
        {"name": "emails", "label": "emails", "type": "list", "sensitive": True},
        {"name": "phones", "label": "téléphones", "type": "list", "sensitive": True},
        {"name": "jobTitle", "label": "intitulé de poste", "type": "string", "sensitive": False},
    ],
    # Forager (prospection — job posts/firmographics/contacts). Champs de contact
    # résolus par les lookups person_* (détail, reverse by email/phone, emails
    # perso/pro, téléphones) — noms alignés sur le schéma Forager réel (`full_name`,
    # `phone_number`…), pas ceux de `_CANDIDATE_FIELDS` (unipile/ATS).
    "forager": [
        {"name": "full_name", "label": "nom complet", "type": "string", "sensitive": True},
        {"name": "first_name", "label": "prénom", "type": "string", "sensitive": True},
        {"name": "last_name", "label": "nom", "type": "string", "sensitive": True},
        {"name": "email", "label": "email", "type": "string", "sensitive": True},
        {"name": "phone_number", "label": "téléphone", "type": "string", "sensitive": True},
        {"name": "photo", "label": "photo", "type": "string", "sensitive": True},
        {"name": "headline", "label": "titre/accroche", "type": "string", "sensitive": False},
    ],
    # Pennylane (compta FR). Tiers & adresses.
    "pennylane": [
        {"name": "name", "label": "nom du tiers", "type": "string", "sensitive": True},
        {"name": "emails", "label": "emails", "type": "list", "sensitive": True},
        {"name": "address", "label": "adresse", "type": "string", "sensitive": True},
        {"name": "billing_address", "label": "adresse de facturation", "type": "string", "sensitive": True},
        {"name": "city", "label": "ville", "type": "string", "sensitive": False},
        {"name": "postal_code", "label": "code postal", "type": "string", "sensitive": False},
    ],
    # Recrutement — profils/candidats (anonymisation par défaut, cf. field_filter_defaults).
    # ⚠️ Keyé `linkedin`, PAS `unipile` — c'est le NAMESPACE que le
    # middleware de rédaction résout (`namespace_of(tool)`), et depuis le split du
    # 2026-08-28 c'est aussi un connecteur. Sous `unipile`, une politique d'org ne
    # gouvernait que `unipile_connect_start`, qui ne rend aucun profil : le
    # catalogue proposait à l'admin de masquer des champs sur des outils qui n'en
    # servent pas, et les profils LinkedIn sortaient en clair.
    "linkedin": _CANDIDATE_FIELDS,
    "ashby": _CANDIDATE_FIELDS,
    "greenhouse": _CANDIDATE_FIELDS,
    "lever": _CANDIDATE_FIELDS,
    "recruitee": _CANDIDATE_FIELDS,
    "teamtailor": _CANDIDATE_FIELDS,
}


def schema_for(service: str) -> list[dict]:
    """Champs de sortie déclarés d'un connecteur (liste vide si non déclaré)."""
    return CONNECTOR_FIELD_SCHEMA.get(service, [])
