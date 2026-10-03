"""Le partage d'UNE procédure par lien — tables `process_shares`, `process_share_readers`,
`process_readers_digest_optouts` (fragment `db/schema/procedures.py::PROCESS_SHARES`).

Le store seul : aucune règle d'accès ici, aucune forme de réponse. La capacité
(`capabilities/partages_procedure.py`) décide qui a le droit et ce qui est servi ;
le résumé quotidien (`digest_lecteurs.py`) lit les lecteurs à résumer.

Non aplati dans `db.*` (comme `apollo_reveals`) : `publier`/`retirer`/`lecteurs` sont
des noms trop communs pour la surface plate. Les appelants écrivent
`from ..db import partages_procedure as db_partages`.

⚠️ Toutes ces fonctions sont SYNCHRONES (psycopg) : un appelant async passe par
`run_in_threadpool` (`docs/event-loop-perf.md`).
"""
from __future__ import annotations

import secrets
from datetime import date, timedelta
from typing import Any, Optional

from psycopg.types.json import Jsonb

from ._conn import _connect

#: Fenêtre gardée du compteur quotidien des vues de vitrine.
VUES_JOURS = 90

_COLONNES = ("id, instruction_id, org_id, token, show_readers, preview_shape, "
             "shape_version, preview_views, created_by, created_at, updated_at, revoked_at")


def _jeton() -> str:
    """Un jeton d'URL : 15 octets aléatoires, 20 caractères url-safe. Il EST le
    secret du lien — jamais dérivé du slug ni de l'id."""
    return secrets.token_urlsafe(15)


def actif(instruction_id: int) -> Optional[dict]:
    """Le partage ACTIF d'une procédure, ou None."""
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {_COLONNES} FROM process_shares "
            "WHERE instruction_id = %s AND revoked_at IS NULL", (instruction_id,)
        ).fetchone()
    return dict(row) if row else None


def par_jeton(token: str) -> Optional[dict]:
    """Le partage ACTIF que désigne `token`, ou None (inconnu ou retiré)."""
    if not token:
        return None
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {_COLONNES} FROM process_shares "
            "WHERE token = %s AND revoked_at IS NULL", (token,)
        ).fetchone()
    return dict(row) if row else None


def publier(instruction_id: int, org_id: Optional[int], created_by: str, *,
            show_readers: bool = True, preview_shape: Optional[dict] = None,
            shape_version: Optional[int] = None) -> tuple[dict, bool]:
    """Pose le partage actif de la procédure. Idempotent : un partage déjà actif est
    rendu tel quel (ses réglages ne bougent pas — c'est `regler` qui les change).
    Rend `(partage, cree)`."""
    with _connect() as conn:
        row = conn.execute(
            f"""INSERT INTO process_shares
                    (instruction_id, org_id, token, show_readers, preview_shape,
                     shape_version, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (instruction_id) WHERE revoked_at IS NULL DO NOTHING
                RETURNING {_COLONNES}""",
            (instruction_id, org_id, _jeton(), show_readers,
             Jsonb(preview_shape) if preview_shape is not None else None,
             shape_version, created_by),
        ).fetchone()
    if row:
        return dict(row), True
    existant = actif(instruction_id)
    if existant is None:  # retiré entre les deux lectures : on réessaie une fois
        return publier(instruction_id, org_id, created_by, show_readers=show_readers,
                       preview_shape=preview_shape, shape_version=shape_version)
    return existant, False


def retirer(instruction_id: int) -> bool:
    """Retire le partage actif (le lien cesse de répondre). Les lecteurs restent.
    Rend True si un partage était actif."""
    with _connect() as conn:
        row = conn.execute(
            "UPDATE process_shares SET revoked_at = NOW(), updated_at = NOW() "
            "WHERE instruction_id = %s AND revoked_at IS NULL RETURNING id",
            (instruction_id,),
        ).fetchone()
    return row is not None


_ABSENT: Any = object()


def regler(instruction_id: int, *, show_readers: Optional[bool] = None,
           preview_shape: Any = _ABSENT, shape_version: Any = _ABSENT) -> Optional[dict]:
    """Change les réglages du partage actif. None s'il n'y en a pas."""
    sets, vals = [], []
    if show_readers is not None:
        sets.append("show_readers = %s")
        vals.append(bool(show_readers))
    if preview_shape is not _ABSENT:
        sets.append("preview_shape = %s")
        vals.append(Jsonb(preview_shape) if preview_shape is not None else None)
    if shape_version is not _ABSENT:
        sets.append("shape_version = %s")
        vals.append(shape_version)
    if not sets:
        return actif(instruction_id)
    with _connect() as conn:
        row = conn.execute(
            f"UPDATE process_shares SET {', '.join(sets)}, updated_at = NOW() "
            f"WHERE instruction_id = %s AND revoked_at IS NULL RETURNING {_COLONNES}",
            (*vals, instruction_id),
        ).fetchone()
    return dict(row) if row else None


def compter_vue(share_id: int, *, aujourd_hui: Optional[date] = None) -> None:
    """+1 au compteur du jour, et retire les jours sortis de la fenêtre. Aucune IP,
    aucun cookie : un nombre par jour, rien d'autre."""
    jour = aujourd_hui or date.today()
    seuil = (jour - timedelta(days=VUES_JOURS)).isoformat()
    cle = jour.isoformat()
    with _connect() as conn:
        conn.execute(
            """UPDATE process_shares SET preview_views = COALESCE((
                   SELECT jsonb_object_agg(k, v) FROM jsonb_each(
                       preview_views || jsonb_build_object(
                           %s::text, COALESCE((preview_views->>%s)::int, 0) + 1)) AS e(k, v)
                   WHERE k >= %s), '{}'::jsonb)
               WHERE id = %s""",
            (cle, cle, seuil, share_id),
        )


def noter_lecture(share_id: int, reader_sub: str, recorded: bool) -> dict:
    """Une lecture connectée : crée la ligne du lecteur ou la met à jour. `recorded`
    suit le réglage du partage AU MOMENT de la lecture — une lecture faite sous
    « ne pas voir qui lit » ne devient pas visible parce que le réglage change ensuite."""
    with _connect() as conn:
        row = conn.execute(
            """INSERT INTO process_share_readers (share_id, reader_sub, recorded)
               VALUES (%s, %s, %s)
               ON CONFLICT (share_id, reader_sub) DO UPDATE
                   SET last_read_at = NOW(), reads = process_share_readers.reads + 1,
                       recorded = process_share_readers.recorded AND EXCLUDED.recorded
               RETURNING *""",
            (share_id, reader_sub, recorded),
        ).fetchone()
    return dict(row)


def lecteur(share_id: int, reader_sub: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM process_share_readers WHERE share_id = %s AND reader_sub = %s",
            (share_id, reader_sub),
        ).fetchone()
    return dict(row) if row else None


def copie_existante(instruction_id: int, reader_sub: str) -> Optional[int]:
    """L'id de la copie déjà faite par ce lecteur depuis N'IMPORTE QUEL partage de
    cette procédure (un lien retiré puis republié porte un autre jeton, pas une autre
    procédure)."""
    with _connect() as conn:
        row = conn.execute(
            """SELECT r.copied_instruction_id FROM process_share_readers r
               JOIN process_shares s ON s.id = r.share_id
               WHERE s.instruction_id = %s AND r.reader_sub = %s
                 AND r.copied_instruction_id IS NOT NULL
               ORDER BY r.copied_at DESC LIMIT 1""",
            (instruction_id, reader_sub),
        ).fetchone()
    return int(row["copied_instruction_id"]) if row else None


def noter_copie(share_id: int, reader_sub: str, recorded: bool,
                copied_instruction_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO process_share_readers
                   (share_id, reader_sub, recorded, copied_at, copied_instruction_id)
               VALUES (%s, %s, %s, NOW(), %s)
               ON CONFLICT (share_id, reader_sub) DO UPDATE
                   SET copied_at = NOW(),
                       copied_instruction_id = EXCLUDED.copied_instruction_id""",
            (share_id, reader_sub, recorded, copied_instruction_id),
        )


def nombre_lecteurs(instruction_id: int, org_id: Optional[int]) -> int:
    """Lecteurs VISIBLES (recorded) de la procédure, tous partages confondus, hors
    membres de l'org propriétaire."""
    with _connect() as conn:
        row = conn.execute(
            """SELECT COUNT(DISTINCT r.reader_sub) AS n FROM process_share_readers r
               JOIN process_shares s ON s.id = r.share_id
               WHERE s.instruction_id = %s AND r.recorded
                 AND NOT EXISTS (SELECT 1 FROM org_members m
                                 WHERE m.org_id = %s AND m.sub = r.reader_sub)""",
            (instruction_id, org_id),
        ).fetchone()
    return int(row["n"])


def lecteurs(instruction_id: int, org_id: Optional[int]) -> list[dict]:
    """Les lecteurs VISIBLES de la procédure, tous partages confondus (un lecteur =
    une ligne), hors membres de l'org propriétaire — du plus récent au plus ancien."""
    with _connect() as conn:
        rows = conn.execute(
            """SELECT r.reader_sub, u.name, u.email,
                      MIN(r.first_read_at) AS first_read_at,
                      MAX(r.last_read_at) AS last_read_at,
                      SUM(r.reads)::int AS reads,
                      MAX(r.copied_at) AS copied_at
               FROM process_share_readers r
               JOIN process_shares s ON s.id = r.share_id
               LEFT JOIN users u ON u.sub = r.reader_sub
               WHERE s.instruction_id = %s AND r.recorded
                 AND NOT EXISTS (SELECT 1 FROM org_members m
                                 WHERE m.org_id = %s AND m.sub = r.reader_sub)
               GROUP BY r.reader_sub, u.name, u.email
               ORDER BY MAX(r.last_read_at) DESC""",
            (instruction_id, org_id),
        ).fetchall()
    return [dict(r) for r in rows]


def vues(instruction_id: int, jours: int = 30, *,
         aujourd_hui: Optional[date] = None) -> int:
    """Vues de vitrine des `jours` derniers jours, tous partages de la procédure."""
    jour = aujourd_hui or date.today()
    seuil = (jour - timedelta(days=jours - 1)).isoformat()
    with _connect() as conn:
        row = conn.execute(
            """SELECT COALESCE(SUM(e.v::int), 0)::int AS n
               FROM process_shares s, jsonb_each_text(s.preview_views) AS e(k, v)
               WHERE s.instruction_id = %s AND e.k >= %s""",
            (instruction_id, seuil),
        ).fetchone()
    return int(row["n"])


# ── Le résumé quotidien des lecteurs ───────────────────────────────────────────

def a_resumer() -> list[dict]:
    """Les lecteurs visibles pas encore résumés, sur les partages ACTIFS dont le
    propriétaire n'a pas refusé le résumé. Une ligne par (partage, lecteur), avec ce
    qu'il faut pour écrire le mail ; les membres de l'org propriétaire sont exclus."""
    with _connect() as conn:
        rows = conn.execute(
            """SELECT s.id AS share_id, s.instruction_id, s.org_id, s.created_by,
                      r.reader_sub, u.name AS reader_name, u.email AS reader_email,
                      r.copied_at, r.first_read_at
               FROM process_share_readers r
               JOIN process_shares s ON s.id = r.share_id
               LEFT JOIN users u ON u.sub = r.reader_sub
               WHERE r.recorded AND r.digested_at IS NULL
                 AND s.revoked_at IS NULL AND s.show_readers
                 AND NOT EXISTS (SELECT 1 FROM process_readers_digest_optouts o
                                 WHERE o.sub = s.created_by)
                 AND NOT EXISTS (SELECT 1 FROM org_members m
                                 WHERE m.org_id = s.org_id AND m.sub = r.reader_sub)
               ORDER BY s.created_by, s.id, r.first_read_at""",
        ).fetchall()
    return [dict(r) for r in rows]


def marquer_resumes(paires: list[tuple[int, str]]) -> None:
    """Pose `digested_at` sur les (partage, lecteur) qui viennent de partir."""
    if not paires:
        return
    with _connect() as conn:
        for share_id, sub in paires:
            conn.execute(
                "UPDATE process_share_readers SET digested_at = NOW() "
                "WHERE share_id = %s AND reader_sub = %s", (share_id, sub))


def refuser_resume(sub: str, *, source: str = "link") -> None:
    """`sub` ne veut plus du résumé des lecteurs. Idempotent."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO process_readers_digest_optouts (sub, source) VALUES (%s, %s) "
            "ON CONFLICT (sub) DO NOTHING", (sub, source))
