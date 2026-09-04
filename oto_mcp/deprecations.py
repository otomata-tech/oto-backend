"""Les noms SERVIS qu'on remplace — et LA date à laquelle l'ancien s'en va (#519).

Le produit a changé de mot (#519) : il dit **guide** (ADR 0042, le guide =
primitive unique d'instruction) et **procédure** pour ce qui s'exécute. Le lot A a
retiré l'ancien de l'interne sans changer un octet servi. Le lot B renomme les
SURFACES — et une surface ne se renomme pas, elle se DOUBLE : le nouveau nom naît à
côté de l'ancien, l'ancien continue de répondre, et la date de son retrait est
écrite là où le consommateur la lit.

**Ce module est cette date.** Elle vit ici et nulle part ailleurs ; chaque avis de
dépréciation servi la recopie depuis `RETRAIT`. Décaler le retrait est alors un
geste — changer cette constante — et non une chasse aux chaînes de caractères dans
quarante descriptions, dont on oublierait trois.

Pourquoi un TAG et pas une date de merge (`RETRAIT` se lit « premier tag `vX.Y.Z`
posé à partir de cette date ») : `main` est la PREPROD. Un alias retiré au merge
serait retiré du serveur que les intégrateurs sondent, deux mois de préavis
annoncés et zéro jour servi. Le retrait est le lot D — issue #526, qui porte la
liste complète de ce que la date emporte.

⚠️ **Ce module n'est pas un fourre-tout de compatibilité.** Il ne porte que des
renommages de vocabulaire à durée de vie FINIE, chacun avec sa contrepartie dans
#526. Un alias sans date de retrait est un second nom permanent : ça se décide, ça
ne s'ajoute pas ici.

⚠️ **Et ce qu'il ne tient PAS — à ne pas croire tenu.** Depuis #767 la DURÉE du
préavis vaut l'engagement contractuel (Art 8.2, deux mois). Deux écarts restent
ouverts, et ils ne se corrigent pas avec une constante :

1. **Le module ne couvre que les renommages ; l'engagement, lui, est plus large.**
   Il porte sur toute rupture d'interface (#767) — donc aussi sur un paramètre qui
   devient obligatoire, un champ retiré, un type changé. Rien de tout cela n'a ici
   d'alias, de date de retrait ni d'en-tête `Sunset`. Le cas vécu (rendre
   `resource_type` obligatoire, #756)
   a d'ailleurs été réglé par une autre voie : la surface s'est **DOUBLÉE**
   (l'héritée intacte, la stricte en bêta, #774/#780) au lieu de se durcir. C'est
   aujourd'hui la seule réponse outillée à une rupture qui n'est pas un
   renommage — et elle ne passe par aucun préavis daté.
2. **Le préavis est PASSIF.** `Deprecation` / `Sunset` et l'avis en tête de
   description se voient quand le consommateur inspecte ses réponses ; rien n'est
   poussé vers lui. Rendre le préavis actif demande le canal de notification qui
   manque à toute la plateforme (#766), pas un réglage de ce module.
"""
from __future__ import annotations

import calendar
import datetime
from typing import NamedTuple
from urllib.parse import quote

# ── Le préavis : sa durée, et d'où elle vient ───────────────────────────────
# **Cette durée est un ENGAGEMENT CONTRACTUEL, pas un réglage.** L'Art 8.2 du contrat
# de service promet un préavis de DEUX MOIS avant toute rupture d'interface. C'est la
# durée qu'on a écrite au client : la raccourcir est un manquement, pas une
# optimisation. Elle a valu 30 jours du 29/08 au 01/09/2026 — l'écart, et la référence
# de l'article, sont dans oto-backend#767. Un test en fait un plancher
# (`tests/test_alias_deprecies_outils.py`), pour que personne ne la « rationalise » un
# jour sans savoir ce qu'il touche : allonger reste libre, descendre sous deux mois
# rougit.
#
# ⚠️ La formulation exacte de l'article ne se recopie pas ici, et ce commentaire n'en
# tient pas lieu : pour arbitrer un cas limite — une rupture imposée par la sécurité,
# le point de départ du préavis — aller lire la pièce.
PREAVIS_MOIS = 2

# Le jour où le préavis a commencé à être SERVI EN PRODUCTION — pas celui du merge.
# Même raison que le retrait au tag (ci-dessus) : `main` est la préproduction, et un
# préavis que le consommateur ne peut pas encore lire n'a averti personne. Les trois
# lots d'alias ont été mergés le 28/08/2026 et sont partis en production avec le tag
# `v1.159.0`, le 29/08/2026 — c'est ce jour-là que la fenêtre s'ouvre.
ANNONCE = datetime.date(2026, 8, 29)


def _plus_de_mois(depart: datetime.date, mois: int) -> datetime.date:
    """`depart` + `mois` mois CALENDAIRES — jamais une approximation en jours.

    Deux mois comptés en 60 jours tombent un jour trop tôt neuf fois sur douze :
    c'est exactement la famille d'écart que #767 et #768 corrigent, et elle penche
    toujours du même côté — contre celui à qui le préavis est dû.
    """
    rang = depart.month - 1 + mois
    annee, mois_cible = depart.year + rang // 12, rang % 12 + 1
    jour = min(depart.day, calendar.monthrange(annee, mois_cible)[1])
    return datetime.date(annee, mois_cible, jour)


# Premier tag `vX.Y.Z` posé à partir de cette date. DÉRIVÉE, pas écrite à la main :
# décaler le retrait, c'est décider une date d'annonce ou une durée de préavis —
# jamais poser un jour de calendrier au hasard.
RETRAIT = _plus_de_mois(ANNONCE, PREAVIS_MOIS)


# ── Retour OAuth : convention unifiée (lot oto-backend#670) ─────────────────
# Un second renommage de surface, INDÉPENDANT du couple doctrine→guide ci-dessus :
# la query string du retour après consentement OAuth (`?connector=<nom>&connect=
# connected|error|forbidden`, généralisée depuis la forme salesforce) remplace
# cinq conventions locales — dont deux replis cassés (atlassian, folk). zoho et
# google servaient déjà un suffixe LU par le dashboard (`?zoho=connected`,
# `?google=connected`) : il se double, à la manière de #519, le temps d'un
# préavis — mais PAS le même préavis : celui-ci n'a pas encore commencé à être
# SERVI EN PRODUCTION, donc son horloge n'a pas de raison de partager `ANNONCE`,
# qui date du tag `v1.159.0` d'un renommage sans rapport.
#
# ⚠️ `ANNONCE_RETOUR_OAUTH` reste `None` tant que CE lot n'est pas réellement
# tagué en production — ni cette session ni Alexis ne décide d'un tag depuis ici
# (`docs/alias-deprecies.md` : « un tag, pas un merge, aux deux bouts »). Tant
# qu'elle est `None`, `RETRAIT_RETOUR_OAUTH` reste `None` lui aussi — rien à
# comparer à une date fantôme — et `dans_le_preavis_retour_oauth()` rend
# TOUJOURS `True` : le doublage reste actif sans discontinuer, ce qui est le
# comportement sûr par défaut (servir trop longtemps l'ancienne forme ne casse
# personne ; arrêter trop tôt casse le dashboard).
# TODO(#670) : poser `ANNONCE_RETOUR_OAUTH` à la date du tag qui déploie ce lot
# — alors, et seulement alors, `RETRAIT_RETOUR_OAUTH` se dérive comme `RETRAIT`
# ci-dessus, avec le MÊME `PREAVIS_MOIS` (l'Art 8.2 ne distingue pas de quelle
# surface il s'agit).
ANNONCE_RETOUR_OAUTH: "datetime.date | None" = None
RETRAIT_RETOUR_OAUTH: "datetime.date | None" = (
    _plus_de_mois(ANNONCE_RETOUR_OAUTH, PREAVIS_MOIS) if ANNONCE_RETOUR_OAUTH else None
)


def dans_le_preavis_retour_oauth(aujourd_hui: "datetime.date | None" = None) -> bool:
    """`True` tant que l'ancien suffixe de retour OAuth (zoho, google) doit rester
    servi À CÔTÉ du nouveau — voir `auth.flow.connector_return_suffix`.

    Sans date de retrait posée (voir `ANNONCE_RETOUR_OAUTH` ci-dessus), rend
    TOUJOURS `True` : ce lot n'a pas encore de fenêtre à fermer, donc rien ne doit
    la fermer tout seul avant qu'Alexis/le superviseur n'ait posé la date, au tag."""
    if RETRAIT_RETOUR_OAUTH is None:
        return True
    return (aujourd_hui or datetime.date.today()) < RETRAIT_RETOUR_OAUTH


# ── Outils MCP (lot B1) ─────────────────────────────────────────────────────
# ancien nom SERVI → nom canonique. L'ancien reste listé et appelable jusqu'au
# retrait ; le bord du protocole (`middleware/alias.ToolAliasMiddleware`) rétablit
# le canonique AVANT que quoi que ce soit d'autre ne le lise, exactement comme pour
# le renommage par tenant — donc rien en aval n'apprend que l'alias existe : les
# gates, la denylist de visibilité, le journal `tool_calls` et les références
# `<tool:slug>` continuent de voir un seul nom pour un seul outil.
TOOLS: dict = {
    "oto_admin_doctrine": "oto_admin_guide",
    # Deux verbes de CONNEXION rangés sous le préfixe transverse (02/09/2026) : le
    # gate par connecteur résout au namespace du NOM, donc `oto_…` les servait à
    # tous les comptes — y compris ceux qui n'ont ni Salesforce ni Zoho. C'est ce
    # qu'Alexis a repéré en trouvant dans ses outils un connecteur qu'il n'utilise
    # pas. Renommés sous leur connecteur ; l'ancien nom reste appelable parce
    # qu'une procédure d'org le référence (mesuré, pas supposé).
    "oto_salesforce_connect": "salesforce_connect",
    "oto_zoho_connect": "zoho_connect",
}


def date_de_retrait() -> str:
    """La date de retrait telle qu'elle est SERVIE (JJ/MM/AAAA)."""
    return RETRAIT.strftime("%d/%m/%Y")


def avis(canonique: str) -> str:
    """L'avis qui PRÉFIXE la description d'un nom déprécié.

    En tête, pas en queue : beaucoup de clients tronquent une description longue,
    et un avis de dépréciation lu après 400 caractères n'a averti personne. C'est
    aussi la première chose que le modèle lit quand il choisit son outil.
    """
    return (f"Déprécié : utilisez `{canonique}` (retrait le {date_de_retrait()}). ")


def tool_canonique(nom: str) -> str:
    """Le nom que le SERVEUR connaît. Un nom non déprécié passe inchangé."""
    return TOOLS.get(nom, nom)


def tools_deprecies_de(canonique: str) -> tuple:
    """Les anciens noms d'un outil, à servir à côté du sien. Vide si aucun."""
    return tuple(sorted(a for a, c in TOOLS.items() if c == canonique))


# ── Chemins REST (lot B2) ───────────────────────────────────────────────────
class AliasRest(NamedTuple):
    """Un ancien chemin REST, monté en **308** vers son chemin d'aujourd'hui.

`ancien` et `nouveau` s'écrivent chacun avec SES propres placeholders — ceux de
    la route réellement montée de chaque côté, pour que le chemin publié dans
    `/openapi.json` et dans la doc soit celui qu'on lit dans la table de routes.
    `params` porte l'écart quand un placeholder change de nom en route ; seule la
    VALEUR capturée voyage.
    """
    verbe: str
    ancien: str
    nouveau: str
    # placeholder de `ancien` → placeholder de `nouveau`, quand ils diffèrent.
    # Jamais muté (une valeur par défaut de NamedTuple est partagée).
    params: dict = {}
    # L'`operationId` HISTORIQUE de ce chemin, quand plus personne ne le réclame.
    #
    # ⚠️ L'`operationId` suit la CAPACITÉ, pas le chemin — c'est le nom de méthode
    # qu'un client généré s'est donné pour une opération. Quand la clé de capacité ne
    # change pas (`library.list`), le NOUVEAU chemin hérite de l'id : regénérer le
    # client ne renomme rien, seule l'URL bouge. C'est le bon résultat, et ça
    # interdit de donner le même id à l'entrée dépréciée (un `operationId` est unique
    # dans un document OpenAPI — garde-fou `test_openapi.py`). Elle en reçoit alors un
    # dérivé de son chemin, laissé à `""` ici.
    #
    # Un seul cas le renseigne : la clé a changé AUSSI (`org.doctrine.get` →
    # `org.guide.get`), l'id historique n'est plus réclamé par personne, et le garder
    # sur l'ancien chemin laisse un client déjà généré retrouver sa méthode.
    operation_id: str = ""


REST: tuple = (
    # Bibliothèque publique de guides (marketplace) — servie sans auth, consommée
    # par le build de la vitrine et par un `fetch` de navigateur.
    AliasRest("GET", "/api/doctrines/library", "/api/guide-library"),
    AliasRest("GET", "/api/doctrines/library/{slug}", "/api/guide-library/{slug}"),
    # ⚠️ ORDRE — `library` AVANT `{doctrine_id}` : le second capture un segment, et
    # servirait `library` comme un identifiant. C'est exactement ce que faisait la
    # table d'avant ce lot (le chemin `…/doctrines/library` y était inatteignable).
    AliasRest("GET", "/api/me/doctrines/library", "/api/me/guide-library"),
    AliasRest("GET", "/api/me/doctrines/library/{slug}",
              "/api/me/guide-library/{slug}"),
    AliasRest("DELETE", "/api/me/doctrines/library/{id}",
              "/api/me/guide-library/{id}"),
    AliasRest("POST", "/api/me/doctrines/publish", "/api/me/guide-library/publish"),
    AliasRest("POST", "/api/me/doctrines/fork", "/api/me/guide-library/fork"),
    AliasRest("GET", "/api/me/doctrines/{doctrine_id}", "/api/me/guides/{guide_id}",
              {"doctrine_id": "guide_id"}, "org_doctrine_get_get"),
)


def cible(alias: AliasRest, path_params: dict, query: str = "") -> str:
    """Le chemin de destination d'un alias, params de chemin injectés.

    La query string est REPORTÉE telle quelle : la vitrine appelle
    `…/library?limit=200`, et un 308 qui la perdrait rendrait 100 entrées au lieu
    de 200 — une régression qu'aucun code d'erreur ne signale.
    """
    chemin = alias.nouveau
    for nom, valeur in (path_params or {}).items():
        cible_nom = alias.params.get(nom, nom)
        chemin = chemin.replace("{" + cible_nom + "}", quote(str(valeur), safe=""))
    return f"{chemin}?{query}" if query else chemin


# ── Clés de capacité (lot B2) ───────────────────────────────────────────────
# ancienne clé → clé d'aujourd'hui. ⚠️ **Renommées SANS alias**, et c'est un choix :
# une clé de capacité ne sort du serveur qu'à deux endroits — `/api/admin/capabilities`
# (le navigateur d'objets de la plateforme, réservé à l'admin plateforme, sans
# intégrateur tiers) et l'`operationId` de `/openapi.json`. Ce second est le seul qui
# engage quelqu'un dehors, et il est préservé : l'entrée DÉPRÉCIÉE du chemin d'avant
# le porte (`AliasRest.operation_id`). Il n'y a donc rien à aliaser.
CAPACITES: dict = {
    "org.doctrine.get": "org.guide.get",
    "org.doctrine.admin_get": "org.guide.admin_get",
    "org.doctrine.admin_list": "org.guide.admin_list",
    "admin.doctrine": "admin.guide",
}


# ── Clés de réponse (lot B3) ────────────────────────────────────────────────
# ancienne clé SERVIE → clé d'aujourd'hui. Le doublage est **additif** : le handler
# écrit la clé d'aujourd'hui, `avec_anciennes_cles` recopie l'ancienne à côté. Au lot
# D on retire l'appel, et les anciennes disparaissent d'un geste.
#
# ⚠️ Une clé de réponse est ce qu'un client LIT. La renommer sec, c'est rendre `null`
# là où il attendait une valeur — sans erreur, sans log, sans que rien ne s'allume.
# C'est la panne la plus chère de la liste, et la plus silencieuse.
CLES: dict = {
    "doctrine_id": "guide_id",
    "doctrine_version": "guide_version",
    "doctrine_ref_count": "guide_ref_count",
    "doctrines": "guides",
    "group_doctrine": "group_guide",
    "doctrine": "guide",
}


def avec_les_deux_noms(payload: dict) -> dict:
    """Chaque clé de `CLES` servie sous SES DEUX noms, quel que soit celui écrit.

    Bidirectionnel à dessein : certains payloads naissent déjà en vocabulaire
    d'aujourd'hui (un handler qu'on vient d'écrire), d'autres en vocabulaire d'hier
    (une ligne SQL, dont la COLONNE ne se renomme qu'au lot B4 — la base est partagée
    prod/preprod). Deux fonctions symétriques auraient créé deux façons de se tromper.

    **NON récursif, et jamais posé globalement.** Il s'appelle site par site, sur des
    payloads qu'on a nommés. Un passage automatique sur toute réponse traverserait
    aussi les données de l'utilisateur — la ligne d'un tableau dont il a nommé une
    colonne « doctrine » gagnerait une colonne fantôme. Une compatibilité ne doit
    jamais inventer un champ dans la donnée de quelqu'un.

    Une clé déjà présente n'est jamais écrasée : le producteur garde le dernier mot.
    """
    out = dict(payload)
    for ancienne, actuelle in CLES.items():
        if actuelle in out and ancienne not in out:
            out[ancienne] = out[actuelle]
        elif ancienne in out and actuelle not in out:
            out[actuelle] = out[ancienne]
    return out


def lignes_avec_les_deux_noms(lignes) -> list:
    """`avec_les_deux_noms` sur chaque ligne d'une liste (un journal de runs)."""
    return [avec_les_deux_noms(l) if isinstance(l, dict) else l for l in lignes or ()]


# ── Codes d'erreur (lot B3) ─────────────────────────────────────────────────
# ancien code → code d'aujourd'hui. Un code d'erreur ne se DOUBLE pas — il n'y a
# qu'un champ `error` — donc le nouveau prend la place, et l'ancien est conservé dans
# `details.legacy_code`. Un client qui teste `error == "unknown_doctrine"` a un mois
# pour aller lire `details.legacy_code`, ou mieux, le nouveau code.
CODES: dict = {
    "unknown_doctrine": "unknown_guide",
}


def details_avec_code_dhier(code_actuel: str, details=None) -> dict:
    """Les `details` d'un refus, augmentés du code d'hier quand il y en a un."""
    ancien = next((a for a, n in CODES.items() if n == code_actuel), None)
    if not ancien:
        return details or {}
    return {**(details or {}), "legacy_code": ancien}


# ── Noms de schéma OpenAPI (lot B3) ─────────────────────────────────────────
# ancien nom de composant → nom d'aujourd'hui. Publié dans `components.schemas` comme
# un `$ref` vers le nouveau, marqué déprécié : un client généré qui référence
# `#/components/schemas/DoctrineMeta` continue de résoudre.
#
# ⚠️ Cette table ne porte que les noms qui étaient VRAIMENT des composants. Un modèle
# `Output` de premier niveau n'en est pas un — son schéma est INLINE dans la réponse
# 200, et son nom n'y apparaît que comme `title`, ce qu'aucun `$ref` ne peut viser.
# `DoctrineView` était dans ce cas ; le renommer n'engage personne.
SCHEMAS: dict = {
    "DoctrineMeta": "GuideMeta",
}
