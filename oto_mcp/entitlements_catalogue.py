"""Le catalogue des droits déclarés (ADR 0070 §7) — la liste FERMÉE des clés que le cœur
sait appliquer, et la forme de leur valeur.

Un droit déclaré est une ligne de `org_entitlements` posée par un producteur (le
commerce, un admin) et relue par le cœur à chaque usage (`access/entitlements.py`). Le
catalogue vit ICI, dans le cœur et en code : un producteur ne pose jamais une clé que le
cœur ne connaît pas — la pose la refuse (`db/entitlements.grant`), et la déclaration des
défauts d'instance aussi (`access/entitlements.defauts_de_l_instance`).

Deux genres de valeur, toujours un entier, jamais vide :

- **oui/non** : `1` = oui, `0` = non ;
- **nombre** : un plafond ou un quota, `>= 0` ; « sans plafond » s'écrit
  `SANS_PLAFOND`, une valeur EXPLICITE — jamais une absence de valeur (une valeur vide
  qui voulait dire « pas d'avis » a déjà laissé un plan n'écrire rien, #805).

Module PUR : il ne lit que le registre des connecteurs (`providers`), pour valider le
suffixe d'une clé `platform_key:<connecteur>`.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from . import providers

# « Sans plafond », dit explicitement : le plus grand INTEGER de PostgreSQL. Le cumul
# prenant le maximum, il l'emporte sur tout plafond chiffré.
SANS_PLAFOND = 2_147_483_647


class Genre(str, Enum):
    OUI_NON = "oui_non"
    NOMBRE = "nombre"


@dataclass(frozen=True)
class Droit:
    cle: str
    genre: Genre
    description: str


UNIPILE = "unipile"
PLATFORM_UNMETERED = "platform_unmetered"
UNIPILE_SEATS = "unipile_seats"
# ⚠️ HÉRITÉ, SANS LECTEUR : le cœur ne connaît aucun plafond de membres (tout membre a
# accès et peut être invité). Posé aujourd'hui par un abonnement réglé hors plateforme
# (licences d'un contrat) ; retiré du catalogue quand le commerce posera les droits
# payants PAR PERSONNE.
MEMBERS_MAX = "members_max"
# Préfixe de la famille `platform_key:<connecteur>` : l'accès à la clé de plateforme d'un
# connecteur, valeur = quota par jour (0 = pas d'accès).
PLATFORM_KEY_PREFIX = "platform_key:"
# La clé joker de la déclaration des défauts : couvre tout `platform_key:<connecteur>`
# qui n'a pas sa propre surcharge. Ce n'est PAS une clé posable.
PLATFORM_KEY_JOKER = PLATFORM_KEY_PREFIX + "*"

# Les étiquettes `source` qu'un producteur peut poser — liste FERMÉE. Informatives
# (affichage, reprise), jamais un paramètre de la règle d'application : `value_for` prend
# le plus généreux, toutes sources confondues.
# L'essai n'est PAS un droit déclaré (docs/droits-declares.md) : `trial` reste une
# étiquette acceptée, que rien dans le cœur ne produit ni ne lit.
SOURCES: tuple[str, ...] = ("subscription", "trial", "offered", "partner", "contract")

FIXES: dict[str, Droit] = {d.cle: d for d in (
    Droit(UNIPILE, Genre.OUI_NON, "messagerie hébergée"),
    Droit(PLATFORM_UNMETERED, Genre.OUI_NON,
          "quotas levés sur les clés de plateforme"),
    Droit(UNIPILE_SEATS, Genre.NOMBRE, "nombre de comptes de messagerie"),
    Droit(MEMBERS_MAX, Genre.NOMBRE, "hérité, sans lecteur : licences d'un contrat"),
)}


def platform_key(connecteur: str) -> str:
    """La clé `platform_key:<connecteur>`."""
    return PLATFORM_KEY_PREFIX + connecteur


def droit(cle: str) -> Droit:
    """Le droit du catalogue pour `cle`. Lève `ValueError` (code
    `entitlement_unknown_key`) hors catalogue — dont un connecteur inconnu du registre."""
    if cle in FIXES:
        return FIXES[cle]
    if cle.startswith(PLATFORM_KEY_PREFIX):
        connecteur = cle[len(PLATFORM_KEY_PREFIX):]
        if connecteur in providers.REGISTRY:
            return Droit(cle, Genre.NOMBRE, f"quota par jour de la clé de plateforme "
                                            f"`{connecteur}`")
        raise ValueError(f"entitlement_unknown_key: {cle!r} — `{connecteur}` n'est pas "
                         "un connecteur du registre")
    raise ValueError(f"entitlement_unknown_key: {cle!r} hors catalogue (clés : "
                     f"{', '.join(sorted(FIXES))}, {PLATFORM_KEY_PREFIX}<connecteur>)")


def est_du_catalogue(cle: str) -> bool:
    """`cle` nomme-t-elle un droit du catalogue — une clé FIXE ou la famille
    `platform_key:` ? Le territoire des droits déclarés, qu'oto-commerce pose seul
    (#1097) : ni un don d'option (`option_comps`) ni sa lecture n'y touchent."""
    return cle in FIXES or cle.startswith(PLATFORM_KEY_PREFIX)


def valeur_valide(cle: str, valeur: object) -> int:
    """La valeur à POSER pour `cle`, vérifiée contre son genre, ou `ValueError` nommée :
    `entitlement_unknown_key`, `entitlement_value_required` (vide),
    `entitlement_value_invalid` (hors genre)."""
    return _verifier(cle, droit(cle).genre, valeur)


def valeur_de_defaut(cle: str, valeur: object) -> int:
    """La valeur DÉCLARÉE par l'instance pour `cle` — une clé du catalogue ou le joker
    `platform_key:*` (qui a le genre des `platform_key:<connecteur>`)."""
    genre = Genre.NOMBRE if cle == PLATFORM_KEY_JOKER else droit(cle).genre
    return _verifier(cle, genre, valeur)


def _verifier(cle: str, genre: Genre, valeur: object) -> int:
    if valeur is None:
        raise ValueError(f"entitlement_value_required: {cle!r} — une valeur est "
                         "obligatoire (oui/non : 1 ou 0 ; nombre : >= 0)")
    if isinstance(valeur, bool) or not isinstance(valeur, int):
        raise ValueError(f"entitlement_value_invalid: {cle!r} attend un entier, "
                         f"reçu {valeur!r}")
    if genre is Genre.OUI_NON and valeur not in (0, 1):
        raise ValueError(f"entitlement_value_invalid: {cle!r} est oui/non (1 ou 0), "
                         f"reçu {valeur}")
    if not 0 <= valeur <= SANS_PLAFOND:
        raise ValueError(f"entitlement_value_invalid: {cle!r} attend un nombre entre 0 "
                         f"et {SANS_PLAFOND} (sans plafond), reçu {valeur}")
    return valeur
