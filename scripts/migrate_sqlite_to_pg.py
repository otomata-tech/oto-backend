"""One-shot migration : SQLite (`OTO_MCP_DB_PATH`) → PostgreSQL (`DATABASE_URL`).

Préserve les `id` des tables à SERIAL (platform_keys, user_datastores,
user_api_tokens) pour que les FKs et les références externes restent valides,
puis resync les sequences PG sur `MAX(id) + 1`.

Run depuis le serveur applicatif (tuls.me) où les deux DBs sont accessibles :

    cd /opt/oto-mcp
    set -a; . .env; set +a
    OTO_MCP_DB_PATH=/opt/oto-mcp/data/oto-mcp.sqlite \\
      ./.venv/bin/python -m scripts.migrate_sqlite_to_pg
"""
from __future__ import annotations

import os
import sqlite3
import sys

from oto_mcp import db as pgdb


SQLITE_PATH_DEFAULT = "/opt/oto-mcp/data/oto-mcp.sqlite"


def _sqlite_conn(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


# Ordre respectant les FKs (platform_keys avant user_grants ; users avant user_api_tokens).
# `sequence` = nom de la sequence PG à resync sur MAX(id)+1 après insert (None si pas de SERIAL).
TABLES = [
    ("users", [
        "sub", "email", "name", "role",
        "linkedin_cookie", "linkedin_user_agent", "linkedin_cookie_set_at",
        "serper_api_key", "hunter_api_key", "sirene_api_key",
        "attio_api_key", "lemlist_api_key",
        "crunchbase_cookies", "crunchbase_user_agent", "crunchbase_set_at",
        "created_at", "updated_at",
    ], None),
    ("platform_keys", [
        "id", "provider", "label", "api_key", "created_at",
    ], "platform_keys_id_seq"),
    ("user_grants", [
        "sub", "platform_key_id", "granted_at", "granted_by",
    ], None),
    ("user_google_oauth", [
        "sub", "refresh_token", "access_token", "expires_at",
        "scopes", "granted_at", "updated_at",
    ], None),
    ("user_datastores", [
        "id", "sub", "namespace", "spreadsheet_id", "created_at",
    ], "user_datastores_id_seq"),
    ("user_api_tokens", [
        "id", "sub", "label", "token_hash", "created_at", "last_used_at",
    ], "user_api_tokens_id_seq"),
    ("user_disabled_tools", [
        "sub", "tool_name", "disabled_at",
    ], None),
    ("usage", [
        "sub", "tool", "day", "count",
    ], None),
]


def migrate(sqlite_path: str) -> None:
    if not os.path.exists(sqlite_path):
        print(f"SQLite source not found: {sqlite_path}", file=sys.stderr)
        sys.exit(1)

    print(f"→ source SQLite : {sqlite_path}")
    print("→ init PG schema")
    pgdb.init_db()

    src = _sqlite_conn(sqlite_path)

    with pgdb._connect() as pg:
        for table, cols, seq in TABLES:
            rows = src.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall()
            n = len(rows)
            if n == 0:
                print(f"  - {table}: 0 rows (skip)")
                continue
            placeholders = ", ".join(["%s"] * len(cols))
            col_list = ", ".join(cols)
            sql = (
                f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
                f"ON CONFLICT DO NOTHING"
            )
            inserted = 0
            for r in rows:
                pg.execute(sql, tuple(r[c] for c in cols))
                inserted += 1
            print(f"  - {table}: {inserted}/{n} rows inserted")

            if seq:
                # Resync sequence sur max(id)+1
                pg.execute(
                    f"SELECT setval('{seq}', COALESCE((SELECT MAX(id) FROM {table}), 0) + 1, false)"
                )
                print(f"    sequence {seq} resynced")

    # Verify counts
    print("\n→ verify counts (sqlite / pg)")
    with pgdb._connect() as pg:
        for table, _, _ in TABLES:
            sl = src.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            pgn = pg.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            tag = "OK" if sl == pgn else "DIVERGED"
            print(f"  {tag:8s}  {table:25s}  sqlite={sl:6d}  pg={pgn:6d}")

    src.close()
    print("\n✓ migration done")


if __name__ == "__main__":
    path = os.environ.get("OTO_MCP_DB_PATH", SQLITE_PATH_DEFAULT)
    migrate(path)
