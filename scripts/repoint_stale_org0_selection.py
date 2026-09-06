"""Rapatrie les sélections de connecteur restées sous l'ancien sentinelle
`org_id=0` (ADR 0015, avant la suppression du « perso sans org », ADR 0030 §8)
vers l'org maison RÉELLE du membre — oto-backend#868.

Ces lignes sont aujourd'hui INVISIBLES : `select`/`pause`/`unselect` et la
lecture `connectors.me` lisent et écrivent tous sous `ctx.org_id or 0`, l'org
ACTIVE au moment de l'appel — jamais `0` pour un membre qui a une org réelle
(`access.current_org` ne rend `0` que pour un espace vraiment sans org, un
régime disparu depuis ADR 0030 §8). Mesuré le 2026-09-04 : 28 comptes réels
(hors comptes de démo), 53 à 61 lignes chacun ; vérifié en exécutant le
handler réellement servi (`connectors.me`, pas une relecture de la base) sur
l'un d'eux — il rend `0` connecteur sélectionné alors que 60 lignes existent
en base sous `org_id=0`.

**27 des 28 comptes portent DÉJÀ au moins une sélection sous leur org réelle**
(ré-installée depuis, à la main) : la migration est donc une FUSION dans
presque tous les cas, pas un simple repointage.

Politique de fusion — **corrigée le 2026-09-04** (relevée par le superviseur
avant le go d'Alexis) : quand le connecteur est déjà sélectionné sous l'org
réelle, **c'est son état à LUI qui gagne, toujours** — la ligne `org_id=0` est
jetée sans jamais toucher l'état réel. La première version reprenait la
politique de `connector_selection.rename_selection` (« le plus permissif
gagne », état ACTIVE prioritaire) : ça convient à un renommage de connecteur
(même personne, même intention, un split n'est pas une occasion de
désinstaller), mais PAS ici — le legacy `org_id=0` n'est pas une intention
récente, c'est un fantôme invisible depuis potentiellement des mois. Une
pause posée sciemment sous l'org réelle est le geste le plus RÉCENT et le
plus délibéré des deux ; un connecteur relevé en `active` par un fantôme
qu'on ne pouvait même pas voir serait réveillé sans qu'on l'ait demandé. Sinon
(rien sous l'org réelle) : la ligne `org_id=0` est simplement repointée, avec
son état inchangé — il n'y a alors rien avec quoi arbitrer.

    ssh -i ~/.ssh/<clé> root@<box> \\
      "cd /opt/oto-mcp && ./.venv/bin/python -m scripts.repoint_stale_org0_selection"
    #   ^ dry-run par défaut : REJOUE les écritures dans une transaction, puis
    #     l'ANNULE (rollback) — ce n'est pas un simple comptage, une contrainte
    #     ou une PK qui casserait à l'exécution réelle casse déjà ici.
    #     --apply pour committer.
"""
from __future__ import annotations

import sys

from oto_mcp.db import _connect


def _candidates(conn) -> list[dict]:
    """(sub, org réelle) des membres qui ont encore une sélection sous `org_id=0`
    ET une org maison réelle — exclut les comptes de démonstration (préfixe
    `demo_`, jamais un sub Logto réel)."""
    rows = conn.execute(
        """
        SELECT DISTINCT usc.sub, m.org_id AS home
          FROM user_selected_connectors usc
          JOIN org_members m ON m.sub = usc.sub AND m.is_active
         WHERE usc.org_id = 0
           AND usc.sub NOT LIKE 'demo\\_%' ESCAPE '\\'
         ORDER BY usc.sub
        """
    ).fetchall()
    return [dict(r) for r in rows]


def main(apply: bool) -> int:
    total_repointees = total_fusionnees = 0
    with _connect() as conn:
        candidates = _candidates(conn)
        print(f"{len(candidates)} compte(s) avec une sélection sous l'ancien org_id=0\n")

        for row in candidates:
            sub, home = row["sub"], int(row["home"])
            stale = conn.execute(
                "SELECT connector FROM user_selected_connectors "
                "WHERE sub = %s AND org_id = 0", (sub,)).fetchall()
            reelles = {r["connector"] for r in conn.execute(
                "SELECT connector FROM user_selected_connectors "
                "WHERE sub = %s AND org_id = %s", (sub, home)).fetchall()}

            repointees, fusionnees = [], []
            for s in stale:
                name = s["connector"]
                if name in reelles:
                    # L'état réel gagne TOUJOURS : on jette le fantôme sans le
                    # toucher, jamais l'inverse (cf. docstring du module).
                    fusionnees.append(name)
                    conn.execute(
                        "DELETE FROM user_selected_connectors "
                        "WHERE sub = %s AND org_id = 0 AND connector = %s",
                        (sub, name))
                else:
                    repointees.append(name)
                    conn.execute(
                        "UPDATE user_selected_connectors SET org_id = %s "
                        "WHERE sub = %s AND org_id = 0 AND connector = %s",
                        (home, sub, name))

            print(f"  {sub} -> org {home} : {len(repointees)} repointée(s), "
                  f"{len(fusionnees)} fusionnée(s) (état réel inchangé)")
            total_repointees += len(repointees)
            total_fusionnees += len(fusionnees)

        print(f"\n{total_repointees} ligne(s) repointée(s), {total_fusionnees} fusionnée(s) "
              "(aucun état réel modifié)")
        if apply:
            conn.commit()
            print("commité.")
        else:
            conn.rollback()
            print("dry-run — écritures rejouées puis ANNULÉES (--apply pour committer)")
    return 0


if __name__ == "__main__":
    sys.exit(main(apply="--apply" in sys.argv))
