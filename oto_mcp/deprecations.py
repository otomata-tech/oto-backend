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
from typing import Any, NamedTuple, Optional
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


# Premier tag `vX.Y.Z` posé à partir de cette date. Le préavis DÛ reste dérivé (un
# test en fait le plancher) ; le retrait servi peut être plus TARD, jamais plus tôt.
# Reporté du 29/10 au 30/11/2026, décidé le 30/10/2026 : le dashboard lit encore les
# clés d'hier, et le journal `tool_calls` ne voit pas les anciens noms d'outils (le
# bord du protocole les traduit avant lui) — l'usage reste à mesurer avant le lot D
# (#526).
PREAVIS_DU = _plus_de_mois(ANNONCE, PREAVIS_MOIS)
RETRAIT = max(PREAVIS_DU, datetime.date(2026, 11, 30))


# ── Le connecteur Custom API renommé `api` (ex-`http`, 12/11/2026) ──────────
# Décision du propriétaire du produit : `http` était un nom de protocole, qui ne dit
# pas ce que le connecteur sert. Le connecteur s'appelle `api`, ses outils `api*` —
# mêmes ops, mêmes arguments, même effet. Comme tout renommage servi, il se DOUBLE :
# les anciens noms d'outils et les anciens chemins REST continuent de répondre, en
# disant le nom d'aujourd'hui et la date.
#
# **Quand l'ancien nom part** : quand tous les consommateurs déclarés ont suivi
# (`.github/contrat-consommateurs.json`, règle du 25/09/2026) ET que l'usage compté
# des alias est nul — le journal `tool_calls` porte `args.deprecated_name` sur chaque
# appel servi par un ancien nom d'outil (`calllog.apply_call_trace`). La date
# ci-dessous est le PLUS TÔT possible, jamais une échéance automatique.
#
# Sa propre date — même raison que `RETRAIT_DATASTORE`. Le plancher contractuel se
# compte depuis l'annonce (deux mois, `PREAVIS_MOIS`) ; la fenêtre s'ouvre au tag de
# production, d'où la marge : un tag posé jusqu'au 30/11/2026 tient encore ses deux
# mois. Posé plus tard, `ANNONCE_CONNECTEUR_API` prend la date du tag.
ANNONCE_CONNECTEUR_API = datetime.date(2026, 11, 12)
RETRAIT_CONNECTEUR_API = max(_plus_de_mois(ANNONCE_CONNECTEUR_API, PREAVIS_MOIS),
                             datetime.date(2027, 1, 31))

#: Ancien nom d'outil → nom d'aujourd'hui, le même suffixe des deux côtés.
OUTILS_CONNECTEUR_API: dict = {f"http{suffixe}": f"api{suffixe}"
                               for suffixe in ("", "_act", "_get", "_post", "_doc")}

#: L'annonce du renommage est servie aux agents (`instructions`) jusqu'à ce jour
#: inclus — un mois. La description et chaque réponse de l'ancien nom le disent
#: ensuite jusqu'au retrait.
ANNONCE_CONNECTEUR_API_JUSQU_AU = _plus_de_mois(ANNONCE_CONNECTEUR_API, 1)


def annonce_connecteur_api_du_jour(aujourd_hui: Optional[datetime.date] = None
                                   ) -> Optional[str]:
    """La ligne servie aux agents tant que le renommage est récent, None ensuite."""
    jour = aujourd_hui or datetime.date.today()
    if jour > ANNONCE_CONNECTEUR_API_JUSQU_AU:
        return None
    return ("The Custom API connector is now `api`, and its tools renamed — "
            + " · ".join(f"`{a}` → `{n}`" for a, n in OUTILS_CONNECTEUR_API.items())
            + ". Same arguments, same effect. The old names still answer as deprecated "
            f"aliases until {RETRAIT_CONNECTEUR_API.isoformat()} at the earliest: "
            "call the new ones.")


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
    **OUTILS_CONNECTEUR_API,
}

#: La date de retrait PROPRE à un ancien nom d'outil, quand elle n'est pas `RETRAIT`.
_RETRAIT_DES_OUTILS: dict = {a: RETRAIT_CONNECTEUR_API for a in OUTILS_CONNECTEUR_API}


def date_de_retrait(ancien: Optional[str] = None) -> str:
    """La date de retrait telle qu'elle est SERVIE (JJ/MM/AAAA) — celle de l'ancien
    nom d'outil `ancien`, ou celle du lot #519."""
    return _RETRAIT_DES_OUTILS.get(ancien, RETRAIT).strftime("%d/%m/%Y")


def avis(canonique: str, ancien: Optional[str] = None) -> str:
    """L'avis qui PRÉFIXE la description d'un nom déprécié.

    En tête, pas en queue : beaucoup de clients tronquent une description longue,
    et un avis de dépréciation lu après 400 caractères n'a averti personne. C'est
    aussi la première chose que le modèle lit quand il choisit son outil.
    """
    return (f"Déprécié : utilisez `{canonique}` (retrait le {date_de_retrait(ancien)}). ")


def avis_d_appel(ancien: str) -> Optional[str]:
    """L'avis en tête de la RÉPONSE d'un appel fait sous un ancien nom — None si `ancien`
    n'est pas déprécié. Il nomme ce qui a été appelé, ce qui a servi et la date : un
    agent qui ne relit pas sa liste d'outils l'apprend au geste."""
    canonique = TOOLS.get(ancien)
    if canonique is None:
        return None
    return (f"Déprécié : `{ancien}` est renommé `{canonique}` (retrait le "
            f"{date_de_retrait(ancien)}). L'appel a été servi par `{canonique}` ; "
            f"appelle désormais `{canonique}`, mêmes arguments.")


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
    # ⚠️ La date de retrait PROPRE à cet alias, quand elle diffère de `RETRAIT`.
    #
    # Ce champ naît d'un défaut mesuré le 08/09/2026 : les alias du renommage
    # `namespace` → `datastore` servaient l'en-tête `Sunset` du renommage #519, annoncé
    # dix jours plus tôt. Un intégrateur qui lit ses en-têtes aurait daté son travail
    # sur une échéance qui n'était pas la sienne. **Une seule date pour plusieurs
    # renommages est une copie qui ne peut que devenir fausse dès le deuxième.**
    retrait: object = None


def _sunset(alias: "AliasRest") -> str:
    """La date de retrait SERVIE pour cet alias — la sienne, ou celle du lot #519."""
    d = getattr(alias, "retrait", None)
    return (d or RETRAIT).strftime("%d/%m/%Y")


# ── Renommage `namespace` → `datastore` (08/09/2026) ────────────────────────
#
# ⚠️ **Ces alias sont DÉRIVÉS des routes réellement montées, pas recopiés.** Le
# renommage touche une vingtaine de couples (verbe, chemin) — les lister à la main,
# c'est en oublier un, et un chemin oublié ne lève rien : il rend 404 chez un
# consommateur qui croyait la redirection posée. La dérivation ne peut pas oublier.
#
# ⚠️ Et le vrai motif de leur existence n'est pas la douceur : **le front de la refonte
# ne PEUT PAS porter les deux noms.** Une garde de son dépôt exige que tout chemin
# appelé existe verbatim dans le contrat servi — déclarer un chemin que le back ne sert
# pas échoue, dans les deux sens. Sans ces alias, sa bascule et celle du back devraient
# coïncider à la seconde ; avec eux, chacune devient indépendante, et elles survivent à
# un retour arrière du back.
#
# Comme tout ce module : ils portent une DATE. `ANNONCE_DATASTORE` ci-dessous.
ANNONCE_DATASTORE = datetime.date(2026, 9, 8)
RETRAIT_DATASTORE = _plus_de_mois(ANNONCE_DATASTORE, PREAVIS_MOIS)

#: L'ancien préfixe et le nouveau. Le placeholder change aussi de nom : seule la
#: VALEUR capturée voyage (cf. `cible`).
_DS_ANCIEN = "/api/datastore/namespaces"
_DS_NOUVEAU = "/api/datastores"
#: Les PARAMÈTRES renommés : ancien nom → (nouveau nom, date servie, ce qui ne change
#: pas). Une seule table pour les deux refus qui les nomment — la base des entrées de
#: capacité (`EntreeDatastore`) et l'enveloppe d'erreur des outils écrits à la main
#: (`error_taxonomy`, oto#135) — et pour le placeholder des alias de chemin.
#: ⚠️ La date est celle du REFUS servi (09/09), pas celle de l'annonce des chemins
#: (`ANNONCE_DATASTORE`, 08/09) : c'est la première que les agents lisent déjà.
PARAMETRES_RENOMMES = {
    "namespace": ("datastore", "2026-09-09",
                  "**the address does not change** (table name, number, "
                  "`slot:<name>` form), only the key switches"),
}
_DS_PARAMS = {ancien: v[0] for ancien, v in PARAMETRES_RENOMMES.items()}


def refus_parametre_renomme(ancien: str, valeur: Any = None,
                            outil: Optional[str] = None) -> Optional[str]:
    """Le refus d'un paramètre passé sous un nom RETIRÉ — `None` s'il n'a pas été renommé.

    Un seul texte, servi tel quel par la face capacité et par la face des outils écrits
    à la main. Jusqu'au 10/09/2026 la seconde disait « valeur(s) refusée(s) : namespace
    (Unexpected keyword argument) » — en anglais, sans le nom neuf : un agent y lisait
    un paramètre manquant et recomposait son appel à neuf (oto#135)."""
    if ancien not in PARAMETRES_RENOMMES:
        return None
    nouveau, date, inchange = PARAMETRES_RENOMMES[ancien]
    rejeu = (f"`{outil}` with `{nouveau}={valeur!r}`" if outil
             else f"the same call with `{nouveau}={valeur!r}`")
    return (f"`{ancien}` was renamed `{nouveau}` on {date} — the parameter no longer "
            f"exists under that name, and nothing was written. Replay {rejeu}: {inchange}. "
            f"⚠️ Do not hunt for another parameter: you provided the right value "
            f"under a retired name.")


def _alias_datastore() -> tuple:
    """Un alias par route datastore réellement montée — jamais une liste à la main.

    Lit les `RestBinding` des capacités du datastore et rend, pour chacune, l'ancien
    chemin équivalent. Un chemin ajouté demain hérite de son alias sans que personne
    n'y pense ; un chemin retiré perd le sien, au lieu de laisser une redirection qui
    mène nulle part.
    """
    from .capabilities.registry import CAPABILITIES  # tardif : évite un cycle

    out = []
    for cap in CAPABILITIES:
        rest = getattr(cap, "rest", None)
        chemin = getattr(rest, "path", None) if rest else None
        if not chemin or not chemin.startswith(_DS_NOUVEAU):
            continue
        ancien = _DS_ANCIEN + chemin[len(_DS_NOUVEAU):]
        ancien = ancien.replace("{datastore}", "{namespace}")
        out.append(AliasRest(getattr(rest, "verb", "GET"), ancien, chemin,
                             _DS_PARAMS, "", RETRAIT_DATASTORE))
    # Un chemin LITTÉRAL doit précéder un chemin à placeholder qui l'engloberait :
    # `…/namespaces` avant `…/namespaces/{namespace}`. Le tri par longueur puis par
    # nombre de placeholders donne cet ordre sans qu'on ait à le tenir à jour.
    return tuple(sorted(out, key=lambda a: (a.ancien.count("{"), len(a.ancien))))


#: Le préfixe REST du connecteur Custom API, avant et après son renommage.
_API_ANCIEN = "/api/orgs/{id}/connectors/http/"
_API_NOUVEAU = "/api/orgs/{id}/connectors/api/"


def _alias_connecteur_api() -> tuple:
    """Un alias par route du connecteur `api` réellement montée — même dérivation que
    `_alias_datastore` : une route ajoutée demain hérite de son ancien chemin, une route
    retirée perd le sien. Le placeholder ne change pas de nom."""
    from .capabilities.registry import CAPABILITIES  # tardif : évite un cycle

    out = []
    for cap in CAPABILITIES:
        for rest in cap.rest_bindings():
            if rest.path.startswith(_API_NOUVEAU):
                ancien = _API_ANCIEN + rest.path[len(_API_NOUVEAU):]
                out.append(AliasRest(rest.verb, ancien, rest.path, {}, "",
                                     RETRAIT_CONNECTEUR_API))
    return tuple(sorted(out, key=lambda a: (a.ancien.count("{"), len(a.ancien))))


def alias_rest_derives() -> tuple:
    """Tous les alias DÉRIVÉS des routes montées (à côté des déclarés, `REST`)."""
    return _alias_datastore() + _alias_connecteur_api()


REST: tuple = (
    # (`/api/doctrines/library[/{slug}]` → `/api/guide-library[/{slug}]` : partis
    # avec leur cible, la vitrine anonyme des guides — otomata-tech/oto#84.)
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

    La query string est REPORTÉE telle quelle : un client qui appelle
    `…/library?limit=200` et suit un 308 qui la perdrait recevrait 100 entrées au
    lieu de 200 — une régression qu'aucun code d'erreur ne signale.
    """
    chemin = alias.nouveau
    for nom, valeur in (path_params or {}).items():
        cible_nom = alias.params.get(nom, nom)
        chemin = chemin.replace("{" + cible_nom + "}", quote(str(valeur), safe=""))
    return f"{chemin}?{query}" if query else chemin


# ── Valeurs d'entrée renommées (otomata-tech/oto#65, 23/09/2026) ───────────
#
# Une VALEUR qu'un appelant passe dans un champ, renommée — pas une clé ni un chemin.
# Premier cas : la famille « procédure » de `oto_resource` (`resource_type`), que la
# surface servait sous le mot d'avant #519. Arbitrage d'Alexis du 23/09/2026 : ce
# mot-là ne doit plus apparaître.
#
# ⚠️ **Pourquoi un alias et pas un retrait sec** : un appelant vivant l'envoie —
# l'écran de partage d'une procédure du dashboard (`DoctrineView` →
# `SharePrincipalDialog`, `resource-type=` codé en dur), mesuré le 23/09/2026. Le
# nouveau front ne l'emploie pas, ni le plugin, ni oto-core, ni la CLI.
#
# **Forme de la coexistence** : l'entrée est RÉÉCRITE vers le nom d'aujourd'hui avant
# toute autre lecture (autz comprise), et la réponse porte `deprecation_warning`, qui
# nomme le nom d'aujourd'hui et la date. Rien en aval n'apprend que l'alias existe ;
# la réponse, elle, ne sert que le nom d'aujourd'hui (`resource_type: "procedure"`).
#
# Sa propre date — même raison que `RETRAIT_DATASTORE` : une seule date pour deux
# renommages est une copie qui ne peut que devenir fausse.
ANNONCE_PROCEDURE = datetime.date(2026, 9, 23)
RETRAIT_PROCEDURE = _plus_de_mois(ANNONCE_PROCEDURE, PREAVIS_MOIS)

#: champ → ({valeur d'hier → valeur d'aujourd'hui}, date de retrait).
VALEURS: dict = {
    "resource_type": ({"doctrine": "procedure"}, RETRAIT_PROCEDURE),
}


def valeur_canonique(champ: str, valeur: Any) -> tuple:
    """`(valeur d'aujourd'hui, avis)` — l'avis vaut `None` si la valeur n'est pas dépréciée.

    L'avis est le texte SERVI dans `deprecation_warning` : il nomme ce qu'on a reçu,
    ce qu'il faut envoyer, et la date — les trois choses qui permettent à l'appelant
    de se corriger sans lire cette page."""
    renommees, retrait = VALEURS.get(champ, ({}, None))
    nouvelle = renommees.get(valeur) if isinstance(valeur, str) else None
    if nouvelle is None:
        return valeur, None
    return nouvelle, (
        f"Déprécié : `{champ}={valeur}` s'écrit `{champ}={nouvelle}` "
        f"(retrait le {retrait.strftime('%d/%m/%Y')}). L'appel a été servi "
        f"comme `{nouvelle}` ; envoie désormais `{nouvelle}`.")


# ── Adresser un tableau par son NOM (lot 0 du retrait, 29/09/2026) ──────────
#
# Un tableau s'adresse par son IDENTIFIANT. Le nom résout encore — toutes les faces
# `data_*`, les routes `/api/datastores/{datastore}/…`, `oto_project op=link`,
# `oto_fleet op=create` — et chaque réponse obtenue par un nom le DIT, avec
# l'identifiant à passer et le jour où le nom sera refusé. Rien n'est accepté en
# silence : c'est un préavis servi, pas une tolérance.
#
# ⚠️ **L'annonce et la date ne sont pas le même jour, et c'est l'écart assumé.**
# Le retrait du nom est servi en production depuis le 07/09/2026 (`v1.233.0` : « a
# name still resolves… it is being retired »), mais SANS date. La date n'est servie
# qu'à partir de ce lot. Alexis l'a alignée le 29/09/2026 sur le retrait de
# `namespace` (`RETRAIT_DATASTORE`, un seul geste pour les deux), en sachant qu'elle
# tombe à moins de deux mois du jour où elle devient lisible — écart à l'Art 8.2
# décidé par le propriétaire du produit, pas une durée de préavis revue à la baisse :
# `PREAVIS_MOIS` ne bouge pas, et le plancher compté depuis l'annonce tient
# (`tests/test_tableau_par_numero.py`).
ANNONCE_NOM_DE_TABLEAU = datetime.date(2026, 9, 7)
RETRAIT_NOM_DE_TABLEAU = RETRAIT_DATASTORE


def date_retrait_nom_de_tableau() -> str:
    """Le jour où un nom de tableau sera refusé, tel qu'il est SERVI à l'agent : ISO
    (AAAA-MM-JJ). Servi en JJ/MM/AAAA dans des phrases anglaises, il se lisait le 11 août."""
    return RETRAIT_NOM_DE_TABLEAU.isoformat()


def adresse_par_nom(adresse: Any, ns_id: Any) -> bool:
    """L'adresse reçue a-t-elle résolu le tableau `ns_id` PAR SON NOM ?

    Juge le résultat, pas la forme : des chiffres qui NOMMENT un tableau (« 2024 »)
    sont un nom, et `slot:` n'arrive jamais jusqu'ici — il rend déjà l'identifiant."""
    return str(adresse or "").strip() != str(ns_id)


def avis_nom_de_tableau(nom: str, ns_id: int) -> str:
    """L'avis servi à chaque réponse obtenue par un nom : l'identifiant et la date."""
    return (f"⚠️ Table `{nom}` was addressed by its NAME: pass "
            f"`datastore={ns_id}`. A table name will be REFUSED from "
            f"{RETRAIT_NOM_DE_TABLEAU.isoformat()} on; the identifier never changes, "
            f"even if the table is renamed.")


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
