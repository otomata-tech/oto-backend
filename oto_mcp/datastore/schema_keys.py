"""Les attributs qu'une colonne de schéma peut porter — **la déclaration**, une seule.

Le validateur acceptait n'importe quelle clé. `readonly` passe, `editable` passe,
`zorglub` passe : aucune n'est refusée, aucune n'est signalée. Le cas fondateur
(oto#56, signal 658) est un agent qui pose `readonly: true` **et** `editable: true` en
espérant que le second rouvre le premier pour un humain — `editable` n'existe nulle
part, il n'a donc pas été « accepté puis ignoré » par une implémentation partielle, il a
été accepté **parce que rien ne regardait**.

⚠️ **Le cas grave est l'autre** : qui écrit `read_only` au lieu de `readonly` croit
avoir verrouillé sa colonne et n'a rien verrouillé. La faute de frappe est silencieuse
**et** elle désarme le cran. Elle ne se découvre qu'à la première écriture qui passe là
où on croyait un verrou.

## Pourquoi une déclaration, et pas « ce que le validateur lit »

Première idée, écartée **par la mesure** : dériver la liste en observant le validateur.
Elle est fausse, et de peu — le schéma n'est pas seulement validé, il est **servi**.
C'est un contrat que le dashboard et les fronts tiers lisent. Cinq attributs vivants y
échappaient (`label`, lu 40 fois côté dashboard, `help`, `placeholder`, `hint`,
`description`) : un avertissement bâti là-dessus aurait crié « `label` n'est lue par
personne » sur presque tous les tableaux existants. Un faux positif dans un signal de
qualité est pire que pas de signal — on apprend à l'ignorer, et il ne sert plus le jour
où il a raison.

D'où la forme retenue : **une déclaration, deux clients.** Le validateur en est le
premier (il en dérive ses crans de niveau colonne), l'avertissement le second. Rien
n'est recopié, et ce qui manquait cruellement est écrit ici : **qui lit quoi.**

⚠️ **Les clés `front` sont déclarées à la main, et c'est une dette assumée.** Rien ne
vérifie aujourd'hui que le dashboard lit bien celles-là et rien d'autre. Le palier
suivant — pas ce lot — est un contrôle CÔTÉ DASHBOARD qui confronte les clés qu'il lit à
cette déclaration ; c'est pour ça qu'elle est **servie** (`GET /api/datastore/schema/
keys`) plutôt que gardée en Python.

⚠️ Ce qui garde la moitié `validateur`, en revanche, est mécanique :
`tests/test_schema_keys_oto56.py` observe le validateur et exige que **tout ce qu'il lit
soit déclaré ici**. Un `f.get("nouveau")` ajouté sans déclaration rougit avant que
l'avertissement ne se mette à mentir.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Cle:
    """Un attribut de colonne, et surtout : **qui le lit**.

    `lecteurs` ⊆ {"validateur", "front"}. Une clé lue par le seul front est
    parfaitement légitime — c'est le fait que personne ne l'écrivait qui a coûté."""
    nom: str
    lecteurs: tuple[str, ...]
    quoi: str
    #: `True` = n'a de sens que sur une COLONNE, jamais sur une couche
    #: (`colonne.comment`). C'est ce que le validateur consomme ici.
    colonne_seulement: bool = False


#: LA déclaration. Ajouter un attribut au schéma passe par cette liste — c'est ce qui
#: rend l'avertissement vrai, et c'est aussi ce qui le rend maintenable.
CLES: tuple[Cle, ...] = (
    # — structure, lues des deux côtés —
    Cle("key", ("validateur", "front"), "le nom de la colonne (ou `colonne.couche`)"),
    Cle("type", ("validateur", "front"), "le type de la valeur"),
    Cle("of", ("validateur", "front"), "le type des éléments d'une liste", True),
    Cle("fields", ("validateur", "front"), "les sous-champs d'un objet", True),
    # — crans de garde, lus par le validateur —
    Cle("readonly", ("validateur", "front"),
        "colonne du fichier source : une écriture ne la change pas", True),
    Cle("system", ("validateur",), "estampille reposée par la plateforme à chaque écriture", True),
    Cle("origine", ("validateur",), "la couche d'origine, posée par la plateforme", True),
    Cle("max_length", ("validateur", "front"), "borne de longueur, publiée dans le contrat"),
    Cle("pattern", ("validateur",), "forme exigée de la valeur"),
    Cle("required_when", ("validateur", "front"), "obligatoire sous condition"),
    Cle("lifecycle", ("validateur", "front"), "états et transitions permises"),
    Cle("enum", ("validateur", "front"), "valeurs permises (avec `type: \"enum\"`)"),
    # — présentation, lues par le FRONT SEUL : invisibles au validateur, et c'est
    #   exactement ce qui a fait échouer la première forme de ce lot —
    Cle("label", ("front",), "le nom affiché de la colonne (le plus lu de tous)"),
    Cle("description", ("front",), "le texte long de la colonne"),
    Cle("help", ("front",), "l'aide affichée à la saisie"),
    Cle("hint", ("front",), "l'indice court à côté du champ"),
    Cle("placeholder", ("front",), "le texte fantôme d'un champ vide"),
    Cle("display", ("validateur", "front"), "comment la colonne se rend", True),
    Cle("role", ("validateur", "front"), "le rôle métier (statut, clé…)", True),
    Cle("flat_alias", ("validateur", "front"), "le nom à plat d'un sous-champ", True),
)

#: Tout ce qu'une colonne a le droit de porter. C'est CE nom que l'avertissement
#: consulte — jamais une liste recopiée à côté.
RECONNUES: frozenset[str] = frozenset(c.nom for c in CLES)

#: Les clés qui n'ont de sens que sur une colonne, jamais sur une couche. Le validateur
#: s'en sert pour refuser `colonne.comment: {readonly: true}` — c'est ce qui fait de
#: cette déclaration le premier client de sa propre liste, et pas une documentation.
COLONNE_SEULEMENT: tuple[str, ...] = tuple(c.nom for c in CLES if c.colonne_seulement)

#: Ce que le validateur consulte réellement. Le banc de garde exige que ce soit un
#: sous-ensemble de `RECONNUES` : une clé lue et non déclarée ferait mentir
#: l'avertissement, et personne ne s'en apercevrait avant qu'un utilisateur ne le
#: signale.
LUES_PAR_LE_VALIDATEUR: frozenset[str] = frozenset(
    c.nom for c in CLES if "validateur" in c.lecteurs)


def servie() -> list[dict]:
    """La déclaration, telle qu'elle part sur la face REST.

    Servie plutôt que gardée en Python pour que le dashboard puisse un jour confronter
    ce qu'il lit à ce qui est déclaré — c'est le seul chemin qui rendra la moitié
    `front` aussi sûre que la moitié `validateur`."""
    return [{"key": c.nom, "readers": list(c.lecteurs), "what": c.quoi,
             "column_only": c.colonne_seulement} for c in CLES]
