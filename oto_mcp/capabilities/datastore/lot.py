"""Un lot enveloppé n'est pas une ligne (oto#48).

`POST …/rows` est unitaire par contrat : le corps EST la ligne, un objet dont les
clés sont les colonnes. Une mission a chargé par lots de cent en `{"data": [...]}` :
quinze 201, quinze lignes, chacune portant cent lignes sous une colonne `data` —
sans un mot, parce qu'une colonne libre à valeur liste est légale (oto#22). Mesuré
sur base réelle le 04/09/2026 : sans schéma → 201 muet ; `strict: true` → 201 avec
un relevé `hors_schema` ; seul `unknown_fields: "reject"` refusait, et sous un autre
nom (« colonne inconnue »).

La garde juge la FORME du corps, jamais ses colonnes — le schéma s'en charge : un
objet à clé unique dont la valeur est une liste NON VIDE d'objets. Une liste de
scalaires (`tags: ["a", "b"]`) n'est pas un lot ; un objet à deux clés non plus
(une ligne a presque toujours sa clé métier à côté). Et une colonne DÉCLARÉE sous
ce nom l'emporte : un sous-tableau (`contacts`, type `list`) est une ligne légitime,
la déclaration dit l'intention mieux que la forme ne la devine.

⚠️ Sur la route unitaire seule, pas sur le PATCH : réécrire la sous-table d'une ligne
est un geste courant, et l'URL d'un patch nomme déjà la ligne visée — un lot égaré là
est improbable, un refus y casserait un usage juste.
"""
from __future__ import annotations

from typing import Optional

from ...datastore.schema import declares_field
from .._types import AuthzDenied

CODE = "batch_body"


def forme_de_lot(row) -> Optional[tuple[str, int]]:
    """`(clé, nombre d'objets)` si `row` a la forme d'un lot enveloppé, sinon None."""
    if not isinstance(row, dict) or len(row) != 1:
        return None
    (cle, valeur), = row.items()
    if not isinstance(valeur, list) or not valeur:
        return None
    if not all(isinstance(v, dict) for v in valeur):
        return None
    return str(cle), len(valeur)


def refuser_un_lot(store, namespace: str, row) -> None:
    """Lève `AuthzDenied(400, batch_body)` si `row` est un lot enveloppé dont la clé
    n'est pas une colonne déclarée du tableau. Le schéma n'est lu que lorsque la forme
    est suspecte : une ligne ordinaire ne paie rien. `NamespaceNotFound` remonte à
    l'appelant, qui le rend comme pour l'écriture elle-même."""
    lot = forme_de_lot(row)
    if lot is None:
        return
    cle, n = lot
    if declares_field(store.get_schema(namespace), cle):
        return
    raise AuthzDenied(400, CODE, (
        f"`POST …/rows` écrit UNE ligne : le corps est un objet, une clé par colonne. "
        f"Reçu un objet dont l'unique clé `{cle}` porte une liste de {n} objets — la "
        f"forme d'un LOT, qui aurait été écrit tel quel comme une seule ligne à colonne "
        f"`{cle}`. Rien n'a été écrit. Un lot passe par `data_write(namespace=…, "
        f"rows=[…])` côté agent ; pour un volume, `oto_upload_url` (NDJSON/CSV) puis "
        f"`PUT /api/upload/{{token}}`. Si c'est bien UNE ligne dont la colonne `{cle}` "
        f"porte ces objets, déclare-la dans le schéma (`data_patch_schema`, type "
        f"`list`) : une colonne déclarée n'est jamais prise pour un lot."),
        {"key": cle, "count": n})
