"""Rafraîchissement périodique de l'index BOAMP (france-opendata#3).

« Live in server » : une boucle de fond démarrée au boot (lifespan, cf. server.py),
sur le modèle exact du scheduler d'email (`scheduler.run_scheduler_loop`). Elle crawle
la fenêtre récente du dump XML DILA (echanges.dila.gouv.fr — joignable, contrairement à
OpenDataSoft bloqué datacenter) et upsert dans la table PG `boamp`. Idempotent
(ON CONFLICT idweb). Le travail est SYNC (réseau + DB) → isolé en `asyncio.to_thread`
pour ne pas bloquer l'event loop, et batché (mémoire minuscule sur la box).

Nécessaire car un appel d'offres « ouvert » périme vite (date limite de réponse) : la
fraîcheur n'est pas optionnelle pour ce connecteur.

Garde de fraîcheur : on saute le tick si l'index a été rafraîchi il y a moins de ~20h
→ pas de crawl à chaque (re)déploiement. Opt-out via OTO_BOAMP_REFRESH_ENABLED=0.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import os
import time

from . import db

log = logging.getLogger("oto_mcp.boamp_refresh")

_INTERVAL_S = int(os.environ.get("OTO_BOAMP_REFRESH_INTERVAL_S", str(24 * 3600)))
_WINDOW_DAYS = int(os.environ.get("OTO_BOAMP_REFRESH_WINDOW_DAYS", "10"))
_MIN_GAP_S = 20 * 3600  # ne pas recrawler si rafraîchi il y a moins de ~20h
_BATCH = 400
_WORKERS = 16


def _refresh_once() -> int:
    """SYNC (réseau + DB) — appelé via to_thread. Retourne le nb d'avis upsertés
    (0 si index déjà frais)."""
    last = db.boamp_last_ingested_epoch()
    if last is not None and (time.time() - last) < _MIN_GAP_S:
        return 0

    from france_opendata import boamp_ingest

    since = (_dt.date.today() - _dt.timedelta(days=_WINDOW_DAYS)).isoformat()
    years = sorted({_dt.date.today().year, _dt.date.fromisoformat(since).year})
    sess = boamp_ingest._session()
    urls = boamp_ingest.iter_avis_urls(sess, years, since=since)
    total = 0
    for i in range(0, len(urls), _BATCH):
        rows = boamp_ingest.fetch_rows(urls[i:i + _BATCH], max_workers=_WORKERS)
        total += db.upsert_boamp(rows)
    return total


async def run_boamp_refresh_loop(interval: int = _INTERVAL_S) -> None:
    """Boucle de fond : rafraîchit l'index BOAMP toutes `interval` secondes. Isolée
    en thread. Ne meurt jamais sur une erreur de tick."""
    log.info("boamp refresh démarré (intervalle %ss, fenêtre %sj)", interval, _WINDOW_DAYS)
    while True:
        try:
            n = await asyncio.to_thread(_refresh_once)
            if n:
                log.info("boamp refresh : %d avis upsertés", n)
        except asyncio.CancelledError:
            log.info("boamp refresh arrêté")
            raise
        except Exception as e:  # un tick raté ne tue pas la boucle
            log.warning("boamp refresh tick échoué : %s", e)
        await asyncio.sleep(interval)
