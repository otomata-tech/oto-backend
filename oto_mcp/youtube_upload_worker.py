"""Worker des mises en ligne YouTube d'un gros fichier de projet — boucle de fond dédiée.

`youtube_act op=upload_video` sur une vidéo DÉPOSÉE dans un projet (jusqu'à des dizaines
de Go, `large_uploads`) ne lit pas le fichier : il dépose un travail
(`db.youtube_uploads.creer`) et rend son numéro. Cette boucle le réclame et relaie le
fichier du stockage vers YouTube **un morceau à la fois** (`CHUNK`, lu par plage dans le
stockage, `media_store.lire_plage`) : la mémoire ne tient jamais qu'un morceau.

**Reprise.** La session reprenable de YouTube est rangée chiffrée sur le travail dès son
ouverture, et chaque morceau accepté note le prochain octet que YOUTUBE attend
(`next_offset`, jamais notre propre compte). Un processus arrêté en cours (bascule
bleu/vert, redémarrage) laisse un bail (`lease_until`) qui échoit : un autre processus
réclame le travail, demande à YouTube où il en est (`video_job_status`) et repart de
là. Rien n'est renvoyé deux fois, rien n'est perdu.

**Hors de tout appel.** Le travail porte le contexte figé au dépôt (`sub`, `org_id`,
`project_id`, `account` = l'adresse Google résolue) : le worker le repose
(`session_org.set_call_org` / `set_call_project`, comme `jev_jobs_worker`) pour que la
cascade résolve le MÊME compte Google, et rafraîchit le jeton d'accès (une heure) en
cours de route. L'identité repasse par la garde (`garde_identite`), la lecture du projet
par `ownership.can_access` — à chaque réclamation.

**Échecs.** Un refus de YouTube (4xx : quota du jour, compte non vérifié, métadonnée
refusée) est TERMINAL, avec la raison lisible par `youtube op=job_status`. Une panne
passagère (5xx, réseau, stockage) se reprend plus tard (`reporter`, délai croissant),
au plus `MAX_ESSAIS` fois. Une session que YouTube ne connaît plus (404/410, ~une
semaine) est rouverte depuis le début.

Hors de la boucle d'événements, comme les autres workers (`docs/event-loop-perf.md`) :
le tour est SYNC, passé par `run_in_threadpool`.
"""
from __future__ import annotations

import asyncio
import logging
import time
from starlette.concurrency import run_in_threadpool

from . import media_store
from .crypto import decrypt as _decrypt
from .crypto import encrypt as _encrypt

logger = logging.getLogger(__name__)

#: Un morceau envoyé à YouTube : multiple de 256 Kio, lu par plage dans le stockage.
CHUNK = 64 * 1024 * 1024
#: Le bail d'un travail réclamé, prolongé à chaque morceau accepté : un morceau de
#: 64 Mio sur une ligne lente tient largement dans dix minutes.
BAIL_S = 900
#: Voies menées de front : chacune tient un thread du pool pendant toute la mise en
#: ligne (des heures pour un gros fichier). Deux, pour garder le pool au reste du serveur.
CONCURRENCE = 2
MAX_ESSAIS = 8
#: Le jeton d'accès Google vit une heure : on le redemande avant.
_JETON_S = 40 * 60
_POLL_S = 15
_HTTP_TIMEOUT = (10, 300)


def _aad(job_id: int) -> str:
    return f"youtube_upload_jobs:{job_id}"


def chiffrer_session(job_id: int, url: str) -> str:
    return _encrypt(url, _aad(job_id))


class _Terminal(Exception):
    """Un refus qui ne passera pas tout seul : le travail échoue, raison dite."""


def _client(job: dict):
    """Le client YouTube du compte figé au dépôt, résolu sous le contexte du dépôt."""
    from oto.tools.google.youtube import YouTubeClient

    from . import session_org
    from .auth import google as google_oauth
    remises = []
    if job.get("org_id") is not None:
        remises.append((session_org.reset_call_org,
                        session_org.set_call_org(int(job["org_id"]))))
    remises.append((session_org.reset_call_project,
                    session_org.set_call_project(int(job["project_id"]))))
    try:
        creds = google_oauth.credentials_for(job["sub"], account=job.get("account"),
                                             service="youtube")
    except RuntimeError as e:
        raise _Terminal(str(e)) from None
    finally:
        for reset, jeton in reversed(remises):
            reset(jeton)
    return YouTubeClient(creds.token, timeout=_HTTP_TIMEOUT)


def _juger(job: dict) -> None:
    from . import garde_identite, ownership
    if not job.get("s3_key"):
        raise _Terminal("the video file was removed from the project.")
    if (coupe := garde_identite.refus(job["sub"])):
        raise _Terminal(coupe[1])
    if not ownership.can_access(job["sub"], "project", str(job["project_id"]), "read"):
        raise _Terminal("the account that asked for this upload can no longer read the "
                        "project.")


def _refus_youtube(e) -> str:
    raisons = ", ".join(getattr(e, "reasons", None) or []) or ""
    return (f"YouTube refused the upload ({e.status_code}"
            f"{': ' + raisons if raisons else ''}): {e.message[:300]}")


def mener(job: dict) -> None:
    """Mène UN travail réclamé jusqu'au bout, ou jusqu'à un refus. Lève ce qui est
    passager (le tour le reporte)."""
    from oto.tools.google.youtube import YouTubeError

    from . import db
    jid, total, mime = int(job["id"]), int(job["total_bytes"]), job["mime"]
    _juger(job)
    client, pris_a = _client(job), time.monotonic()
    session = _decrypt(job["session_url_enc"], _aad(jid)) if job.get("session_url_enc") else None
    offset = 0
    try:
        if session:
            etat = client.video_job_status(session, total)
            if etat["done"]:
                return _fini(job, etat["video"])
            offset = etat["next_offset"]
    except YouTubeError as e:
        if e.status_code not in (404, 410):
            raise
        session = None                      # session oubliée par YouTube : on rouvre
    if not session:
        meta = dict(job.get("meta") or {})
        try:
            session = client.start_video_upload(
                total, mime, notify_subscribers=bool(job.get("notify_subscribers")), **meta)
        except ValueError as e:
            raise _Terminal(str(e)) from None
        db.youtube_uploads.poser_session(jid, chiffrer_session(jid, session))
        offset = 0
    while True:
        if time.monotonic() - pris_a > _JETON_S:
            client, pris_a = _client(job), time.monotonic()
        fin = min(offset + CHUNK, total) - 1
        data = media_store.lire_plage(job["s3_key"], offset, fin)
        if len(data) != fin - offset + 1:
            raise _Terminal(f"the stored file is shorter than expected ({total} bytes).")
        try:
            out = client.send_video_chunk(session, data, offset, total, mime)
        except YouTubeError as e:
            if e.status_code != 401:
                raise
            # Le jeton d'accès a expiré en cours de morceau : on le redemande et on
            # renvoie CE morceau, une fois — un second 401 remonte (reporté).
            client, pris_a = _client(job), time.monotonic()
            out = client.send_video_chunk(session, data, offset, total, mime)
        if out["done"]:
            return _fini(job, out["video"])
        offset = out["next_offset"]
        if not db.youtube_uploads.avancer(jid, offset, BAIL_S):
            logger.info("youtube_upload_worker: travail #%s n'est plus en cours.", jid)
            return


def _fini(job: dict, video: dict) -> None:
    from . import db
    vid = (video or {}).get("id") or ""
    db.youtube_uploads.terminer(int(job["id"]), vid)
    db.log_project_activity(int(job["project_id"]), job["sub"], "youtube.upload",
                            (job.get("meta") or {}).get("title") or vid)


def _tour() -> int:
    """Réclame un travail et le mène. Rend 1 si un travail a été pris, 0 sinon."""
    from oto.tools.google.youtube import YouTubeError

    from . import db
    job = db.youtube_uploads.reclamer(BAIL_S)
    if job is None:
        return 0
    jid = int(job["id"])
    try:
        mener(job)
    except _Terminal as e:
        db.youtube_uploads.echouer(jid, str(e))
    except YouTubeError as e:
        # 401 : le jeton d'accès a expiré en cours de morceau — le tour suivant le
        # redemande. 429 : débit. Le reste des 4xx est un refus qui ne passera pas seul.
        if e.status_code < 500 and e.status_code not in (401, 429):
            db.youtube_uploads.echouer(jid, _refus_youtube(e))
        else:
            _reporter(job, f"YouTube is unavailable for now ({e.status_code}).")
    except ValueError as e:                  # un morceau refusé par le client lui-même
        db.youtube_uploads.echouer(jid, f"internal error: {e}")
    except Exception as e:  # noqa: BLE001 — réseau, stockage, base : passager, reporté
        logger.warning("youtube_upload_worker: travail #%s interrompu : %s", jid, e)
        _reporter(job, f"interrupted ({type(e).__name__}); it will resume.")
    return 1


def _reporter(job: dict, raison: str) -> None:
    from . import db
    essais = int(job.get("attempts") or 1)
    if essais >= MAX_ESSAIS:
        db.youtube_uploads.echouer(int(job["id"]), f"{raison} — gave up after {essais} tries.")
        return
    db.youtube_uploads.reporter(int(job["id"]), raison, min(60 * 2 ** essais, 1800))


async def _voie(numero: int, interval: int) -> None:
    while True:
        pris = 0
        try:
            pris = await run_in_threadpool(_tour)
        except Exception as e:  # noqa: BLE001 — un tour raté ne tue pas la voie
            logger.warning("youtube_upload_worker[%d]: tour en échec : %s", numero, e)
        if not pris:
            await asyncio.sleep(interval)


async def run_youtube_upload_loop(interval: int = _POLL_S) -> None:
    logger.info("youtube_upload_worker: démarré (%d voies, poll %ss).", CONCURRENCE, interval)
    await asyncio.gather(*(_voie(i, interval) for i in range(CONCURRENCE)))


def etat_public(job: dict) -> dict:
    """Ce que `youtube op=job_status` rend d'un travail — jamais la session."""
    total = int(job["total_bytes"]) or 1
    out = {"job_id": job["id"], "status": job["status"],
           "progress": round(100 * int(job["next_offset"] or 0) / total, 1),
           "bytes": int(job["total_bytes"]), "title": (job.get("meta") or {}).get("title")}
    if job.get("video_id"):
        out["video_id"] = job["video_id"]
        out["studio_url"] = f"https://studio.youtube.com/video/{job['video_id']}/edit"
    if job.get("error"):
        out["error" if job["status"] == "failed" else "last_interruption"] = job["error"]
    return out

