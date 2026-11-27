"""Retire les fichiers de projet dont l'échéance est passée — boucle de fond dédiée.

Un gros fichier déposé par parties (`large_uploads`) est rangé avec une échéance
(`project_files.expires_at`, 14 jours par défaut, `large_uploads.keep_days`). Cette
boucle la fait tenir : l'objet du stockage d'ABORD, la ligne ENSUITE — une panne entre
les deux laisse une ligne échue que le tour suivant reprend (`delete_by_key` ne lève
jamais sur un objet déjà parti), jamais un objet de plusieurs Go que plus rien ne
désigne. Le journal du projet le dit (`project.file_expired`).

N'écrit qu'en base et au stockage de l'instance : deux processus sur la même base
retirent au pire deux fois le même objet (`tiers=False`). La versionnement du seau garde
un objet retiré 30 jours (`infra/scaleway/media-versioning.sh`).

Hors de la boucle d'événements : le tour est SYNC, passé par `run_in_threadpool`.
"""
from __future__ import annotations

import asyncio
import logging

from starlette.concurrency import run_in_threadpool

logger = logging.getLogger(__name__)

_POLL_S = 3600
_LOT = 50


def purger(lot: int = _LOT) -> int:
    """Retire un lot de fichiers échus ; rend combien."""
    from . import db, media_store
    echus = db.project_files_expired(lot)
    for f in echus:
        media_store.delete_by_key(f["s3_key"])
        if db.delete_project_file(int(f["id"])):
            db.log_project_activity(int(f["project_id"]), None, "project.file_expired",
                                    f.get("title") or f.get("filename"))
    return len(echus)


async def run_fichiers_echus_loop(interval: int = _POLL_S) -> None:
    logger.info("fichiers_echus: démarré (poll %ss).", interval)
    while True:
        try:
            if await run_in_threadpool(purger, _LOT) >= _LOT:
                continue
        except Exception as e:  # noqa: BLE001 — un tour raté ne tue pas la boucle
            logger.warning("fichiers_echus: tour en échec : %s", e)
        await asyncio.sleep(interval)
