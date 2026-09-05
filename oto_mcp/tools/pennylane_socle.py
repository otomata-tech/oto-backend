"""Socle partagé des modules du connecteur `pennylane` — oto-backend#872.

Le connecteur tient sur plusieurs modules (`tools/pennylane*.py`, cf.
`Connector.modules` au registre). Ce fichier porte ce qu'ils ont en commun :
la résolution de la clé, les deux formes d'erreur, et surtout la **traduction
d'un refus amont en exception**.

Pourquoi un fichier plutôt qu'une copie par module : le client d'oto-core rend
un refus comme une *valeur* (`{"error": "422", "details": …}`) et non comme une
exception. La pièce qui rattrape ça ne doit exister qu'une fois — dupliquée,
elle diverge, et c'est le module oublié qui écrira dans une comptabilité sans
que personne le voie.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from mcp.types import ErrorData, INVALID_PARAMS

from ..mcp_errors import McpError
from .. import access

if TYPE_CHECKING:  # l'annotation de `_client()` seulement — jamais évaluée
    from oto.tools.pennylane import PennylaneClient


def _client() -> PennylaneClient:
    """Le client Pennylane pour la clé de CET appelant.

    L'import réel est fait dans le corps, pas au chargement du module : les
    tests remplacent `PennylaneClient` sur le package, et un import différé est
    ce qui leur laisse la main.

    Le type de retour est annoté avec la classe NUE, et pas seulement pour la
    lecture : la sonde de version-skew lit cette annotation pour savoir contre
    quel client vérifier que les méthodes appelées ici existent dans l'oto-core
    épinglé. Sans elle, ce module passerait à travers ce contrôle.
    """
    from oto.tools.pennylane import PennylaneClient

    key, _is_platform = access.resolve_api_key("pennylane")
    # Rédaction appliquée à la frontière des tools par `FieldRedactionMiddleware`
    # (policy de l'org active), plus au niveau client.
    return PennylaneClient(api_key=key)


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _need(value, name: str, op: str):
    """Argument obligatoire pour CET op — erreur actionnable, jamais de fallback."""
    if value is None:
        raise _bad(f"op='{op}' requiert {name}")
    return value


def _ecrit(appel, geste: str):
    """EXÉCUTE une écriture Pennylane et rend son retour, ou LÈVE en orientant.

    Prend une fonction, pas un résultat : depuis oto-core#77 le client lève sur
    refus amont, et une exception levée dans l'argument n'atteindrait jamais un
    contrôle placé après l'appel. Le geste doit se produire ici, sous la garde.

    La taxonomie du backend classe déjà `UpstreamHTTPError` ; ce que cette garde
    ajoute lui est propre au connecteur : dire à l'agent QUOI FAIRE. Un 401/403
    sur Pennylane n'est presque jamais un argument à corriger, c'est un droit qui
    manque à la clé — et rien ne le montrait avant l'échec.
    """
    from oto.tools.common.errors import UpstreamHTTPError

    try:
        return appel()
    except UpstreamHTTPError as e:
        st, detail = e.status_code, str(e.body)[:400]
    except RuntimeError as e:
        # Refus sans statut HTTP : réseau, débit limité, corps illisible.
        raise _bad(f"Pennylane n'a pas répondu à {geste} : {e}") from e

    if st in (401, 403):
        raise _bad(
            f"Pennylane a refusé {geste} ({st}) : c'est un DROIT qui manque à la "
            "clé, pas un argument à corriger — rejouer à l'identique échouera "
            "pareil. Chaque utilisateur pose sa propre clé, avec son propre "
            "périmètre : qu'un tool soit monté ne prouve donc AUCUN droit. Lis "
            "les droits réels de la clé avec `pennylane_ref(kind=\"company\")`, "
            f"champ `scopes`, puis dis à l'utilisateur lequel manque. Détail : {detail}")
    if st == 422:
        raise _bad(f"Pennylane a refusé le CONTENU de {geste} ({st}) : les valeurs "
                   f"envoyées ne passent pas ses contrôles. Détail : {detail}")
    if st == 404:
        raise _bad(f"Pennylane ne trouve pas la cible de {geste} ({st}) : l'id "
                   f"n'existe pas dans CETTE société. Détail : {detail}")
    raise _bad(f"Pennylane a refusé {geste} ({st}). Détail : {detail}")
