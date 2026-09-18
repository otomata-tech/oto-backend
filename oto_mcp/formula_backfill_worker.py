"""Recalcul en fond des colonnes `type: "formula"` (oto-backend#1008 v2) — draine
l'outbox `datastore_rows.formula_dirty`.

Boucle de fond démarrée au lifespan (patron `rank_backfill_worker.py`). Poser ou
modifier une formule (`data_set_schema`/`data_patch_schema`) ne recalcule plus les
lignes existantes SYNCHRONE dans l'appel : mesuré sur un vivier de 8910 lignes,
1min34-1min47, au-delà du délai du client MCP (`schema_ops.py::set_schema` ne fait
plus qu'un `UPDATE ... SET formula_dirty = TRUE` de masse et rend). Ce worker
draine par tranches, verrou PAR LIGNE (`datastore_merge_row_locked`), jamais sur la
table.

Réconciliation, pas file de tâches : une row `formula_dirty` reste éligible tant
qu'elle n'est pas passée par un tour — aucun état intermédiaire « en cours » à
perdre sur un crash entre deux tours, le tour suivant la reprend telle quelle.
"""
from __future__ import annotations

import asyncio
import logging

from starlette.concurrency import run_in_threadpool

from . import db
from .datastore import formule as dsformule
from .datastore import schema as dsv2
from .datastore.outils import _now_iso

logger = logging.getLogger(__name__)

_POLL_S = 10
_BATCH = 500
# Combien de namespaces dirty traiter par tour — borne le travail d'UN tour, comme
# `rank_backfill_worker` borne sa tranche par source.
_NAMESPACES_PAR_TOUR = 20


def _appliquer_formules_en_place(schema: dict, merged: dict) -> None:
    """Même geste que `ControlesMixin._appliquer_formules` (`controles.py`), en
    fonction libre : le worker n'a pas d'instance de store, seulement `schema` et
    la `data` d'une row — la SEULE chose dont le calcul de formule a besoin."""
    plate = {k: dsv2.unwrap(v) for k, v in merged.items()}
    for cle, resultat in dsformule.compute_row_formulas(schema, plate).items():
        existant = merged.get(cle)
        couches = dict(existant) if isinstance(existant, dict) else {}
        couches[dsv2.VALUE_LAYER] = resultat["valeur"]
        if "comment" in resultat:
            couches["comment"] = resultat["comment"]
        merged[cle] = couches


def _backfill_round() -> dict:
    """Un tour SYNC (threadpool) : une tranche par namespace dirty. Renvoie
    `{ns_id: nombre_de_rows_traitees}`."""
    now = _now_iso()
    faits: dict = {}
    for ns_id in db.datastore_namespaces_avec_formule_dirty(_NAMESPACES_PAR_TOUR):
        ns = db.get_datastore_by_id(ns_id)
        schema = (ns or {}).get("schema")
        a_des_formules = schema and any(
            isinstance(f, dict) and f.get("type") == "formula"
            for f in (schema.get("fields") or []))
        if not a_des_formules:
            # Namespace supprimé, ou schéma changé entre le marquage et ce tour
            # (formule retirée) : rien à calculer, on baisse juste le drapeau.
            db.datastore_clear_formula_dirty_ns(ns_id)
            continue
        rows = db.datastore_list_formula_dirty(ns_id, _BATCH)
        if not rows:
            continue
        n = 0
        for r in rows:
            row_id = r["row_id"]

            def _apply(current: dict, _schema=schema) -> dict:
                _appliquer_formules_en_place(_schema, current)
                return current

            try:
                db.datastore_merge_row_locked(ns_id, row_id, _apply, now)
            except Exception as e:  # noqa: BLE001 — une row en échec ne bloque pas les autres
                logger.warning("formula_backfill: ns=%s row=%s échoué (re-tenté) : %s",
                               ns_id, row_id, e)
                continue
            db.datastore_clear_formula_dirty(ns_id, row_id)
            n += 1
        if n:
            faits[ns_id] = n
    return faits


async def run_formula_backfill_loop(interval: int = _POLL_S) -> None:
    """La boucle, composée au lifespan (`server._bg_loops`, `boucles_de_fond.py`).
    Aucun effet chez un tiers (`tiers=False`) : ne lit/écrit que `datastore_rows`."""
    logger.info("formula_backfill: démarré (poll %ss, tranche %s).", interval, _BATCH)
    while True:
        try:
            faits = await run_in_threadpool(_backfill_round)
            if faits:
                logger.info("formula_backfill: %s",
                            ", ".join(f"ns={ns}+{n}" for ns, n in faits.items()))
        except Exception as e:  # noqa: BLE001 — un tour raté ne tue pas la boucle
            logger.warning("formula_backfill: tour en échec : %s", e)
        await asyncio.sleep(interval)
