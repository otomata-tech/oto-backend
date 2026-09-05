"""Servir la déclaration des attributs de colonne — pour qu'un front puisse s'y
confronter (oto#56).

`datastore/schema_keys.py` déclare ce qu'une colonne a le droit de porter, et **qui lit
chaque attribut** : le validateur, le front, ou les deux. Le validateur en dérive ses
crans, l'avertissement `unknown_keys_warning` s'en sert de référence.

⚠️ **La moitié `front` est déclarée à la main, et rien ne la vérifie encore.** C'est la
dette assumée du lot : personne ne garantit que le dashboard lit bien ces attributs-là
et pas d'autres. Le palier suivant est un contrôle **côté dashboard** qui confronte les
clés qu'il lit à ce qui est déclaré ici — et il ne peut exister que si la déclaration
est SERVIE. C'est tout l'objet de cette route : elle n'a pas de lecteur aujourd'hui,
elle en aura un, et sans elle ce lecteur serait impossible à écrire.

Lecture seule, sans paramètre, identique pour tout le monde : la déclaration est un fait
de plateforme, pas une donnée d'org. `SUB_ONLY` — il faut être authentifié, rien de plus.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from ...datastore import schema_keys as decl
from .._authz import SUB_ONLY
from .._types import Capability, ResolvedCtx, RestBinding
from ..registry import CAPABILITIES


class _NoInput(BaseModel):
    pass


class SchemaKey(BaseModel):
    key: str
    #: Qui lit cet attribut : `validateur` (le backend le fait respecter), `front`
    #: (il n'existe que pour l'affichage), ou les deux. C'est l'information qui
    #: manquait : cinq attributs vivants n'étaient lus QUE par le dashboard, et une
    #: première version de l'avertissement les aurait déclarés morts.
    readers: list[str]
    what: str
    #: `true` = n'a de sens que sur une colonne, jamais sur une couche
    #: (`colonne.comment`).
    column_only: bool = False


class SchemaKeys(BaseModel):
    keys: list[SchemaKey] = Field(
        description="Tout ce qu'une colonne de schéma a le droit de porter.")


def _schema_keys(ctx: ResolvedCtx, inp: _NoInput) -> dict:
    return {"keys": decl.servie()}


CAPABILITIES += [
    Capability(
        key="datastore.schema_keys", handler=_schema_keys,
        Input=_NoInput, Output=SchemaKeys,
        authz=SUB_ONLY,
        mcp=None,  # contrat de FRONT : un agent lit la description de l'outil, pas ça
        rest=RestBinding("GET", "/api/datastore/schema/keys"),
        description=(
            "Every attribute a schema column may carry, and WHO reads each one — the "
            "validator, the front-end, or both. Served so a client can check what it "
            "reads against what is declared: an attribute nobody reads is accepted "
            "silently, so a typo (`read_only` for `readonly`) disarms a guard without "
            "a word. Posting a schema returns `unknown_keys_warning` on the same "
            "basis."),
    ),
]
