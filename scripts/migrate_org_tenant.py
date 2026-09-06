"""Fait porter à chaque organisation le tenant qui l'héberge (`orgs.tenant_id`).

**Pourquoi une commande, et pas le boot.** Ce sont les données d'un partenaire : ses
organisations, ses utilisateurs, sa responsabilité. Repointer 65 lignes doit être un
geste NOMMÉ, joué en connaissance de cause, pas un effet de bord d'un déploiement que
personne ne regarde. Le déploiement, lui, ne change que l'avenir : depuis ce lot,
`org_store.create_org` déclare le tenant à la naissance de l'org — les lignes
existantes, elles, attendent cette commande.

**Ce que ça change, et ce que ça ne change pas.** Mesuré en prod le 2026-09-03 :

- `orgs.tenant_id` ÉTAIT inerte — 165 orgs sur 165 portaient le tenant primaire,
  dont les 65 qui vivent chez le partenaire. Le provisioning ne l'écrivait pas.
- Le seul décideur qui lise le rattachement (`billing_grants.org_is_ours`, le
  périmètre commercial) passe par `db.org_tenant_slug`, l'**union** de trois axes
  dont le déclaré n'est que le premier. Remplir la colonne ne change donc AUCUN de
  ses verdicts : vérifié org par org, 165 sur 165 identiques avant/après.
- Ce qui change est le SUIVI : la console plateforme cesse de compter les 65 orgs
  chez nous, et son compteur `orgs_desalignees` tombe de 48 à 0.
- Ce qui ne change pas non plus : la résolution de credentials. La cascade lit le
  tenant du SUB appelant, « jamais sur le rattachement de l'org » — c'est écrit à ses
  deux points d'entrée (`access/cascade.py`, `access/chain_resolution.py`).

**Le décompte à blanc EST la migration, sans le commit.** Le mode par défaut joue le
`UPDATE … RETURNING` réel dans une transaction, affiche les lignes rendues, puis
`ROLLBACK`. Ce qu'il annonce n'est donc pas une requête qui *ressemble* à la
migration : c'est elle. Et comme le prédicat est réévalué à chaque exécution, un
décompte d'hier n'engage rien — on rejoue à blanc juste avant d'appliquer.

**Deux refus, avant toute écriture** (`--force` ne les lève pas, ils n'ont pas
d'échappatoire — un rattachement qu'on ne sait pas dériver se tranche à la main) :

1. **désaccord entre les deux dérivations** — une org dont la marque de front dit un
   tenant et dont un membre qualifié en dit un autre. C'est la concordance des deux
   axes indépendants qui autorise à écrire sans deviner ; sans elle, on devine.
2. **org déjà déclarée, et contredite** — elle porte un tenant qu'aucune dérivation
   ne soutient. L'écraser effacerait l'information qui dit qu'il y a un problème.

    # sur la box, après déploiement :
    ssh -i ~/.ssh/<clé> root@<box> \\
      "cd /opt/oto-mcp && ./.venv/bin/python -m scripts.migrate_org_tenant"
    #   ^ à blanc par défaut : joue la migration et la défait, n'écrit rien
    #     --apply pour valider la transaction

Idempotent : le prédicat ne retient que les lignes dont le rattachement DIFFÈRE de sa
dérivation. Un second passage n'a plus rien à toucher et le dit.

**JOUÉ EN PRODUCTION le 2026-09-03** (`--apply`, tag servi v1.186.0) : 65 orgs
repointées vers `tulina` — 48 vivantes, 17 archivées — le compteur `orgs_desalignees`
tombé de 48 à 0, et le passage suivant rendu « rien à faire ». Les deux refus n'ont
tiré ni l'un ni l'autre. Ce qui précède décrit donc l'état d'AVANT ce geste ; la
commande reste, elle resservira au prochain tenant, et son décompte à blanc est la
façon normale de vérifier qu'il n'y a rien à faire.
"""
from __future__ import annotations

import sys

from oto_mcp import tenancy
from oto_mcp.db._conn import _connect
from oto_mcp.db.tenants import _AXE_MARQUE, _AXE_MEMBRE

# Les axes viennent de `db/tenants.py`, ils ne sont pas recopiés ici : deux
# définitions du « chez le partenaire » divergent toujours, et c'est la moins prudente
# qui l'emporte en silence. Ce script écrit donc EXACTEMENT ce que `db.org_tenant_slug`
# dérive — c'est la seule chose qui les tient ensemble depuis que le contrôle de
# conformité a été retiré (03/09/2026, il ne rapportait qu'un écart sans conséquence).
_DERIVE = f"COALESCE({_AXE_MARQUE}, {_AXE_MEMBRE})"

# ── Les deux refus, joués AVANT l'écriture ───────────────────────────────────

_DESACCORD_SQL = f"""
    SELECT o.id, o.name, {_AXE_MARQUE} AS par_marque, {_AXE_MEMBRE} AS par_membre
      FROM orgs o
     WHERE {_AXE_MARQUE} IS NOT NULL
       AND {_AXE_MEMBRE} IS NOT NULL
       AND {_AXE_MARQUE} IS DISTINCT FROM {_AXE_MEMBRE}
     ORDER BY o.id
"""

_CONTREDITES_SQL = f"""
    SELECT o.id, o.name,
           (SELECT t.slug FROM tenants t WHERE t.id = o.tenant_id) AS declare,
           {_DERIVE} AS derive
      FROM orgs o
     WHERE (SELECT t.slug FROM tenants t WHERE t.id = o.tenant_id) <> %(primary)s
       AND {_DERIVE} IS DISTINCT FROM
           (SELECT t.slug FROM tenants t WHERE t.id = o.tenant_id)
     ORDER BY o.id
"""

# L'ORDRE D'ÉCRITURE — le même en décompte à blanc et à l'application. `RETURNING`
# rend exactement les lignes touchées : c'est le décompte, pas son approximation.
_UPDATE_SQL = f"""
    UPDATE orgs o
       SET tenant_id = cible.id
      FROM tenants cible
     WHERE cible.slug = {_DERIVE}
       AND cible.slug <> %(primary)s
       AND o.tenant_id IS DISTINCT FROM cible.id
    RETURNING o.id, o.name, o.archived_at IS NULL AS vivante, cible.slug
"""

_ETAT_SQL = """
    SELECT COALESCE(t.slug, '(id ' || o.tenant_id || ' inconnu)') AS tenant,
           COUNT(*) AS orgs,
           COUNT(*) FILTER (WHERE o.archived_at IS NULL) AS vivantes
      FROM orgs o LEFT JOIN tenants t ON t.id = o.tenant_id
     GROUP BY 1 ORDER BY 2 DESC
"""


def _etat(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(_ETAT_SQL).fetchall()]


def _bloquants(conn, params) -> tuple[list[dict], list[dict]]:
    desaccords = [dict(r) for r in conn.execute(_DESACCORD_SQL, params).fetchall()]
    contredites = [dict(r) for r in conn.execute(_CONTREDITES_SQL, params).fetchall()]
    return desaccords, contredites


def main(apply: bool) -> int:
    params = {"primary": tenancy.PRIMARY_SLUG}
    with _connect() as conn:
        print("── état AVANT ──")
        for r in _etat(conn):
            print(f"  {r['tenant']:<24} {r['orgs']:>4} orgs  "
                  f"({r['vivantes']} vivantes)")

        # ⚠️ La concordance se REVÉRIFIE ici, au moment d'écrire — jamais sur la foi
        # d'une mesure antérieure. C'est elle qui autorise à écrire sans deviner.
        desaccords, contredites = _bloquants(conn, params)
        if desaccords:
            print(f"\n✗ REFUS — {len(desaccords)} org(s) dont les deux dérivations se "
                  "contredisent :")
            for r in desaccords:
                print(f"    #{r['id']} {r['name']!r} : marque={r['par_marque']} "
                      f"membre={r['par_membre']}")
            print("  Les deux axes sont indépendants ; leur concordance est ce qui "
                  "autorise à écrire sans deviner.\n  Trancher à la main avant de "
                  "rejouer — ce refus n'a pas d'échappatoire.")
            conn.rollback()
            return 2
        print("\n✓ concordance : aucune org où marque de front et membre qualifié se "
              "contredisent.")

        if contredites:
            print(f"\n✗ REFUS — {len(contredites)} org(s) déjà déclarées, et "
                  "contredites par leur dérivation :")
            for r in contredites:
                print(f"    #{r['id']} {r['name']!r} : déclaré={r['declare']} "
                      f"dérivé={r['derive'] or '(aucun signal)'}")
            print("  Les écraser effacerait l'information qui dit qu'il y a un "
                  "problème.")
            conn.rollback()
            return 2

        touchees = [dict(r) for r in conn.execute(_UPDATE_SQL, params).fetchall()]
        par_tenant: dict[str, list[dict]] = {}
        for r in touchees:
            par_tenant.setdefault(r["slug"], []).append(r)

        print(f"\n── {len(touchees)} org(s) à repointer ──")
        for slug, lignes in sorted(par_tenant.items()):
            vivantes = sum(1 for r in lignes if r["vivante"])
            print(f"  → tenant {slug!r} : {len(lignes)} orgs "
                  f"({vivantes} vivantes, {len(lignes) - vivantes} archivées)")
            print("     ids : " + ", ".join(str(r["id"]) for r in lignes))

        if not touchees:
            print("  (rien à faire — chaque org porte déjà le tenant qu'on dérive)")

        if not apply:
            conn.rollback()
            print("\nÀ BLANC — la transaction est annulée, rien n'a été écrit.")
            print("Les lignes ci-dessus sont celles que `--apply` toucherait, rendues "
                  "par\nl'ordre d'écriture lui-même. Rejouer juste avant d'appliquer : "
                  "le prédicat\nest réévalué, un décompte d'hier n'engage rien.")
            return 0

        print("\n── état APRÈS (dans la transaction, avant commit) ──")
        for r in _etat(conn):
            print(f"  {r['tenant']:<24} {r['orgs']:>4} orgs  "
                  f"({r['vivantes']} vivantes)")
    print("\n✓ APPLIQUÉ — transaction validée.")
    return 0


if __name__ == "__main__":
    sys.exit(main(apply="--apply" in sys.argv[1:]))
