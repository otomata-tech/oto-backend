#!/usr/bin/env python3
"""Rattrape les totaux du journal par jour UTC sur l'historique (oto-backend#1147).

La maintenance quotidienne (`oto-mcp maintenance journal-jour`) ne consolide que la
veille et les quelques jours clos qui suivent le dernier consolidé. L'historique du
journal — au déploiement, ~72 jours et ~12 M lignes — se rattrape ICI, à la main, une
fois, hors pointe : les lecteurs des totaux refusent une fenêtre qui commence avant le
premier jour consolidé tant que le journal y a encore des lignes.

⚠️ **La base est PARTAGÉE entre préproduction et production**, et c'est une nano de
4 Go. Le rattrapage avance donc UN JOUR À LA FOIS : une transaction courte par jour
(`db/journal_jour.consolider_jour` — agrégation par tri, `statement_timeout` par jour),
une pause entre deux jours. **À blanc par défaut** : sans `--appliquer`, il liste les
jours qu'il consoliderait et n'écrit rien.

Ordre : les jours après le dernier consolidé d'abord (en avançant), puis ceux d'avant le
premier (en reculant, du plus récent au plus ancien) — la couverture reste CONTIGUË à
chaque instant, un arrêt ne laisse aucun trou. **Reprise idempotente** : un jour déjà
consolidé est sauté (sauf `--refaire`), donc relancer la même commande reprend où le
précédent passage s'est arrêté. Un jour qui dépasse sa borne ARRÊTE le passage (le
suivant creuserait un trou) : il se relance seul avec une borne plus large
(`--du J --au J --duree-max 300`).

    # sur la box, par le lanceur (docs/commands.md §Un script d'entretien par le lanceur)
    lanceur --script scripts/rattraper_journal_jour.py                 # à blanc
    lanceur --script scripts/rattraper_journal_jour.py --appliquer     # écrit
    # options : --du AAAA-MM-JJ --au AAAA-MM-JJ --pause 3 --duree-max 120 --refaire

Contrôle : un second passage à blanc annonce 0 jour ; `--etat` imprime le registre
(premier et dernier jour consolidés, trous — 0 attendu).
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from oto_mcp.db import journal_jour


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="rattraper_journal_jour",
                                description=__doc__.split("\n\n")[0])
    p.add_argument("--appliquer", action="store_true", help="écrit (à blanc sinon)")
    p.add_argument("--du", help="premier jour (AAAA-MM-JJ), défaut : le premier du journal")
    p.add_argument("--au", help="dernier jour (AAAA-MM-JJ), défaut et plafond : la veille UTC")
    p.add_argument("--pause", type=float, default=3.0,
                   help="secondes de pause entre deux jours (défaut 3)")
    p.add_argument("--duree-max", type=int, default=journal_jour.DUREE_MAX_MS // 1000,
                   help="borne d'un jour, en secondes (défaut %(default)s)")
    p.add_argument("--refaire", action="store_true",
                   help="reprend aussi les jours déjà consolidés")
    p.add_argument("--etat", action="store_true", help="imprime le registre et sort")
    args = p.parse_args(argv)

    if args.etat:
        print(json.dumps(journal_jour.etat(), default=str))
        return 0
    jours = journal_jour.jours_a_consolider(du=args.du, au=args.au, refaire=args.refaire)
    print(f"{len(jours)} jour(s) à consolider"
          + (f" : de {jours[0]} à {jours[-1]}" if jours else ""), flush=True)
    if not args.appliquer:
        for j in jours:
            print(f"  {j}")
        print("À BLANC — rien n'est écrit. Relancer avec --appliquer.")
        return 0
    total = 0
    debut = time.monotonic()
    for i, jour in enumerate(jours):
        t = time.monotonic()
        try:
            r = journal_jour.consolider_jour(jour, duree_max_ms=args.duree_max * 1000)
        except Exception as e:
            print(f"ÉCHEC sur {jour} ({type(e).__name__}: {e}) — passage ARRÊTÉ pour ne "
                  f"pas creuser de trou. Relancer la même commande reprend ici ; un jour "
                  f"trop lourd se relance seul : --du {jour} --au {jour} --duree-max 300",
                  file=sys.stderr, flush=True)
            return 1
        total += r["lignes"]
        print(f"[{i + 1}/{len(jours)}] {jour} : {r['lignes']} lignes, {r['totaux']} totaux, "
              f"{r['jobs']} jobs, {time.monotonic() - t:.1f} s", flush=True)
        if i + 1 < len(jours):
            time.sleep(args.pause)
    print(f"fini : {len(jours)} jour(s), {total} lignes, {time.monotonic() - debut:.0f} s ; "
          f"registre : {json.dumps(journal_jour.etat(), default=str)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
