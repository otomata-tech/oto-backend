"""File d'envoi d'email différé (scheduled_emails).

Extrait de l'ex-monolithe `db.py` (barreau final). Fonctions de domaine — la
plomberie est dans `_conn`. Ré-exporté par `db/__init__`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
from datetime import date, datetime, timezone
from typing import Any, Iterator, Optional

import psycopg

logger = logging.getLogger(__name__)

from ._conn import _connect


_SCHED_MAX_ATTEMPTS = 3


def enqueue_scheduled_email(*, org_id: Optional[int], created_by: Optional[str],
                            to_email: str, subject: str, body_html: str,
                            from_email: Optional[str], from_name: Optional[str],
                            reply_to: Optional[str], transport: str,
                            scheduled_at: datetime) -> int:
    """Met un email en file pour envoi différé (HTML déjà rendu, autz déjà vérifiée).
    `scheduled_at` doit être un datetime aware (UTC). Retourne l'id."""
    with _connect() as conn:
        row = conn.execute(
            """INSERT INTO scheduled_emails
                 (org_id, created_by, to_email, subject, body_html, from_email,
                  from_name, reply_to, transport, scheduled_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING id""",
            (org_id, created_by, to_email, subject, body_html, from_email,
             from_name, reply_to, transport, scheduled_at),
        ).fetchone()
        return int(row["id"])


def claim_due_scheduled_emails(limit: int = 50) -> list[dict]:
    """Réclame atomiquement les emails dus (pending & scheduled_at <= now), en
    incrémentant `attempts` (claim). `FOR UPDATE SKIP LOCKED` = sûr même si deux
    boucles tournaient. Retourne les lignes à envoyer."""
    with _connect() as conn:
        rows = conn.execute(
            """UPDATE scheduled_emails SET attempts = attempts + 1
               WHERE id IN (
                   SELECT id FROM scheduled_emails
                   WHERE status = 'pending' AND scheduled_at <= NOW()
                   ORDER BY scheduled_at ASC
                   FOR UPDATE SKIP LOCKED
                   LIMIT %s)
               RETURNING id, org_id, to_email, subject, body_html, from_email,
                         from_name, reply_to, transport, attempts""",
            (max(1, int(limit)),),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_scheduled_sent(email_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE scheduled_emails SET status = 'sent', sent_at = NOW(), error = NULL "
            "WHERE id = %s", (email_id,),
        )


def mark_scheduled_failed(email_id: int, error: str) -> None:
    """Échec d'une tentative : repasse en `pending` pour réessayer au prochain tick
    tant que `attempts < _SCHED_MAX_ATTEMPTS` ; sinon fige en `failed`."""
    with _connect() as conn:
        conn.execute(
            """UPDATE scheduled_emails
               SET status = CASE WHEN attempts >= %s THEN 'failed' ELSE 'pending' END,
                   error = %s
               WHERE id = %s""",
            (_SCHED_MAX_ATTEMPTS, error[:500], email_id),
        )


def list_scheduled_emails(org_id: int, status: str = "pending", limit: int = 100) -> list[dict]:
    """Emails programmés d'une org (par statut ; 'all' = tous). Sans le HTML."""
    where = "org_id = %s"
    params: list = [org_id]
    if status and status != "all":
        where += " AND status = %s"
        params.append(status)
    params.append(max(1, int(limit)))
    with _connect() as conn:
        rows = conn.execute(
            f"""SELECT id, to_email, subject, from_email, from_name, transport, status,
                       scheduled_at, attempts, sent_at, error, created_at, created_by
                FROM scheduled_emails WHERE {where}
                ORDER BY scheduled_at ASC LIMIT %s""",
            tuple(params),
        ).fetchall()
        return [dict(r) for r in rows]


def cancel_scheduled_email(org_id: int, email_id: int) -> bool:
    """Annule un email encore `pending` de l'org. False si introuvable / déjà parti."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE scheduled_emails SET status = 'cancelled' "
            "WHERE id = %s AND org_id = %s AND status = 'pending'",
            (email_id, org_id),
        )
        return (cur.rowcount or 0) > 0
