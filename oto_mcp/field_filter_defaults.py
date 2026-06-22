"""Politiques de redaction de champs par défaut, côté serveur.

`~/.otomata/config.yaml` (la source de `FieldFilter.from_config`) est absente du
serveur ; on pose donc ici un **plancher PII explicite** par connecteur. Ce défaut
ne s'applique que tant que l'org n'a **rien** configuré pour le service : dès que
l'org_admin pose une politique (via le dashboard / `oto_set_org_field_filters`),
elle devient autoritaire (décision « contrôle total org »). Appliqué à la frontière
des tools par `middleware.FieldRedactionMiddleware`.

Forme = celle d'un bloc `field_filters.<service>` :
    { "salt": str?, "rules": [ {fields, action, ...} ] }

`FieldFilter` (oto-core) matche par **nom de clé feuille**, récursivement et insensible
à la casse → un même jeu de règles couvre les variantes de nommage (snake/camel/kebab)
qu'émettent des connecteurs différents.
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

SERVER_DEFAULTS: dict[str, dict] = {
    # Silae (paie FR) : masque les coordonnées bancaires (rarement utiles à un agent
    # d'analyse), garde noms/montants. Hérité du `_REDACT` jadis hardcodé dans
    # tools/silae.py.
    "silae": {
        "rules": [
            {"fields": ["iban", "bic", "rib"], "action": "mask", "keep_last": 4},
        ],
    },
    # Connecteurs « recrutement » : anonymisation candidat par défaut (overridable par
    # l'org, champ par champ, depuis le dashboard — « voir en clair »).
    "unipile": {"rules": _CANDIDATE_PII},
    "ashby": {"rules": _CANDIDATE_PII},
    "greenhouse": {"rules": _CANDIDATE_PII},
    "lever": {"rules": _CANDIDATE_PII},
    "recruitee": {"rules": _CANDIDATE_PII},
    "teamtailor": {"rules": _CANDIDATE_PII},
}
