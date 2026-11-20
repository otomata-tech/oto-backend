"""Les demandes d'effacement (`org_erasures`) : leur cycle, et rien d'autre.

    requested ──confirmation du titulaire──▶ scheduled ──échéance──▶ running ──▶ done
        │                                       │                      │
        └─ annulée / lien échu ─▶ cancelled / expired                  └─ trop d'échecs ─▶ failed

- `requested` : un lien à usage unique est parti chez le titulaire ; seule son
  EMPREINTE est gardée. Il échoit (`DUREE_DU_LIEN`).
- `scheduled` : le titulaire a confirmé ; l'org est suspendue et la purge est fixée à
  `scheduled_for` (confirmation + `DELAI_DE_GRACE`). Encore annulable.
- `running` : la purge a commencé — plus d'annulation. Un passage qui échoue garde
  l'état et note `last_error` ; le suivant reprend. Au-delà de `ESSAIS_MAX`, `failed`
  appelle un opérateur.
- `done` : le reçu est écrit ; ce qui était personnel dans la demande est retiré.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from typing import Optional

from psycopg.types.json import Jsonb

from ..db import _connect

DELAI_DE_GRACE = timedelta(days=7)       # décision du 12/11/2026
DUREE_DU_LIEN = timedelta(hours=72)
ESSAIS_MAX = 5
VIVANTES = ("requested", "scheduled", "running")

_COLONNES_PUBLIQUES = (
    "id, org_id, state, via, reason, requested_by, requested_at, token_expires_at, "
    "confirm_sub, confirm_email, org_name, confirmed_at, scheduled_for, cancelled_by, "
    "cancelled_at, started_at, finished_at, attempts, last_error, plan, receipt, "
    "commerce_erased_at")


class DemandeEnConflit(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def nouveau_jeton() -> tuple[str, str]:
    """(jeton porté par le lien, empreinte gardée en base)."""
    jeton = secrets.token_urlsafe(32)
    return jeton, empreinte(jeton)


def empreinte(jeton: str) -> str:
    return hashlib.sha256(jeton.encode()).hexdigest()


def vivante(org_id: int) -> Optional[dict]:
    with _connect() as conn:
        r = conn.execute(f"SELECT {_COLONNES_PUBLIQUES} FROM org_erasures "
                         "WHERE org_id = %s AND state = ANY(%s)", (org_id, list(VIVANTES))).fetchone()
    return dict(r) if r else None


def derniere(org_id: int) -> Optional[dict]:
    with _connect() as conn:
        r = conn.execute(f"SELECT {_COLONNES_PUBLIQUES} FROM org_erasures WHERE org_id = %s "
                         "ORDER BY id DESC LIMIT 1", (org_id,)).fetchone()
    return dict(r) if r else None


def lire(id_: int) -> Optional[dict]:
    with _connect() as conn:
        r = conn.execute(f"SELECT {_COLONNES_PUBLIQUES}, snapshot FROM org_erasures WHERE id = %s",
                         (id_,)).fetchone()
    return dict(r) if r else None


def creer(*, org_id: int, via: str, reason: Optional[str], requested_by: str,
          confirm_sub: str, confirm_email: str, org_name: str, token_hash: str,
          plan: dict) -> dict:
    """Pose la demande `requested`. Lève `DemandeEnConflit` si une demande vit déjà."""
    import psycopg
    try:
        with _connect() as conn:
            r = conn.execute(
                "INSERT INTO org_erasures (org_id, via, reason, requested_by, token_hash, "
                "  token_expires_at, confirm_sub, confirm_email, org_name, plan) "
                "VALUES (%s, %s, %s, %s, %s, NOW() + %s, %s, %s, %s, %s) "
                f"RETURNING {_COLONNES_PUBLIQUES}",
                (org_id, via, reason, requested_by, token_hash, DUREE_DU_LIEN, confirm_sub,
                 confirm_email, org_name, Jsonb(plan))).fetchone()
    except psycopg.errors.UniqueViolation as e:
        raise DemandeEnConflit("erasure_pending",
                               "Une demande d'effacement est déjà en cours pour cette org.") from e
    return dict(r)


def par_jeton(token_hash: str) -> Optional[dict]:
    with _connect() as conn:
        r = conn.execute(f"SELECT {_COLONNES_PUBLIQUES} FROM org_erasures WHERE token_hash = %s",
                         (token_hash,)).fetchone()
    return dict(r) if r else None


def confirmer(id_: int, *, sub: str, snapshot: dict, plan: dict) -> Optional[dict]:
    """`requested` → `scheduled`, si le lien est encore valide et que `sub` est bien le
    titulaire désigné. Le jeton est consommé (usage unique). None sinon."""
    with _connect() as conn:
        r = conn.execute(
            "UPDATE org_erasures SET state = 'scheduled', confirmed_at = NOW(), "
            "  scheduled_for = NOW() + %s, token_hash = NULL, snapshot = %s, plan = %s "
            "WHERE id = %s AND state = 'requested' AND confirm_sub = %s "
            "  AND token_expires_at > NOW() "
            f"RETURNING {_COLONNES_PUBLIQUES}",
            (DELAI_DE_GRACE, Jsonb(snapshot), Jsonb(plan), id_, sub)).fetchone()
    return dict(r) if r else None


def annuler(id_: int, *, by: str) -> Optional[dict]:
    """`requested` | `scheduled` → `cancelled`. Ce qui était personnel est retiré."""
    with _connect() as conn:
        r = conn.execute(
            "UPDATE org_erasures SET state = 'cancelled', cancelled_by = %s, "
            "  cancelled_at = NOW(), token_hash = NULL, confirm_email = NULL, snapshot = NULL "
            "WHERE id = %s AND state IN ('requested', 'scheduled') "
            f"RETURNING {_COLONNES_PUBLIQUES}", (by, id_)).fetchone()
    return dict(r) if r else None


def expirer() -> int:
    with _connect() as conn:
        return conn.execute(
            "UPDATE org_erasures SET state = 'expired', token_hash = NULL, "
            "  confirm_email = NULL "
            "WHERE state = 'requested' AND token_expires_at <= NOW()").rowcount


def dues() -> list[dict]:
    """Les demandes à faire avancer : échues, ou en cours (reprise)."""
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {_COLONNES_PUBLIQUES}, snapshot FROM org_erasures "
            "WHERE (state = 'scheduled' AND scheduled_for <= NOW()) OR state = 'running' "
            "ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def demarrer(id_: int) -> bool:
    with _connect() as conn:
        return conn.execute(
            "UPDATE org_erasures SET state = 'running', "
            "  started_at = COALESCE(started_at, NOW()) "
            "WHERE id = %s AND (state = 'running' OR (state = 'scheduled' "
            "  AND scheduled_for <= NOW()))", (id_,)).rowcount == 1


def noter(id_: int, recu: dict) -> None:
    """Fusionne `recu` dans le reçu (étapes faites, comptes)."""
    with _connect() as conn:
        conn.execute("UPDATE org_erasures SET receipt = COALESCE(receipt, '{}'::jsonb) || %s "
                     "WHERE id = %s", (Jsonb(recu), id_))


def maj_snapshot(id_: int, snapshot: dict) -> None:
    with _connect() as conn:
        conn.execute("UPDATE org_erasures SET snapshot = %s WHERE id = %s AND state = 'running'",
                     (Jsonb(snapshot), id_))


def echec(id_: int, erreur: str) -> str:
    """Note l'échec d'un passage ; rend l'état (`running`, ou `failed` au-delà de
    `ESSAIS_MAX`)."""
    with _connect() as conn:
        r = conn.execute(
            "UPDATE org_erasures SET last_error = %s, attempts = attempts + 1, "
            "  state = CASE WHEN attempts + 1 >= %s THEN 'failed' ELSE state END "
            "WHERE id = %s RETURNING state", (erreur[:2000], ESSAIS_MAX, id_)).fetchone()
    return r["state"] if r else "unknown"


def terminer(id_: int, recu: dict) -> None:
    """`running` → `done`. Ce qui était personnel est retiré de la demande : le nom de
    l'org, l'adresse et le compte du titulaire, les comptes du périmètre (seuls leurs
    nombres restent, dans le plan et le reçu)."""
    with _connect() as conn:
        conn.execute(
            "UPDATE org_erasures SET state = 'done', finished_at = NOW(), last_error = NULL, "
            "  receipt = COALESCE(receipt, '{}'::jsonb) || %s, org_name = NULL, "
            "  confirm_email = NULL, confirm_sub = NULL, snapshot = NULL, token_hash = NULL "
            "WHERE id = %s AND state = 'running'", (Jsonb(recu), id_))


def commerce_a_efface(id_: int) -> bool:
    with _connect() as conn:
        return conn.execute(
            "UPDATE org_erasures SET commerce_erased_at = COALESCE(commerce_erased_at, NOW()) "
            "WHERE id = %s AND state = 'done'", (id_,)).rowcount == 1


def pour_le_commerce(limite: int = 100) -> list[dict]:
    """Ce que le commerce doit traiter, tant qu'il ne l'a pas acquitté : une org
    confirmée (`scheduled`, `running` : il cesse de facturer, résilie), une org effacée
    (`done` : il efface sa part, puis acquitte — `commerce_a_efface`)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, org_id, state, scheduled_for, finished_at FROM org_erasures "
            "WHERE state IN ('scheduled', 'running', 'done') AND commerce_erased_at IS NULL "
            "ORDER BY id LIMIT %s", (limite,)).fetchall()
    return [dict(r) for r in rows]
