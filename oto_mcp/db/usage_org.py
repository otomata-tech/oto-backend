"""The day's counter of an ORG on a platform key whose quota counts per org.

One row per (org, key carrier, day) in `usage_org`. Which connector counts per org is
declared in the registry (`Connector.grant_quota_per`); the refusal reads this counter
(`access.quotas.platform_used_today`) and the settlement of a call adds to it
(`access.quotas.debiter_plateforme`). This module only counts.
"""
from __future__ import annotations

from ._conn import _connect


def ajouter(org_id: int, tool: str, amount: int) -> int:
    """Adds `amount` to the day's counter of `org_id` on the key `tool`, and returns the
    new total.

    One statement, atomic: the upsert holds the row while it adds, so two calls of the
    same org settled at the same time lose nothing. The day is the DATABASE's
    (`CURRENT_DATE`), the same for every process and the same as `usage`'s."""
    if int(amount) <= 0:
        raise ValueError(f"usage_org: a debit is a positive amount, not {amount!r}")
    with _connect() as conn:
        row = conn.execute(
            """
            INSERT INTO usage_org (org_id, tool, day, count)
            VALUES (%s, %s, CURRENT_DATE, %s)
            ON CONFLICT (org_id, tool, day)
            DO UPDATE SET count = usage_org.count + EXCLUDED.count
            RETURNING count
            """,
            (int(org_id), tool, int(amount))).fetchone()
    return int(row["count"]) if row else 0


def du_jour(org_id: int, tool: str) -> int:
    """The day's counter of `org_id` on the key `tool` (0 without a row)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT count FROM usage_org WHERE org_id = %s AND tool = %s "
            "AND day = CURRENT_DATE",
            (int(org_id), tool)).fetchone()
    return int(row["count"]) if row else 0
