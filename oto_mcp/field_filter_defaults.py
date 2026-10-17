"""Rédaction de champs : **quasi rien par défaut** + templates applicables en 1 clic.

Décision (2026-06-22) : on ne redacte **AUCUN** connecteur par défaut. La rédaction
est *disponible* partout (le middleware `FieldRedactionMiddleware` l'applique dès qu'une
org pose une politique pour un service), mais elle ne s'active que sur décision explicite
de l'org. Raison : la PII n'est pas toujours un risque — sur un CRM/inbox/annuaire
légal, c'est le but. Et le matching par clé est aveugle au contexte (faux positifs).

**Amendement du 2026-09-17 — `SERVER_DEFAULTS` n'est plus vide** (signal d'usage
#1063). La décision de 2026-06-22 tenait tant qu'un connecteur pouvait, en plus, RETIRER
en dur ce qu'il jugeait trop lourd : c'est ce que faisait `payfit`, et ça revenait à
décider à la place de l'entreprise qui possède ses données. On a inversé les deux
leviers : le connecteur sert **tout** ce que l'API expose, et ce qui protège est un
défaut serveur **liftable**, ici. Ce qui n'a pas changé : la rédaction reste une
POLITIQUE, pas un retrait — l'org_admin la lève connecteur par connecteur.

**Amendement du 2026-10-06 — un plancher ne se lève qu'en NOMMANT ses champs**
(signal oto #1269). Jusque-là la politique d'org remplaçait le plancher, et `rules: []`
levait tout : une org a posé ce geste pour ouvrir ses documents PayFit et a sorti du
même coup le NIR et l'IBAN de tous ses salariés dans toutes les réponses. Désormais la
politique s'AJOUTE au plancher (`bloc_effectif`) : un champ du plancher ne sort en clair
que nommé dans `unmask` (ou visé par une règle de l'org), et les documents s'ouvrent
par un consentement à part, `documents: true`, qui ne lève aucun champ.

Un défaut ne se pose donc ici qu'à trois conditions : la donnée est sensible **par
nature** (RGPD art. 9, identifiant national, coordonnée bancaire), le connecteur la
sert par construction, et le **nom de feuille est sans ambiguïté** — `FieldFilter`
matche par nom de clé, à toute profondeur, sans savoir sous quel parent il est. Un
champ dont le nom est partagé (`type`, `name`, `id`) ne peut PAS recevoir de défaut :
il faut que le connecteur le serve sous un nom qui n'appartient qu'à lui (cf.
`absence_type` dans `tools/payfit_socle.py`), sinon la règle abîme ses homonymes.

Pour ne pas re-saisir des règles utiles, on expose aussi des **TEMPLATES** nommés
(jeux de règles prêts) que l'UI applique en un clic — ex. « anonymisation candidat »
pour le recrutement. Appliquer un template = poser une politique d'org normale (rien
de magique).

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
# ⚠️ Calé sur la FORME RÉELLE observée (`linkedin_unipile_profile` : `contact_info.emails`/
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

# PayFit (paie et RH) — le PLANCHER d'un logiciel de paie, posé le 2026-09-17 quand le
# connecteur a cessé de retirer en dur (cf. `tools/payfit.py`). Trois familles, et rien
# d'autre : tout le reste de la paie (rémunérations, bulletins, coût employeur,
# contrats, temps de travail, mutuelle, coordonnées, naissance) sort SANS masque —
# c'est ce que l'entreprise vient chercher.
#
# ⚠️ Chaque nom ci-dessous a été vérifié SANS HOMONYME dans les réponses PayFit : le
# moteur matche la feuille à toute profondeur, donc un nom partagé abîmerait ses
# voisins. C'est exactement pourquoi le type d'une absence est servi en
# `absence_type` et pas en `type` (qui désigne aussi le type d'un e-mail, d'un
# téléphone, d'une adresse, d'un code analytique et d'un document).
_PAYFIT_PII: list[dict] = [
    # NIR, et le NTT qui en tient lieu avant immatriculation. Masque TOTAL : les
    # premiers chiffres d'un NIR portent sexe, année et département de naissance —
    # un `keep_first` en ferait fuiter la partie la plus signifiante.
    {"fields": ["socialSecurityNumber", "numeroSecuriteSociale",
                "temporaryTechnicalNumber", "numeroTechniqueTemporaire"],
     "action": "mask"},
    # Coordonnées bancaires du salarié. `preserve: iban` garde de quoi reconnaître un
    # compte sans pouvoir l'utiliser.
    {"fields": ["iban"], "action": "mask", "preserve": "iban"},
    {"fields": ["bic"], "action": "mask"},
    # Motif d'une absence : maladie, accident du travail, maternité/paternité sont des
    # données de SANTÉ (RGPD art. 9). Le moteur ne sait pas masquer « seulement les
    # motifs médicaux » — il n'a aucune règle conditionnée à la valeur — donc le champ
    # part en entier. `absence_category` (ordinary_leave | restricted), lui, n'est pas
    # masqué : il suffit à planifier une charge sans lire un motif.
    {"fields": ["absence_type"], "action": "mask"},
]

# Défauts serveur, par service : le plancher. Sans politique d'org, il s'applique tel
# quel ; avec, la politique s'y AJOUTE et ne lève un champ qu'en le nommant
# (`bloc_effectif`, décision du 2026-10-06).
# Malibou : mêmes natures que PayFit. Chaque nom vérifié sans homonyme dans les
# réponses de la Public API v1 : le NIR s'appelle `ssn` dans la liste des
# collaborateurs et `socialSecurityNumber` dans la fiche ; IBAN et BIC sous
# `bankAccount`. Le type d'une absence (code de congé, maladie comprise) est servi
# en `absence_type` par `tools/malibou.py` — `type` a des homonymes.
_MALIBOU_PII: list[dict] = [
    {"fields": ["ssn", "socialSecurityNumber"], "action": "mask"},
    {"fields": ["iban"], "action": "mask", "preserve": "iban"},
    {"fields": ["bic"], "action": "mask"},
    {"fields": ["absence_type"], "action": "mask"},
]

SERVER_DEFAULTS: dict[str, dict] = {
    "payfit": {"rules": _PAYFIT_PII},
    "malibou": {"rules": _MALIBOU_PII},
}

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
    "free_text": {
        "label": "données sensibles dans le texte",
        "hint": "remplace, dans TOUT le texte des réponses (tickets, fils, notes), les "
                "valeurs sensibles par un jeton stable ([NIR-3f2a9c]) : NIR, IBAN, carte, "
                "e-mail, téléphone, secret annoncé, et — modèle de l'instance — noms, "
                "adresses, identifiants dits dans une phrase.",
        "rules": [{"fields": ["*"], "action": "scrub",
                   "detect": ["nir", "iban", "card", "email", "phone", "secret",
                              "person", "address", "credential"]}],
    },
}


# Sonde : une valeur qu'aucune donnée réelle ne porte. Un champ est « masqué » par une
# politique si son `apply` la réécrit ou la retire — éprouvé par le chemin même de la
# sortie, jamais en relisant les règles (un `keep_last`, un `preserve` ou une action
# inconnue, fail-safe en masque total, se lisent mal ; ils s'éprouvent bien).
_SONDE = "sonde-de-redaction"


def champs_du_plancher(service: str) -> list[str]:
    """Les champs que le défaut serveur de `service` masque, sous leur nom de sortie
    (vide pour un service sans plancher)."""
    return [c for regle in SERVER_DEFAULTS.get(service, {}).get("rules", [])
            for c in regle["fields"]]


def masque(ff, champ: str) -> bool:
    """Vrai si le `FieldFilter` `ff` réécrit ou retire `champ`."""
    return ff.apply({champ: _SONDE}).get(champ) != _SONDE


def champs_du_plancher_en_clair(service: str, ff) -> list[str]:
    """Les champs du plancher de `service` que `ff` laisse sortir tels quels."""
    return [c for c in champs_du_plancher(service) if not masque(ff, c)]


# Services dont des DOCUMENTS (fichiers, extraits verbatim) portent les champs du
# plancher : un filtre ne voit pas l'intérieur d'un fichier, donc ils ne sortent que sur
# consentement NOMMÉ de l'org (`documents: true` dans sa politique du service).
SERVICES_A_DOCUMENTS = frozenset({"payfit"})


def bloc_effectif(service: str, bloc_org: dict | None) -> dict | None:
    """Le bloc de règles qui s'applique à `service`. Seule source de la cascade : la
    sortie (`access.resolve_field_filter`), le dry-run et l'écriture la lisent ici.

    - Sans politique d'org : le défaut serveur (le plancher), sinon rien.
    - Service SANS plancher : la politique de l'org est autoritaire, `rules: []` ne
      masque rien.
    - Service AVEC plancher (décision du 2026-10-06, signal oto #1269, qui remplace
      celle du 2026-09-18 « `rules: []` lève tout ») : la politique de l'org s'AJOUTE
      au plancher. Un champ du plancher ne sort en clair que s'il est NOMMÉ — dans
      `unmask`, ou par une règle de l'org qui le vise (elle remplace alors la règle du
      plancher pour ce champ). Aucun geste ne lève un plancher sans le nommer."""
    plancher = SERVER_DEFAULTS.get(service)
    if bloc_org is None or not plancher:
        return bloc_org if bloc_org is not None else plancher
    nommes = {c.lower() for c in bloc_org.get("unmask") or []}
    nommes |= {c.lower() for r in bloc_org.get("rules", []) for c in r.get("fields", [])}
    garde = []
    for regle in plancher["rules"]:
        champs = [c for c in regle["fields"] if c.lower() not in nommes]
        if champs:
            garde.append({**regle, "fields": champs})
    out: dict = {"rules": list(bloc_org.get("rules", [])) + garde}
    if bloc_org.get("salt"):
        out["salt"] = bloc_org["salt"]
    return out


def leve_sans_nommer(service: str, bloc_org: dict | None) -> bool:
    """Vrai pour une politique STOCKÉE avant le 2026-10-06 qui levait le plancher sans
    rien nommer (`rules: []` seul) : elle ne lève plus rien, et sa lecture le
    journalise. Une écriture de cette forme est refusée (`floor_lift_must_be_explicit`)."""
    return (bool(SERVER_DEFAULTS.get(service)) and bloc_org is not None
            and not bloc_org.get("rules") and not bloc_org.get("unmask")
            and not bloc_org.get("documents"))


def filtre(bloc: dict | None):
    """Le `FieldFilter` d'un bloc (vide si le bloc est vide ou absent)."""
    from oto.tools.common import FieldFilter

    from . import scrub_model

    if not bloc:
        return FieldFilter()
    regles = bloc.get("rules", [])
    # Le modèle n'est construit que pour une règle `scrub` qui le demande : une
    # déclaration d'instance bancale ne retient que les politiques qui s'en servent.
    detecteur = scrub_model.detecteur() if scrub_model.sortes_de_modele(regles) else None
    return FieldFilter(rules=regles, salt=bloc.get("salt"), detector=detecteur)
