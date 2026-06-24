#!/usr/bin/env python3
r"""Ingestion BOAMP → table PG `boamp` (france-opendata#3).

OpenDataSoft (boamp-datadila) est bloqué depuis les IP datacenter. On crawle le dump
XML brut DILA (echanges.dila.gouv.fr, joignable) via la lib france-opendata, et on
upsert dans la table PG `boamp` de oto_mcp — par batches jour-après-jour, donc mémoire
minuscule (safe sur la box 2 Go) et idempotent (ON CONFLICT idweb).

À lancer sur la box (env de process = DATABASE_URL via .env), depuis /opt/oto-mcp :
    .venv/bin/python deploy/ingest_boamp.py --years 2025 2026      # backfill complet
    .venv/bin/python deploy/ingest_boamp.py --since 2026-06-18     # incrémental (cron)

Cron léger conseillé (quotidien, fenêtre courte) sur la box :
    30 5 * * * cd /opt/oto-mcp && set -a; . .env; set +a; \
        .venv/bin/python deploy/ingest_boamp.py --since "$(date -d '10 days ago' +\%F)" \
        >> /var/log/oto-mcp/boamp-ingest.log 2>&1
"""
from __future__ import annotations

import argparse
import datetime as _dt
import sys

from france_opendata import boamp_ingest

from oto_mcp import db


def _chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Ingestion BOAMP → PG")
    p.add_argument("--years", type=int, nargs="*", help="années (défaut : 2 ans glissants)")
    p.add_argument("--since", help="borne basse YYYY-MM-DD (incrémental)")
    p.add_argument("--batch", type=int, default=400, help="taille de batch d'upsert")
    p.add_argument("--max-workers", type=int, default=24)
    args = p.parse_args(argv)

    years = args.years
    if not years:
        y = _dt.date.fromisoformat(args.since).year if args.since else _dt.date.today().year
        years = sorted({y, y - 1})

    db.init_db()  # idempotent : crée la table boamp si le déploiement ne l'a pas encore fait

    sess = boamp_ingest._session()
    print(f"[boamp] crawl années={years} since={args.since} …", flush=True)
    urls = boamp_ingest.iter_avis_urls(sess, years, since=args.since)
    print(f"[boamp] {len(urls)} avis à ingérer", flush=True)

    total = 0
    for n, chunk in enumerate(_chunks(urls, args.batch), 1):
        rows = boamp_ingest.fetch_rows(chunk, max_workers=args.max_workers)
        total += db.upsert_boamp(rows)
        print(f"[boamp] batch {n} : +{len(rows)} (cumul {total})", flush=True)

    info = db.boamp_info()
    print(f"[boamp] terminé. table: {info}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
