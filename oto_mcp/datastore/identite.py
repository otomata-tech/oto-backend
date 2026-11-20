"""L'IDENTITÉ d'un tableau dans une réponse : son NUMÉRO et son NOM canonique.

Deux défauts fermés ensemble, parce qu'ils vivent dans la même clé.

**1. Rien ne donnait le numéro à l'agent.** Le tableau s'adresse indifféremment par
son nom ou par son numéro — `db.resolve_datastore_ns` accepte les deux, avec le
MÊME prédicat de visibilité — mais aucune remise ne rendait le numéro : un agent qui
ne l'avait pas lu dans `data_list_datastores` ne pouvait pas l'employer. Le nom part
en retrait ; l'usage ne bascule que si le numéro arrive DANS la réponse, au moment où
l'agent en a besoin. D'où `ns_id`, servi à côté de `datastore`.

**2. `datastore` était un ÉCHO, pas une identité.** La réponse répétait la chaîne
reçue : adresser par nom rendait le nom, adresser par numéro rendait `"600"`. Elle
répondait donc à la question posée au lieu de dire quel tableau a été touché — et deux
appelants sur le même tableau lisaient deux valeurs différentes sans que rien ne le
dise. Ici, `datastore` est le nom canonique lu dans `user_datastores`, quelle que soit
la forme de l'adresse.

⚠️ **Changement de comportement assumé, et le seul** : un appelant qui adressait par
numéro (ou par `slot:<nom>`) et relisait `datastore` pour se vérifier reçoit désormais
le nom du tableau, pas sa propre chaîne. C'est ce que la clé prétendait dire.

Le nom de clé `ns_id` n'est pas neuf : c'est déjà celui de la résolution
(`DatastorePg._resolve`), du relevé de journal (`_trace`, `NsContext`), de la cible
d'un upload signé, du relevé d'appel MCP (`_TRACED_ARGS`) — et il est déjà SERVI aux
agents par `oto_search`, dont un hit `kind: "ligne"` porte `ref: {ns_id, row_id}`.
`id`, lui, était pris : dans une remise de ligne (`data_release`, `data_delete_row`)
il désigne la LIGNE, et réutiliser ce mot pour le tableau créerait un homonyme
exactement là où l'agent adresse.
"""
from __future__ import annotations

from typing import Annotated, Any, Mapping, Optional

from pydantic import BeforeValidator, WithJsonSchema


# Le nom de la clé, écrit une fois : les deux faces et les tests le citent d'ici.
CLE = "ns_id"

# La phrase servie AVEC la clé, au contrat comme aux descriptions d'outils : une clé
# neuve dont personne ne dit à quoi elle sert reste une clé que personne n'emploie.
DESCRIPTION = (
    "The table's NUMBER — the form to pass as `datastore` from here on (a table name is "
    "refused). `null` only when no table was resolved.")


def identite(ns_id: Any = None, nom: Optional[str] = None, *,
             adresse: str = "") -> dict:
    """`{"datastore": <nom canonique>, "ns_id": <numéro>}` à fusionner dans une réponse.

    `adresse` = la chaîne que l'appelant a passée, gardée en DERNIER recours : un
    chemin qui a échoué avant de relever quoi que ce soit rend alors ce qu'il rendait
    avant, plutôt qu'un `null` à la place d'un nom. `ns_id` vaut `None` dans ce seul
    cas — sa présence est donc la preuve que le tableau a bien été résolu.
    """
    valeur = nom or adresse
    # ⚠️ **Le doublon `namespace` est RETIRÉ (10/09/2026, décision d'Alexis).** Il avait
    # été posé pour le préavis, avec une raison qui reste vraie et qu'il faut connaître :
    # c'était la seule panne MUETTE de la bascule. Un chemin qui change rend un 404 ou un
    # 308 — on le voit. Une clé de réponse qui disparaît ne rend rien : `r.namespace`
    # vaut `undefined`, sans erreur et sans journal, chez un consommateur qu'on ne
    # connaît pas forcément.
    #
    # Ce qui a fait pencher : le renommage était INCOHÉRENT, et l'incohérence coûtait
    # plus que la rupture. Trois politiques coexistaient — la liste `/api/datastores`
    # basculée à sec, les réponses unitaires doublées, l'upload de lignes jamais
    # basculé. Nos deux fronts (le nôtre et celui du partenaire) portaient chacun un
    # pont pour absorber ça, et *un pont qu'on ne retire pas masque le renommage
    # suivant*. Un seul nom, partout, coûte une rupture annoncée plutôt qu'une dette
    # permanente répartie sur trois dépôts.
    #
    # Les consommateurs connus sont prévenus ; les ponts des fronts lisent `datastore`
    # en premier, donc ils ne cassent pas — ils deviennent inutiles et tombent.
    return {"datastore": valeur,
            CLE: int(ns_id) if ns_id is not None else None}


def de_releve(releve: Optional[Mapping], adresse: str = "") -> dict:
    """La même, depuis un relevé `{ns_id, datastore}` — `DatastorePg.dernier_tableau`
    ou le `trace` d'une mutation, qui portent tous deux ces deux clés."""
    releve = releve or {}
    return identite(releve.get("ns_id"), releve.get("datastore"), adresse=adresse)


def numero(releve: Optional[Mapping]) -> dict:
    """Le NUMÉRO seul : `{"ns_id": …}`, sans le nom.

    Pour les remises dont le corps EST la ligne (`data_write`, `POST …/rows`) : elles
    ne servaient aucun `datastore`, il n'y a donc pas d'écho à corriger — et y poser
    une clé `datastore` à côté des colonnes de l'utilisateur mettrait un mot très
    plausible en collision avec une vraie colonne. `ns_id` s'ajoute, `datastore` non.
    """
    return {CLE: identite((releve or {}).get("ns_id"))[CLE]}


# ── L'ADRESSE d'un tableau à l'ENTRÉE — texte OU nombre (07/09/2026) ─────────
#
# Le pendant obligatoire de `CLE` : on ne peut pas RENDRE un numéro, dire à l'agent
# « c'est la forme à employer », et refuser ce numéro quand il le repasse.
#
# ⚠️ **C'est exactement ce qui s'est produit, et ça a coûté cher.** Mesuré par une
# campagne le 07/09/2026 sur dix lignes, juste après la mise en production : le modèle
# lit `ns_id: 609` dans la réponse — un NOMBRE en JSON —, le repasse tel quel à
# `data_write`, et se fait refuser « Input should be a valid string ». **7 écritures
# refusées sur 13 (54 %)**, 3 lignes travaillées puis jamais écrites, et le coût par
# fiche qui passe de 7,1 à 11,0 tours (+55 %). `data_rows` et `data_get_schema`
# l'acceptaient ; `data_write` non. Deux outils du même module, deux contrats — et
# l'écart ne se voit qu'à l'exécution.
#
# La leçon générale, au-delà du correctif : **une valeur qu'on sert doit être reprise
# telle quelle par la porte d'entrée qui la réclame.** Un aller-retour dont les deux
# moitiés ne s'accordent pas sur le TYPE est une promesse creuse, comme une clé
# annoncée et jamais lue — sauf que celle-ci refuse au lieu de se taire.
#
# La résolution, elle, savait déjà faire : `db.resolve_datastore_ns` accepte le nom ET
# le numéro (« id OU nom », même prédicat de visibilité, aucun IDOR). Il ne manquait
# que la coercition à l'entrée. On normalise vers le texte pour ne rien changer en
# aval — un seul chemin de résolution, pas deux.


def _texte_de_l_adresse(v: Any) -> Any:
    """Un entier est l'adresse par NUMÉRO : on la rend au format que tout le reste
    manipule. Le reste passe intact, y compris ce qui est fautif — refuser proprement
    est le travail du validateur, pas celui d'une coercition silencieuse.

    ⚠️ `bool` est un `int` en Python : `data_write(datastore=True)` deviendrait le
    tableau « True ». On le laisse donc au validateur, qui le refusera."""
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return str(v)
    return v


# ⚠️ **Le SCHÉMA publié doit dire ce que la surface accepte, pas ce qu'elle stocke.**
#
# La coercition ci-dessus a fermé l'écart entre la description et le SERVEUR ; il en
# restait un, remonté d'une couche, entre la description et le SCHÉMA. La description
# ordonne « adresse le tableau par son NUMÉRO — la forme à employer », et le schéma
# annonçait `{"type": "string"}`. Un agent consciencieux qui lit les deux ne peut pas
# les concilier.
#
# Ce n'est pas théorique : mesuré le 08/09/2026, un client de campagne **validait contre
# ce schéma et interceptait le nombre avant l'envoi**. Il ne pouvait donc ni reproduire
# le défaut, ni bénéficier du correctif — pendant que les agents du même runner, qui ne
# valident pas, l'envoyaient et se faisaient refuser. Le schéma décidait qui pouvait
# suivre l'instruction et qui ne le pouvait pas.
#
# ⚠️ **DEUX types, parce qu'il y a DEUX fils, et ils ne portent pas les mêmes formes.**
#
# Le fil MCP porte du JSON : un nombre y est un nombre, et annoncer l'alternative est
# exact. **Un chemin d'URL, lui, ne porte que du texte** — `/datastores/609/schema` est
# une chaîne de caractères, il n'existe aucun entier à y mettre. Y publier une
# alternative n'est pas seulement inutile : c'est FAUX.
#
# ⚠️ Et ce n'était pas une subtilité théorique — c'est ce qui a cassé la préproduction
# le 08/09/2026. Le descriptif REST dérive le type d'un paramètre de chemin du champ
# correspondant : une alternative n'a pas de clé `type`, donc six routes du datastore
# ont vu leur paramètre `datastore` passer de « chaîne » à « rien » dans le contrat
# servi. La garde du front l'a arrêté avant toute mise en production, et un pair a
# annulé son propre déploiement plutôt que de passer outre sur mon commit.
#
# La leçon, et elle vaut au-delà de ce cas : **rendre un schéma plus honnête sur un fil
# peut le rendre faux sur un autre.** Un type partagé par deux surfaces doit être jugé
# sur les deux.

#: L'adresse d'un tableau **à l'entrée d'une surface REST** : coercition du nombre vers
#: le texte, mais publiée comme une CHAÎNE — c'est tout ce qu'un chemin d'URL peut
#: porter. Un corps JSON qui enverrait un nombre est quand même accepté ; il est
#: simplement sous-annoncé, ce qui est le bon côté de l'erreur.
Adresse = Annotated[str, BeforeValidator(_texte_de_l_adresse)]

#: La même, **pour le fil MCP**, où le JSON distingue vraiment un nombre d'une chaîne.
#: Le nombre est annoncé EN PREMIER : c'est la forme que la description prescrit, et
#: l'ordre d'une alternative se lit comme une préférence.
AdresseJson = Annotated[
    str,
    BeforeValidator(_texte_de_l_adresse),
    WithJsonSchema({"anyOf": [{"type": "integer"}, {"type": "string"}]}),
]
