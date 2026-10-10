"""La face d'une lecture d'agrégat bornée : `LectureTropLongue` → refus NOMMÉ.

oto-backend#1145. `db.lecture_bornee` coupe une lecture d'agrégat au-delà de sa durée
maximale et lève `LectureTropLongue`. Sans traduction, l'adaptateur la rendrait en 500
anonyme — une panne, là où il y a une réponse à donner : la lecture était trop large,
la resserrer. `bornee(handler)` enveloppe le handler d'une capacité de lecture
d'agrégat et rend `503 aggregate_timeout`, avec le message de la lecture, sur les deux
faces (REST et MCP lisent `AuthzDenied` de la même façon).

Un décorateur plutôt qu'un `try` par handler : la règle est UNE, et la liste des
capacités bornées se lit à leur déclaration (`handler=bornee(…)`).

oto-backend#1147. Les mêmes lectures passent par les totaux du journal par jour
(`db/journal_jour.py`), qui lèvent `AgregatIncomplet` quand un jour clos de la fenêtre
n'est pas consolidé (maintenance en retard, historique non rattrapé, trou). Ce n'est pas
la faute de l'appelant : la face rend `503 aggregate_incomplete`, avec le motif et le
geste qui répare, et le JOURNALISE en erreur — le chiffre n'est jamais lu au journal à la
place, en silence.
"""
from __future__ import annotations

import functools
import inspect
import logging

from ..db import journal_jour, lecture_bornee
from ._types import AuthzDenied

logger = logging.getLogger(__name__)

#: Le statut et le code du refus — servis, donc stables.
STATUT = 503
CODE = "aggregate_timeout"
#: Le refus d'une lecture dont les totaux par jour sont incomplets (#1147).
CODE_INCOMPLET = "aggregate_incomplete"


def bornee(handler):
    """Le handler, dont une `LectureTropLongue` ou un `AgregatIncomplet` sort en
    `AuthzDenied(503, …)`.

    Handler SYNCHRONE seulement : d'une coroutine, l'enveloppe ne verrait que la
    construction, pas l'exécution — elle laisserait passer l'erreur sans le dire."""
    if inspect.iscoroutinefunction(handler):
        raise TypeError(f"bornee({handler.__name__}) : handler asynchrone non supporté")

    @functools.wraps(handler)
    def _borne(ctx, inp):
        try:
            return handler(ctx, inp)
        except lecture_bornee.LectureTropLongue as e:
            raise AuthzDenied(STATUT, CODE, str(e)) from e
        except journal_jour.AgregatIncomplet as e:
            logger.error("totaux du journal incomplets : %s", e)
            raise AuthzDenied(STATUT, CODE_INCOMPLET, str(e)) from e
    return _borne
