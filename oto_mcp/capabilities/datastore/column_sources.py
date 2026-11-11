"""`me.datastore.column_sources` — GET …/column-sources : QUI a trouvé chaque colonne.

L'en-tête d'une colonne dit d'où viennent ses valeurs : « Jev », « Hunter 62 %,
FullEnrich 30 % », « à la main ». Lu dans `datastore_cell_sources`, qu'un
déclencheur entretient à chaque écriture d'après l'estampille (outil, fournisseur,
face) — jamais déclaré à côté, donc jamais périmé, et jamais deviné
(`db/attribution_cellules.py`, `db/historique.sources_des_colonnes`).

REST seule : c'est une lecture d'écran ; l'agent a `data_row_history`.

Autz : `SUB_ONLY` au seuil, le vrai gate est la LECTURE du tableau, résolue par le
store (org active + propriété) — un tableau hors périmètre est un 404, comme partout
dans le datastore.

⚠️ Deux lectures d'agrégat bornées (`statement_timeout`, route lourde), chacune
proportionnelle à la taille du tableau. Gardée `_TTL_S` secondes par tableau
dans ce processus : l'en-tête est relu à chaque ouverture du tableau, la provenance d'une
colonne ne change pas d'une seconde à l'autre.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from pydantic import BaseModel, Field

from ...datastore.core import DatastoreNotFound, make_store
from ...datastore.identite import Adresse
from ...db import historique
from .._authz import SUB_ONLY
from .._lecture_bornee import bornee
from .._types import AuthzDenied, Capability, RestBinding, ResolvedCtx
from ..registry import CAPABILITIES
from .common import EntreeDatastore

_TTL_S = 120.0
_MAX_ENTREES = 512
_cache: dict[int, tuple[float, dict]] = {}
_verrou = threading.Lock()


class ColumnSourcesInput(EntreeDatastore):
    datastore: Adresse


class SourceGroup(BaseModel):
    source: Optional[str] = Field(default=None, description=(
        "La face de l'écriture qui a posé la valeur : `agent`, `api`, `system`…"))
    tool: Optional[str] = Field(default=None, description=(
        "L'outil qui a trouvé la valeur (`jev_rows`, `waterfall_email_act`, l'outil "
        "qu'un `data_write(via=…)` a prouvé)."))
    provider: Optional[str] = Field(default=None, description=(
        "L'outil de l'étape d'une cascade qui a trouvé la valeur "
        "(`hunter_email_finder`) ; `null` hors cascade."))
    cells: int


class ColumnSources(BaseModel):
    columns: dict[str, list[SourceGroup]] = Field(description=(
        "Par colonne, les cases dont la valeur ACTUELLE a été trouvée par un outil, "
        "groupées par outil / fournisseur. Une case écrite à la main, importée, par un "
        "agent qui n'a pas nommé l'outil, ou avant la mise en service, n'y est pas."))
    held: Optional[dict[str, int]] = Field(default=None, description=(
        "Par colonne listée dans `columns`, le nombre de cases qui tiennent une valeur "
        "— le dénominateur des parts. `null` quand la lecture a dépassé sa borne : les "
        "parts se lisent alors entre cases attribuées seulement."))
    since: str = Field(description=(
        "La mise en service de l'attribution : une case écrite avant n'est pas "
        "attribuée."))


#: Le jour où l'attribution a été posée (la révision `20261111_1100_cell_sources`).
MISE_EN_SERVICE = "2026-11-11"


def _lire(ns_id: int) -> dict:
    maintenant = time.monotonic()
    with _verrou:
        garde = _cache.get(ns_id)
        if garde and maintenant - garde[0] < _TTL_S:
            return garde[1]
    brut = historique.sources_des_colonnes(ns_id)
    servi = {
        "columns": {
            champ: [{"source": g["source"], "tool": g["outil"],
                     "provider": g["fournisseur"], "cells": g["cellules"]}
                    for g in groupes]
            for champ, groupes in brut["colonnes"].items()},
        "held": brut["tenues"],
        "since": MISE_EN_SERVICE,
    }
    with _verrou:
        if len(_cache) >= _MAX_ENTREES:
            _cache.clear()
        _cache[ns_id] = (maintenant, servi)
    return servi


def _column_sources(ctx: ResolvedCtx, inp: ColumnSourcesInput) -> dict:
    store = make_store(ctx.sub)
    try:
        ns_id = store.resolve_ns_id(inp.datastore)
    except DatastoreNotFound:
        raise AuthzDenied(404, "datastore_not_found")
    return _lire(int(ns_id))


CAPABILITIES += [
    Capability(
        key="me.datastore.column_sources",
        handler=bornee(_column_sources),
        Input=ColumnSourcesInput,
        Output=ColumnSources,
        authz=SUB_ONLY,
        mcp=None,  # lecture d'écran ; l'agent a `data_row_history`
        rest=RestBinding(
            verb="GET",
            path="/api/datastores/{datastore}/column-sources",
        ),
        description="D'où viennent les valeurs de chaque colonne (outil, fournisseur, face).",
    ),
]
