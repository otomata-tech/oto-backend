"""La forme IMBRIQUÉE d'une ligne servie — l'option `layers=nested` (oto#53).

Une colonne s'ÉCRIT imbriquée, `{"champ": {"valeur": …, "comment": …, "origine": …,
"link": …}}`, et se RELIT à plat : `champ` = la valeur seule, `champ.comment` à côté,
au premier niveau de la ligne (`schema.flat_layers`, appelée par `core._row_to_dict`).
Un client qui relit `row["champ"]` en attendant la forme qu'il a écrite conclut que sa
couche a disparu — elle n'a pas disparu, la forme a changé entre l'aller et le retour
(oto#47). Ici, la forme symétrique : une cellule à couches revient comme elle s'écrit.

Palier 1 d'une bascule en trois temps (oto#53) : l'option d'abord, le défaut reste
`flat` ; la mesure des consommateurs de la forme plate ensuite ; la bascule du défaut
enfin, avec préavis daté et double-service. **Le défaut se lit ici et nulle part
ailleurs** — les deux faces le prennent d'ici, pour qu'une bascule soit un seul geste.

Ce module ne fait QUE la mise en forme. Il ne connaît ni la base, ni le schéma : il
reçoit ce que `_row_to_dict` reçoit — la valeur stockée d'une colonne — et rend ce
qu'un lecteur `nested` doit voir. La règle du premier niveau s'applique un cran plus
bas, dans les items d'une colonne-liste, exactement comme `schema._served_item` le
fait pour la forme plate : qui sait lire `row["email"]["origine"]` sait lire
`item["email"]["origine"]`.
"""
from __future__ import annotations

from typing import Any

from . import schema as dsv2

FLAT = "flat"
NESTED = "nested"
FORMES = (FLAT, NESTED)
DEFAUT = FLAT  # ⚠️ palier 3 (oto#53) : bascule vers NESTED, avec préavis daté.


def check(value: Any) -> str:
    """La valeur de `layers`, ou un refus qui NOMME le paramètre, la valeur reçue et
    les deux formes admises — jamais un `invalid_input` nu qui oblige à deviner.

    `None` vaut le défaut : les deux faces passent leur paramètre tel quel, et une
    face qui n'a rien reçu ne doit pas avoir à connaître le défaut de l'autre."""
    if value is None:
        return DEFAUT
    if value in FORMES:
        return str(value)
    raise ValueError(
        f"`layers` inconnu : `{value}` — attendu `{FLAT}` (défaut : `champ` = la valeur, "
        f"`champ.origine`/`.comment`/`.link` à plat à côté) ou `{NESTED}` "
        f"(`champ` = {{\"valeur\", \"origine\", \"comment\", \"link\"}}, la forme écrite).")


def nested_value(value: Any) -> Any:
    """Ce qu'un lecteur `nested` reçoit pour une colonne.

    - cellule à couches → `{"valeur": …, + chaque couche RENSEIGNÉE}` : `valeur` est
      toujours là (`None` quand la colonne porte une provenance sans valeur posée —
      l'import de socle), les couches seulement quand elles le sont, comme à plat
      (`flat_layers` tait `None`/`""`) ;
    - cellule sans couche → sa valeur, telle que la forme plate la sert (`unwrap`,
      puis la descente dans les items d'une liste).

    Les DEUX formes partent de la même valeur déballée : ce que `flat` sert sous le nom
    nu et ce que `nested` sert sous `valeur` est le même objet, par construction."""
    if isinstance(value, dict) and any(k in dsv2.LAYER_KEYS for k in value):
        out: dict = {dsv2.VALUE_LAYER: _plain(dsv2.unwrap(value))}
        for layer in dsv2.LAYER_KEYS:
            if value.get(layer) not in (None, ""):
                out[layer] = value[layer]
        return out
    return _plain(dsv2.unwrap(value))


def _plain(v: Any) -> Any:
    """La valeur déballée, descendue dans une liste de fiches : un item non-dict
    traverse tel quel (une liste de scalaires reste une liste de scalaires)."""
    if isinstance(v, list):
        return [({k: nested_value(x) for k, x in item.items()}
                 if isinstance(item, dict) else item) for item in v]
    return v
