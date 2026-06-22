"""Rédaction de champs : **rien par défaut** + templates applicables en 1 clic.

Décision (2026-06-22) : on ne redacte **AUCUN** connecteur par défaut. La rédaction
est *disponible* partout (le middleware `FieldRedactionMiddleware` l'applique dès qu'une
org pose une politique pour un service), mais elle ne s'active que sur décision explicite
de l'org. Raison : la PII n'est pas toujours un risque — sur un CRM/inbox/annuaire
légal, c'est le but. Et le matching par clé est aveugle au contexte (faux positifs).

`SERVER_DEFAULTS` reste donc **vide**. Pour ne pas re-saisir des règles utiles, on
expose des **TEMPLATES** nommés (jeux de règles prêts) que l'UI applique en un clic —
ex. « anonymisation candidat » pour le recrutement. Appliquer un template = poser une
politique d'org normale (rien de magique).

Forme d'un bloc / template = `{ "salt": str?, "rules": [ {fields, action, ...} ] }`.
`FieldFilter` (oto-core) matche par **nom de clé feuille**, récursivement et insensible
à la casse → un même jeu couvre les variantes de nommage (snake/camel/kebab).
"""
from __future__ import annotations

# Anonymisation d'un profil/candidat (use-case recrutement) : on masque l'identité
# avant que l'agent voie le profil — pseudonyme cohérent pour les noms (analyse
# possible sans ré-identification), masque format-préservant pour les coordonnées,
# suppression des ré-identifiants directs (photo, URL/ids publics), drop de la date de
# naissance. La localisation/headline sont **gardés** (utiles au scoring).
#
# ⚠️ Calé sur la FORME RÉELLE observée (`unipile_profile` : `contact_info.emails`/
# `phones`, `profile_picture_url` + `_large`, `provider_id`, `birthdate`…). On NE
# pseudonymise PAS la clé générique `name` : le moteur matche par clé feuille à toute
# profondeur, et `name` désigne aussi `skills[].name`/`languages[].name` → ça les
# corromprait. Le nom de la personne passe par `first_name`/`last_name` (et `full_name`/
# `display_name`, sans ambiguïté). Un connecteur dont la personne vit sous `name` se
# règle via le dry-run (schéma réel), pas par un défaut aveugle.
_CANDIDATE_PII: list[dict] = [
    {"fields": ["first_name", "firstName", "first-name", "prenom", "given_name", "givenName"],
     "action": "pseudonym", "kind": "first_name"},
    {"fields": ["last_name", "lastName", "last-name", "nom", "family_name", "familyName", "surname"],
     "action": "pseudonym", "kind": "last_name"},
    {"fields": ["full_name", "fullName", "full-name", "display_name", "displayName"],
     "action": "pseudonym", "kind": "name"},
    {"fields": ["email", "emails", "email_address", "emailAddress"],
     "action": "mask", "preserve": "email"},
    {"fields": ["phone", "phones", "phone_number", "phoneNumber", "mobile", "telephone"],
     "action": "mask", "preserve": "phone"},
    {"fields": ["photo_url", "photoUrl", "picture_url", "pictureUrl", "profile_picture_url",
                "profile_picture_url_large", "profilePicture", "avatar_url", "avatarUrl", "image_url"],
     "action": "drop"},
    {"fields": ["public_profile_url", "publicProfileUrl", "profile_url", "profileUrl",
                "public_identifier", "publicIdentifier", "permalink", "profile_link",
                "provider_id", "member_urn"],
     "action": "drop"},
    {"fields": ["birthdate", "birth_date", "date_of_birth", "dob", "dateNaissance"],
     "action": "drop"},
]

# Rien par défaut : aucune rédaction tant que l'org n'en pose pas (cf. docstring).
SERVER_DEFAULTS: dict[str, dict] = {}

# Templates appliquables en 1 clic depuis le dashboard (≠ défaut : pas auto-appliqués).
TEMPLATES: dict[str, dict] = {
    "candidate": {
        "label": "anonymisation candidat",
        "hint": "masque l'identité d'un profil/CV (nom→pseudonyme, contact masqué, "
                "photo/URL/ids retirés) — pour analyser un candidat sans le ré-identifier.",
        "rules": _CANDIDATE_PII,
    },
    "bank_details": {
        "label": "coordonnées bancaires",
        "hint": "masque IBAN/BIC/RIB (garde les 4 derniers).",
        "rules": [{"fields": ["iban", "bic", "rib"], "action": "mask", "keep_last": 4}],
    },
}
