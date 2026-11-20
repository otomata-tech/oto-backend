"""Exécution commune : préparation et I/O sync au thread, coroutine à la boucle.

`prepare` valide/résout le contexte et rend `(ctx, inp)`. Appeler une fonction
async ne joue pas son corps : le thread ne fait que construire sa coroutine,
attendue ensuite dans la boucle de l'appel. Le contexte anyio est copié, comme
pour les adaptateurs avant cette extraction.
"""
from __future__ import annotations

import inspect

from starlette.concurrency import run_in_threadpool

from ..datastore.errors import TableauParNom
from ._types import AuthzDenied
from .datastore.common import refus_nom_de_tableau


def _nom_de_tableau_sous(refus: AuthzDenied):
    """Le `TableauParNom` qu'un handler a traduit en refus générique, s'il y en a un.

    Les handlers rattrapent `DatastoreNotFound` (dont `TableauParNom` hérite) et lèvent
    leur 404 dans ce `except` : l'exception d'origine reste dans la chaîne."""
    e = refus.__cause__ or refus.__context__
    while e is not None:
        if isinstance(e, TableauParNom):
            return e
        e = e.__cause__ or e.__context__
    return None


async def execute(handler, prepare):
    """Une préparation, un handler ; aucune traduction d'erreur de transport."""
    def work():
        ctx, inp = prepare()
        try:
            return ctx, handler(ctx, inp)
        except AuthzDenied as refus:
            # Un nom de tableau est refusé sous SON code, quel que soit le 404 que le
            # handler a posé en le rattrapant comme « introuvable ».
            nom = _nom_de_tableau_sous(refus) if refus.code != "datastore_by_name" else None
            if nom is None:
                raise
            raise refus_nom_de_tableau(nom.datastore) from nom

    ctx, result = await run_in_threadpool(work)
    if inspect.isawaitable(result):
        result = await result
    return ctx, result
