#!/usr/bin/env python3
"""Retire les orgs perso existantes : l'étiquette tombe, les vides s'archivent.

Plus aucune org perso ne naît depuis le lot « plus d'org perso » (05/11/2026,
`org_store/personal.py`). Celles qui existent gardent `orgs.personal_of` tant que ce
geste n'est pas joué. Il fait, dans cet ordre :

1. **ranger** — un projet ou un tableau perso SANS org de création
   (`context_org_id` nul) se listait dans l'org perso de son propriétaire. Il y est
   rangé (`context_org_id` = cette org), pour qu'il reste listé là où on le voyait ;
2. **marquer** — chaque org perso devient une org posée par l'inscription
   (`auto_created = TRUE`) — la révision `20261105_1700_orgs_auto_created` l'a déjà
   fait, ce pas le rejoue (idempotent) : tant que son propriétaire y est seul, elle
   cède la place d'org active à une org rejointe, et reste hors quota, comme avant ;
3. **archiver** (option `--archiver`) les orgs perso VIDES : seul membre, ni projet
   (hors « Découverte »), ni tableau, ni procédure, ni clé, ni compte connecté — et dont
   le propriétaire a au moins une AUTRE org vivante (sans elle, l'archiver lui en
   reposerait aussitôt une neuve) ;
4. **vider l'étiquette** — `personal_of = NULL` partout.

⚠️ Lancé À LA MAIN, APRÈS le tag du lot et `oto-mcp migrer upgrade head`. **À blanc par
défaut** : sans `--appliquer`, il ne fait QUE lire, et n'imprime que des NOMBRES —
aucun nom, email, sub ni id d'org. Chaque requête est bornée (`statement_timeout`
15 s, `lock_timeout` 5 s) : une base trop chargée fait échouer le script, jamais
attendre la production. Idempotent.

    ./.venv/bin/python -m scripts.retirer_orgs_perso                 # rapport à blanc
    ./.venv/bin/python -m scripts.retirer_orgs_perso --appliquer     # 1, 2, 4
    ./.venv/bin/python -m scripts.retirer_orgs_perso --appliquer --archiver   # + 3

Compteurs (une ligne `clé: nombre` chacun) :

- `orgs_perso_vivantes` — orgs perso non archivées (`personal_of` renseigné) ;
- `orgs_perso_marquees` — parmi elles, déjà `auto_created` (= `orgs_perso_vivantes`
  si la révision a été jouée) ;
- `orgs_perso_peuplees` — parmi elles, avec au moins un AUTRE membre que leur
  propriétaire : ce sont des orgs d'équipe, elles ne cèdent plus la place d'org active ;
- `orgs_perso_actives` — comptes dont l'org active est leur org perso (leur maison ne
  bouge pas : c'est déjà l'org active) ;
- `projets_a_ranger` / `tableaux_a_ranger` — objets perso sans org de création, que
  le pas 1 range dans l'org perso de leur propriétaire ;
- `orgs_perso_vides_archivables` — ce que `--archiver` archiverait.
"""
from __future__ import annotations

import argparse
import sys
from typing import Optional

from oto_mcp import discovery, org_store
from oto_mcp.db._conn import _connect_autocommit

_BORNES = ("SET statement_timeout = '15s'", "SET lock_timeout = '5s'")

# Une org perso VIDE, archivable sans rien perdre ni laisser son propriétaire sans org.
_VIDES = """
SELECT o.id FROM orgs o
 WHERE o.personal_of IS NOT NULL AND o.archived_at IS NULL
   AND NOT EXISTS (SELECT 1 FROM org_members m WHERE m.org_id = o.id AND m.sub <> o.personal_of)
   AND EXISTS (SELECT 1 FROM org_members m JOIN orgs o2 ON o2.id = m.org_id
                WHERE m.sub = o.personal_of AND o2.id <> o.id AND o2.archived_at IS NULL)
   AND NOT EXISTS (SELECT 1 FROM projects p
                    WHERE ((p.owner_type = 'org' AND p.owner_id = o.id::text)
                           OR p.context_org_id = o.id
                           OR (p.owner_type = 'user' AND p.owner_id = o.personal_of
                               AND p.context_org_id IS NULL))
                      AND p.name <> %(decouverte)s AND p.archived_at IS NULL)
   AND NOT EXISTS (SELECT 1 FROM user_datastores d
                    WHERE (d.owner_type = 'org' AND d.owner_id = o.id::text)
                       OR d.context_org_id = o.id
                       OR (d.owner_type = 'user' AND d.owner_id = o.personal_of
                           AND d.context_org_id IS NULL))
   AND NOT EXISTS (SELECT 1 FROM org_instructions i
                    WHERE i.owner_type = 'org' AND i.owner_id = o.id::text)
   AND NOT EXISTS (SELECT 1 FROM connector_credentials c
                    WHERE (c.entity_type = 'org' AND c.entity_id = o.id::text)
                       OR (c.entity_type = 'member' AND c.entity_id LIKE o.id::text || ':%%'))
   AND NOT EXISTS (SELECT 1 FROM unipile_accounts a WHERE a.org_id = o.id)
 ORDER BY o.id
"""

_PERSO = "FROM orgs o WHERE o.personal_of IS NOT NULL AND o.archived_at IS NULL"

# Les compteurs du rapport : chacun UN `count(*)`, rien de nominatif ne sort.
_COMPTEURS = {
    "orgs_perso_vivantes": f"SELECT count(*) AS n {_PERSO}",
    "orgs_perso_marquees": f"SELECT count(*) AS n {_PERSO} AND o.auto_created",
    "orgs_perso_peuplees": (
        f"SELECT count(*) AS n {_PERSO} AND EXISTS (SELECT 1 FROM org_members m "
        " WHERE m.org_id = o.id AND m.sub <> o.personal_of)"),
    "orgs_perso_actives": (
        f"SELECT count(*) AS n {_PERSO} AND EXISTS (SELECT 1 FROM org_members m "
        " WHERE m.org_id = o.id AND m.sub = o.personal_of AND m.is_active)"),
    "projets_a_ranger": (
        "SELECT count(*) AS n FROM projects p JOIN orgs o ON o.personal_of = p.owner_id "
        " WHERE p.owner_type = 'user' AND p.context_org_id IS NULL AND o.archived_at IS NULL"),
    "tableaux_a_ranger": (
        "SELECT count(*) AS n FROM user_datastores d JOIN orgs o ON o.personal_of = d.owner_id "
        " WHERE d.owner_type = 'user' AND d.context_org_id IS NULL AND o.archived_at IS NULL"),
}


def _borner(conn) -> None:
    for s in _BORNES:
        conn.execute(s)


def _vides(conn) -> list[int]:
    return [int(r["id"]) for r in
            conn.execute(_VIDES, {"decouverte": discovery.PROJECT_NAME}).fetchall()]


def rapport(conn) -> dict[str, int]:
    """Les compteurs, en lecture seule — des nombres, jamais une ligne nominative."""
    out = {k: int(conn.execute(q).fetchone()["n"]) for k, q in _COMPTEURS.items()}
    out["orgs_perso_vides_archivables"] = len(_vides(conn))
    return out


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--appliquer", action="store_true", help="écrire (sinon : à blanc)")
    p.add_argument("--archiver", action="store_true", help="archiver aussi les orgs perso vides")
    a = p.parse_args(argv)

    with _connect_autocommit() as conn:
        _borner(conn)
        if not a.appliquer:
            conn.execute("SET default_transaction_read_only = on")
        for k, n in rapport(conn).items():
            print(f"{k}: {n}")
        vides = _vides(conn) if a.archiver else []
    if not a.appliquer:
        print("\nÀ blanc : rien écrit. --appliquer pour écrire.")
        return 0

    with _connect_autocommit() as conn:
        _borner(conn)
        n1 = conn.execute(
            "UPDATE projects p SET context_org_id = o.id FROM orgs o "
            " WHERE o.personal_of = p.owner_id AND o.archived_at IS NULL "
            "   AND p.owner_type = 'user' AND p.context_org_id IS NULL").rowcount
        n2 = conn.execute(
            "UPDATE user_datastores d SET context_org_id = o.id FROM orgs o "
            " WHERE o.personal_of = d.owner_id AND o.archived_at IS NULL "
            "   AND d.owner_type = 'user' AND d.context_org_id IS NULL").rowcount
        n3 = conn.execute(
            "UPDATE orgs SET auto_created = TRUE "
            " WHERE personal_of IS NOT NULL AND NOT auto_created").rowcount
    print(f"\nprojets_ranges: {n1}\ntableaux_ranges: {n2}\norgs_marquees: {n3}")

    archivees = gardees = 0
    for oid in vides:
        try:
            if org_store.archive_org(oid):
                archivees += 1
        except Exception:  # abonnement actif, etc. : on la garde, on la compte
            gardees += 1

    with _connect_autocommit() as conn:
        _borner(conn)
        n4 = conn.execute("UPDATE orgs SET personal_of = NULL WHERE personal_of IS NOT NULL").rowcount
    print(f"orgs_archivees: {archivees}\norgs_gardees_sur_refus: {gardees}\n"
          f"etiquettes_videes: {n4}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
