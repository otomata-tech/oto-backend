"""L'avertissement : ces clés de schéma, oto ne les interprète pas (oto#56, signal 658).

⚠️ **« Oto ne les interprète pas », jamais « personne ne les lit ».** La formule
d'origine — « lues par PERSONNE » — a coûté cher : six attributs ont été portés comme
morts sur la foi de cet avertissement, un seul l'était. Les cinq autres sont lus, sur
des tableaux de production, par le consommateur qui les affiche. Le retrait a été
arrêté à temps. La plateforme ne sait pas qui lit en aval : elle sait seulement ce
qu'ELLE interprète, et c'est tout ce que le message a le droit de dire. Un texte qui
porte un nom plus large que ce qu'il mesure fait prendre des décisions plus larges que
ce qu'il autorise — et celui-ci est lu au moment de retirer.

Le validateur acceptait n'importe quel attribut sur une colonne. `readonly` passe,
`editable` passe, `zorglub` passe — aucune n'est refusée, aucune n'est signalée.

Le cas fondateur est un agent qui pose `readonly: true` **et** `editable: true` en
espérant que le second rouvre le premier pour un humain, et qui le découvre en comparant
deux messages de refus strictement identiques. ⚠️ **Le cas grave est l'autre** : qui
écrit `read_only` au lieu de `readonly` croit avoir verrouillé sa colonne et n'a rien
verrouillé — la faute de frappe est silencieuse **et** elle désarme le cran.

## Avertissement, jamais refus

Refuser durcirait un contrat déjà servi (ADR 0019/0050 : *un contrat servi ne se durcit
pas en place, il se double*) et casserait à la première écriture tous les schémas
existants qui portent déjà des clés mortes. Même régime que `digest_warning`,
`diagram_warning` et `retrait_warning` : la pose réussit, l'auteur reçoit le signal.

## La référence est la DÉCLARATION, pas ce que le validateur lit

Première forme de ce lot, écartée par la mesure : dériver la liste en observant le
validateur. Le schéma n'est pas seulement validé, il est **servi** — cinq attributs
vivants n'existent que pour le front (`label`, lu 40 fois côté dashboard, `help`,
`placeholder`, `hint`, `description`). L'avertissement aurait crié « `label` n'est lue
par personne » sur presque tous les tableaux. **Un faux positif dans un signal de
qualité est pire que pas de signal** : on apprend à l'ignorer, et il ne sert plus le jour
où il a raison.

## Une seule référence, parce qu'il y en avait deux

⚠️ Cet avertissement consultait `schema_keys.RECONNUES` — la déclaration écrite à la
main — pendant qu'un SECOND avertissement, dérivé du code, était servi dans la même
réponse. Chacun était aveugle exactement là où l'autre voyait : celui-ci dénonçait
`options`, `required` et `max_items`, que le validateur applique ; l'autre dénonçait
`label`, `help`, `hint`, `placeholder` et `description`, que le front lit. Ils ne
s'accordaient que sur `read_only`, la seule vraie faute — et l'auteur d'un schéma
recevait deux verdicts contradictoires sur le sien.

⚠️ **Et celui-ci se trompait de sens sur le cas fondateur.** Un champ portant `enum`
à côté d'`options` — la faute qui a laissé 504 valeurs libres sur un tableau qui se
croyait contraint — passait ici en SILENCE (`enum` était déclarée), tandis
qu'`options`, la clé qui fait foi, était accusée. Le message envoyait retirer la
bonne et garder la mauvaise.

La référence est désormais `schema.vocabulaire_vivant()` : le vocabulaire DÉRIVÉ du
code (ce que le validateur lit) uni à la moitié `front` de `schema_keys`, la seule
qu'aucune dérivation ne peut voir d'ici. Les deux avertissements la partagent.
"""
from __future__ import annotations

from typing import Any, Iterable

from .schema import vocabulaire_vivant


def inconnues(fields: Iterable) -> dict[str, list[str]]:
    """`{colonne: [clés que personne ne lit]}`. Vide = rien à signaler.

    Les couches (`colonne.comment`) portent les mêmes attributs que leur colonne : on
    les traite pareil, et c'est le validateur qui refuse celles qui n'ont de sens que
    sur une colonne."""
    out: dict[str, list[str]] = {}
    vivantes = vocabulaire_vivant()
    for f in fields or []:
        if not isinstance(f, dict):
            continue
        mortes = sorted(k for k in f
                        if isinstance(k, str) and k not in vivantes)
        if mortes:
            out[str(f.get("key") or "?")] = mortes
    return out


def check(schema: Any) -> dict:
    """Check croisé à la pose, dans la forme des autres (`digest_check`,
    `retrait_check`) : la clé est TOUJOURS présente, `None` = rien à signaler.
    Best-effort — un check ne casse jamais une écriture."""
    try:
        if not isinstance(schema, dict):
            return {"unknown_keys_warning": None}
        trouvees = inconnues(schema.get("fields") or [])
        if not trouvees:
            return {"unknown_keys_warning": None}
        detail = " ; ".join(
            f"`{col}` : {', '.join('`' + k + '`' for k in cles)}"
            for col, cles in sorted(trouvees.items()))
        return {"unknown_keys_warning": (
            f"ces clés ne sont interprétées par AUCUN contrôle de la plateforme — "
            f"{detail}. Le schéma est bien posé, mais ce qu'elles devaient garder n'est "
            "pas gardé : une faute de frappe sur un attribut (`read_only` pour "
            "`readonly`) désarme le cran sans rien dire. ⚠️ Cela ne dit PAS qu'elles ne "
            "servent à personne : un consommateur peut les lire en aval — un front "
            "affiche des libellés, une éditabilité, un motif — et oto ne sait pas qui "
            "lit quoi. Ne retire rien sur la seule foi de ce message. La liste des "
            "attributs qu'oto interprète, avec le lecteur de chacun, est servie sur "
            "`GET /api/datastore/schema/keys`.")}
    # noqa: SILENT — contrôle de forme optionnel : pas d'avertissement plutôt qu'un faux
    except Exception:  # noqa: BLE001 — cf. `digest_check`
        return {"unknown_keys_warning": None}
