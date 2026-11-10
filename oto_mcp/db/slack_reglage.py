"""The org's Slack setting — column `orgs.slack_mention_only`.

**Mention only** (`oto_org_settings domain=slack`, `slack_events.py`): outside a DM, the
app answers only a message that tags it (`@<app>`). Off by default — the state of every
org before it: a reply in the thread of an agent's message restarts that agent, and the
same person's reply in a thread of the in-app agent's carries on their conversation,
without a mention (decision of 29/10/2026). An org admin turns it on.

Read by the event route BEFORE any credential (`slack_events.traiter`): the org is
known from the thread itself — the agent's noted message, or the in-app agent's
conversation —, never from the workspace's install.

The column is born in `CREATE TABLE orgs` (`db/schema/orgs.py`); an existing base gets
it from the revision `20261109_2100_slack_mention_only` or from the boot, same DDL
(`DDL_COLONNE`), in either order.

Not flattened into `db.*` — callers write `db.slack_reglage.<fn>`.

⚠️ Synchronous (psycopg): an async caller goes through `run_in_threadpool`.
"""
from __future__ import annotations

from typing import Optional

from ._conn import _connect

COLONNE = "slack_mention_only"
DDL_COLONNE = (f"ALTER TABLE orgs ADD COLUMN IF NOT EXISTS {COLONNE} "
               "BOOLEAN NOT NULL DEFAULT FALSE")


def mention_seule(org_id: int) -> Optional[bool]:
    """Does the org's Slack app answer, outside a DM, only a message that tags it? None
    when the org no longer exists — a message noted by an agent outlives its org until
    the purge (`slack_agent_posts`, no foreign key): nobody is left to answer."""
    with _connect() as conn:
        row = conn.execute(f"SELECT {COLONNE} AS v FROM orgs WHERE id = %s",
                           (int(org_id),)).fetchone()
    return None if row is None else bool(row["v"])


def poser_mention_seule(org_id: int, valeur: bool) -> bool:
    """Sets the org's "mention only". False if the org does not exist."""
    with _connect() as conn:
        row = conn.execute(f"UPDATE orgs SET {COLONNE} = %s WHERE id = %s RETURNING id",
                           (bool(valeur), int(org_id))).fetchone()
    return row is not None
