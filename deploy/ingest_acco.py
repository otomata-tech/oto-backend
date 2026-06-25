#!/usr/bin/env python3
r"""Ingestion ACCO → table PG `acco` (accords d'entreprise, base nationale DILA).

On lit le dump XML DILA (echanges.dila.gouv.fr/OPENDATA/ACCO/) via la lib
france-opendata (parser durci defusedxml) et on upsert dans la table PG `acco` de
oto_mcp — par batches, mémoire minuscule (safe box 2 Go), idempotent (ON CONFLICT id).

Le dump se présente en archives tar.gz : un **stock global** (~45 Go, depuis 2017 —
streamé, jamais sur disque, seuls les XML métadonnées sont extraits) + des
**incréments hebdo** (~80 Mo, ~12 mois glissants en ligne).

À lancer sur la box (env de process = DATABASE_URL via .env), depuis /opt/oto-mcp :
    .venv/bin/python deploy/ingest_acco.py --bootstrap          # stock + tous les hebdo
    .venv/bin/python deploy/ingest_acco.py --since 2026-06-15   # incrémental (cron)

Cron léger conseillé (hebdo) sur la box :
    40 5 * * 1 cd /opt/oto-mcp && set -a; . .env; set +a; \
        .venv/bin/python deploy/ingest_acco.py --since "$(date -d '14 days ago' +\%F)" \
        >> /var/log/oto-mcp/acco-ingest.log 2>&1

Nécessite `france-opendata[stock]` (defusedxml) dans le venv.
"""
from __future__ import annotations

import argparse
import sys

from france_opendata import acco_ingest

from oto_mcp import db

BATCH = 2000


def _ingest_rows(rows_iter, label: str) -> int:
    """Upsert un itérateur de lignes par batches. Retourne le total."""
    batch, total = [], 0
    for row in rows_iter:
        batch.append(row)
        if len(batch) >= BATCH:
            total += db.upsert_acco(batch)
            batch = []
            print(f"[acco] {label}: cumul {total}", flush=True)
    if batch:
        total += db.upsert_acco(batch)
    print(f"[acco] {label}: terminé ({total} lignes)", flush=True)
    return total


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Ingestion ACCO → PG")
    p.add_argument("--bootstrap", action="store_true",
                   help="stock global complet (depuis 2017) puis tous les hebdo en ligne")
    p.add_argument("--since", help="incrémental : archives hebdo de date >= YYYY-MM-DD")
    args = p.parse_args(argv)

    if not args.bootstrap and not args.since:
        p.error("préciser --bootstrap (plein) ou --since YYYY-MM-DD (incrémental)")

    db.init_db()  # idempotent : crée la table acco si le déploiement ne l'a pas fait
    sess = acco_ingest._session()
    total = 0

    if args.bootstrap:
        print("[acco] bootstrap : stream du stock global (~45 Go, XML seuls) …", flush=True)
        total += _ingest_rows(
            acco_ingest.rows_from_archive(
                f"{acco_ingest.BASE_URL}/{acco_ingest.GLOBAL_NAME}", sess=sess),
            "global",
        )

    since = args.since  # bootstrap : prend aussi tous les hebdo en ligne (since=None)
    urls = acco_ingest.list_weekly_archives(sess, since=since)
    print(f"[acco] {len(urls)} archives hebdo (since={since}) …", flush=True)
    for url in urls:
        name = url.rsplit("/", 1)[-1]
        total += _ingest_rows(acco_ingest.rows_from_archive(url, sess=sess), name)

    info = db.acco_info()
    print(f"[acco] OK — table : {info}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
