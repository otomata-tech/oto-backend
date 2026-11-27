"""File des mises en ligne YouTube (`db/schema/youtube.py`) — `db.youtube_uploads.<fn>`.

NON aplati dans `db.*` : `creer`/`reclamer`/`avancer` sont trop communs.

Un travail se RÉCLAME par bail (`lease_until`) et non par statut seul : une mise en
ligne de plusieurs Go dure des heures, et un processus arrêté en cours (bascule
bleu/vert, redémarrage) la laisse `running`. Un bail échu la rend réclamable, et le
worker reprend là où YouTube s'est arrêté (`session_url_enc`, `next_offset`). Chaque
morceau envoyé prolonge le bail.
"""
from __future__ import annotations

import json
from typing import Optional

from ._conn import _connect

_COLS = ("id, project_id, file_id, sub, org_id, account, status, total_bytes, mime, "
         "meta, notify_subscribers, session_url_enc, next_offset, video_id, error, "
         "attempts, created_at, updated_at")


def creer(*, project_id: int, file_id: int, sub: str, org_id: Optional[int],
          account: Optional[str], total_bytes: int, mime: str, meta: dict,
          notify_subscribers: bool) -> dict:
    with _connect() as conn:
        row = conn.execute(
            "INSERT INTO youtube_upload_jobs (project_id, file_id, sub, org_id, account, "
            "total_bytes, mime, meta, notify_subscribers) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s) RETURNING {_COLS}",
            (project_id, file_id, sub, org_id, account, total_bytes, mime,
             json.dumps(meta), notify_subscribers)).fetchone()
        return dict(row)


def lire(job_id: int, sub: str) -> Optional[dict]:
    """Le travail `job_id` de `sub` — celui d'un autre compte ne se lit pas."""
    with _connect() as conn:
        row = conn.execute(f"SELECT {_COLS} FROM youtube_upload_jobs "
                           "WHERE id = %s AND sub = %s", (job_id, sub)).fetchone()
        return dict(row) if row else None


def reclamer(bail_s: int) -> Optional[dict]:
    """Réclame ATOMIQUEMENT le plus ancien travail ouvert dont personne ne tient le bail
    (`FOR UPDATE SKIP LOCKED`), le passe `running` et pose le bail. Avec la clé d'objet
    du fichier, lue à la réclamation (le fichier peut avoir changé de clé, jamais
    d'octets)."""
    with _connect() as conn:
        row = conn.execute(
            f"""
            WITH pris AS (
                SELECT id FROM youtube_upload_jobs
                 WHERE status IN ('pending', 'running')
                   AND (lease_until IS NULL OR lease_until < NOW())
                 ORDER BY created_at
                   FOR UPDATE SKIP LOCKED
                 LIMIT 1
            )
            UPDATE youtube_upload_jobs j
               SET status = 'running', attempts = attempts + 1,
                   lease_until = NOW() + make_interval(secs => %s), updated_at = NOW()
              FROM pris WHERE j.id = pris.id
            RETURNING {", ".join("j." + c.strip() for c in _COLS.split(","))},
                      (SELECT s3_key FROM project_files f WHERE f.id = j.file_id) AS s3_key
            """, (bail_s,)).fetchone()
        return dict(row) if row else None


def poser_session(job_id: int, session_url_enc: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE youtube_upload_jobs SET session_url_enc = %s, "
                     "updated_at = NOW() WHERE id = %s", (session_url_enc, job_id))


def avancer(job_id: int, next_offset: int, bail_s: int) -> bool:
    """Note le prochain octet attendu par YouTube et prolonge le bail. `False` si le
    travail n'est plus `running` (annulé, ou fichier supprimé entre-temps). Un morceau
    accepté REMET les essais à zéro : `MAX_ESSAIS` compte les pannes d'affilée, pas les
    reprises d'une mise en ligne de plusieurs heures (déploiements, jetons expirés)."""
    with _connect() as conn:
        row = conn.execute(
            "UPDATE youtube_upload_jobs SET next_offset = %s, attempts = 0, "
            "lease_until = NOW() + make_interval(secs => %s), updated_at = NOW() "
            "WHERE id = %s AND status = 'running' RETURNING id",
            (next_offset, bail_s, job_id)).fetchone()
        return row is not None


def terminer(job_id: int, video_id: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE youtube_upload_jobs SET status = 'done', video_id = %s, "
                     "next_offset = total_bytes, session_url_enc = NULL, "
                     "lease_until = NULL, error = NULL, updated_at = NOW() WHERE id = %s",
                     (video_id, job_id))


def echouer(job_id: int, error: str) -> None:
    """Échec TERMINAL : la session est oubliée, le travail ne se reprend plus."""
    with _connect() as conn:
        conn.execute("UPDATE youtube_upload_jobs SET status = 'failed', error = %s, "
                     "session_url_enc = NULL, lease_until = NULL, updated_at = NOW() "
                     "WHERE id = %s", (error[:2000], job_id))


def reporter(job_id: int, error: str, delai_s: int) -> None:
    """Échec PASSAGER : le travail se reprendra après `delai_s` (bail posé au futur)."""
    with _connect() as conn:
        conn.execute("UPDATE youtube_upload_jobs SET status = 'pending', error = %s, "
                     "lease_until = NOW() + make_interval(secs => %s), updated_at = NOW() "
                     "WHERE id = %s", (error[:2000], delai_s, job_id))
