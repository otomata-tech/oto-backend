"""Archive les instances de connecteur ORPHELINES — celles sans ligne de coffre.

Une instance vivante dont la ligne du coffre n'existe plus est un objet qui désigne
une clé absente : un binding, une arête de grant ou une consommation peuvent la
nommer, et elle répondrait d'une clé que personne ne peut plus lire.

**D'où elles viennent, et pourquoi il n'y en aura plus.** Entre la pièce 1 du lot L6
(le boot NOMMAIT chaque ligne de coffre) et la pièce 2 (la pose nomme, le retrait
archive), un retrait de credential supprimait la ligne sans débaptiser l'instance :
chaque suppression en fabriquait une. La pièce 2 ferme la source — retirer une clé
archive son instance dans la MÊME transaction, et les trois retraits en masse qui
contournaient l'entonnoir y sont rentrés. Ce script nettoie ce qui reste, une fois.

Mesuré sur la base servie le 2026-08-28, avant déploiement de la pièce 2 : **2
orphelines** sur 139 instances vivantes.

⚠️ **Hors du boot, délibérément** (ADR 0065) : le filet de démarrage ne sait
qu'insérer, et lui faire archiver au boot ferait d'un ordonnanceur de maintenance un
écrivain de masse sur une base PARTAGÉE avec la production. C'est une commande
explicite, à sec par défaut, qu'on lance une fois et qu'on regarde.

Archive (`revoked_at`, motif `vault_row_missing`), jamais un DELETE : « elle a été
retirée » et « elle n'a jamais existé » ne sont pas le même verdict.

    # sur la box, après déploiement :
    ssh -i ~/.ssh/<clé> root@<box> \
      "cd /opt/oto-mcp && ./.venv/bin/python -m scripts.archive_orphan_instances"
    #   ^ dry-run par défaut : liste et compte, n'écrit rien
    #     --apply pour exécuter

Idempotent : le prédicat est « vivante ET sans ligne de coffre », donc un second
passage rend 0. Rejouable sans risque.
"""
from __future__ import annotations

import sys

from oto_mcp.db import connector_instances


def main(apply: bool) -> int:
    orphelines = connector_instances.list_orphan_instances()
    print(f"{len(orphelines)} instance(s) vivante(s) SANS ligne de coffre\n")
    for i in orphelines:
        compte = i["account"] or "(mono-compte)"
        print(f"  inst:{i['id']:<6} {i['owner_type']}:{i['owner_id']} "
              f"{i['connector']} {compte}  créée {i['created_at']}")

    if not orphelines:
        print("\nrien à archiver — l'invariant tient dans ce sens.")
        return 0
    if not apply:
        print("\ndry-run — rien n'a été écrit (--apply pour exécuter)")
        return 0

    n = connector_instances.archive_orphan_instances()
    print(f"\n{n} instance(s) archivée(s), motif "
          f"{connector_instances.REVOKED_VAULT_ROW_MISSING!r}.")
    reste = connector_instances.list_orphan_instances()
    print(f"vérification : {len(reste)} orpheline(s) restante(s) "
          f"(attendu 0 — le prédicat ne les voit plus).")
    return 0


if __name__ == "__main__":
    sys.exit(main(apply="--apply" in sys.argv))
