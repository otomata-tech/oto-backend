"""Backfill datastore Sheets → PG (ADR 0016).

One-shot : copie les rows de chaque Google Sheet (substrat legacy) dans la table
`datastore_rows` (substrat natif). Idempotent (`ON CONFLICT DO NOTHING` sur
`(ns_id, row_id)`) — re-jouable sans doublon. Préserve `_id`/`_created_at`/
`_updated_at`.

À lancer **sur la box**, après déploiement du code PG (la table existe via
init_db) — la fenêtre datastore-vide entre le restart et ce run est brève :

    ssh -i ~/.ssh/alexis root@<box> \
      "cd /opt/oto-mcp && ./.venv/bin/python -m scripts.migrate_datastore_to_pg"

Le code de lecture Sheets vit ICI (auto-suffisant) : `oto_mcp/datastore.py` ne
le porte plus (cutover). `--dry-run` pour compter sans écrire.
"""
from __future__ import annotations

import json
import sys

from oto_mcp import db, google_oauth


_TYPE_PREFIX = "__j:"  # sentinelle de typage de l'ère Sheets


def _deserialize(v: str):
    if v == "":
        return None
    if isinstance(v, str) and v.startswith(_TYPE_PREFIX):
        try:
            return json.loads(v[len(_TYPE_PREFIX):])
        except Exception:
            return v
    return v


def _read_sheet_rows(sub: str, account: str | None, spreadsheet_id: str) -> list[dict]:
    """Lit toutes les rows d'un Sheet (range `data`) → liste de dicts (headers
    désérialisés). Renvoie [] si le sheet est vide/inaccessible."""
    from googleapiclient.discovery import build

    creds = google_oauth.credentials_for(sub, account=account)
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
    result = sheets.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range="data",
        valueRenderOption="FORMATTED_VALUE",
    ).execute()
    rows = result.get("values", [])
    if not rows:
        return []
    headers = rows[0]
    out: list[dict] = []
    for raw in rows[1:]:
        padded = raw + [""] * (len(headers) - len(raw))
        rec = {h: _deserialize(padded[i]) for i, h in enumerate(headers)}
        if rec.get("_id") in (None, ""):
            continue  # ligne vide
        out.append(rec)
    return out


def _insert(ns_id: int, rec: dict) -> bool:
    """INSERT idempotent d'une row legacy. data = champs user (hors méta).
    Renvoie True si une ligne a été insérée."""
    row_id = rec["_id"]
    created = rec.get("_created_at")
    updated = rec.get("_updated_at")
    data = {k: v for k, v in rec.items() if k not in ("_id", "_created_at", "_updated_at")}
    with db._connect() as conn:
        cur = conn.execute(
            "INSERT INTO datastore_rows (ns_id, row_id, data, created_at, updated_at) "
            "VALUES (%s, %s, %s::jsonb, COALESCE(%s::timestamptz, NOW()), COALESCE(%s::timestamptz, NOW())) "
            "ON CONFLICT (ns_id, row_id) DO NOTHING",
            (ns_id, row_id, json.dumps(data), created, updated),
        )
        return (cur.rowcount or 0) > 0


def main() -> None:
    dry = "--dry-run" in sys.argv
    db.init_db()
    with db._connect() as conn:
        namespaces = conn.execute(
            "SELECT id, sub, namespace, spreadsheet_id, owner_email FROM user_datastores "
            "WHERE spreadsheet_id IS NOT NULL ORDER BY id"
        ).fetchall()

    total_ns = total_rows = total_inserted = 0
    for ns in namespaces:
        total_ns += 1
        try:
            recs = _read_sheet_rows(ns["sub"], ns.get("owner_email"), ns["spreadsheet_id"])
        except Exception as e:
            print(f"  ! {ns['sub']}/{ns['namespace']} (sheet {ns['spreadsheet_id']}): {e}",
                  file=sys.stderr)
            continue
        inserted = 0
        for rec in recs:
            total_rows += 1
            if dry:
                continue
            if _insert(ns["id"], rec):
                inserted += 1
        total_inserted += inserted
        print(f"  {ns['sub']}/{ns['namespace']}: {len(recs)} rows lues, {inserted} insérées"
              + (" (dry-run)" if dry else ""))

    print(f"\n{total_ns} namespaces · {total_rows} rows lues · {total_inserted} insérées"
          + (" (dry-run, rien écrit)" if dry else ""))


if __name__ == "__main__":
    main()
