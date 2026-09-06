"""Étend la clé du compteur de la fenêtre L7 à l'ORIGINE de l'écriture.

**Pourquoi une commande, et pas le boot.** La colonne `origine` est additive et naît
au démarrage ; la CLÉ PRIMAIRE, elle, ne s'étend qu'en la retirant d'abord —
`DROP CONSTRAINT`, un ordre non additif. [ADR 0065] les sort du démarrage : ils
deviennent un acte nommé, daté, rejouable, joué **entre deux promotions** sur une base
PARTAGÉE avec la production.

**Ce que ça change.** Avant : `(day, connector, org_id, classe)` — prod et preprod se
partagent une ligne, et leurs compteurs se mélangent. Après : la même clé plus
`origine`, donc une ligne par environnement. C'est ce qui rend lisible « une fenêtre en
PROD », la mesure qui autorise la bascule d'autorité du lot L7.

**L'ordre n'a pas d'importance, et c'est voulu.** L'écriture (`db.access_shadow.
bump_shadow`) ne suppose aucune des deux formes : elle tente la ligne de SON origine,
et si l'ancienne clé tient encore elle crédite la ligne partagée — le comportement
d'avant la colonne. Cette commande peut donc tourner avant ou après le déploiement,
sans fenêtre où l'on perd des observations.

**Les lignes existantes gardent `origine` NULL** et ne sont pas réécrites : elles sont
réellement ambiguës (écrites quand personne ne notait l'origine), et leur prêter une
valeur serait inventer une mesure. ⚠️ Une clé primaire refuse le NULL : la clé étendue
est donc portée par un **index unique** avec `NULLS NOT DISTINCT`, qui accepte les
lignes ambiguës tout en gardant l'unicité — y compris entre elles.

    # sur la box, après déploiement :
    ssh -i ~/.ssh/<clé> root@<box> \
      "cd /opt/oto-mcp && ./.venv/bin/python -m scripts.migrate_shadow_origine"
    #   ^ dry-run par défaut : dit l'état et ce qu'il ferait, n'écrit rien
    #     --apply pour exécuter

Idempotent : l'état est LU dans `pg_indexes`/`pg_constraint`, pas supposé. Un second
passage constate que c'est fait et ne touche à rien.
"""
from __future__ import annotations

import sys

from oto_mcp.db._conn import _connect

INDEX = "access_shadow_l7_origine_key"
PK = "access_shadow_l7_pkey"


def _etat() -> dict:
    with _connect() as conn:
        pk = conn.execute(
            "SELECT 1 FROM pg_constraint WHERE conname = %s AND contype = 'p'",
            (PK,)).fetchone()
        idx = conn.execute(
            "SELECT 1 FROM pg_indexes WHERE schemaname = 'public' AND indexname = %s",
            (INDEX,)).fetchone()
        colonne = conn.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_name = "
            "'access_shadow_l7' AND column_name = 'origine'").fetchone()
        lignes = conn.execute(
            "SELECT count(*) FILTER (WHERE origine IS NULL) AS ambigues, count(*) AS n "
            "FROM access_shadow_l7").fetchone() if colonne else {"ambigues": 0, "n": 0}
    return {"pk_ancienne": bool(pk), "index_neuf": bool(idx),
            "colonne": bool(colonne), **dict(lignes)}


def main(apply: bool) -> int:
    e = _etat()
    print(f"colonne `origine` : {'présente' if e['colonne'] else 'ABSENTE'}")
    print(f"ancienne clé primaire : {'encore là' if e['pk_ancienne'] else 'retirée'}")
    print(f"clé étendue `{INDEX}` : {'posée' if e['index_neuf'] else 'absente'}")
    print(f"{e['n']} ligne(s), dont {e['ambigues']} d'origine inconnue "
          "(écrites avant la colonne — elles le restent).")

    if not e["colonne"]:
        print("\n⚠️  la colonne n'existe pas : déploie d'abord le code qui la pose au "
              "démarrage (ordre additif), puis rejoue cette commande.")
        return 1
    if e["index_neuf"] and not e["pk_ancienne"]:
        print("\nrien à faire — la clé porte déjà l'origine.")
        return 0
    if not apply:
        print(f"\ndry-run : poserait `{INDEX}` sur (day, connector, org_id, classe, "
              f"origine) NULLS NOT DISTINCT, puis retirerait `{PK}`. "
              "Rien n'a été écrit (--apply pour exécuter)")
        return 0

    with _connect() as conn:
        # L'index D'ABORD, la clé retirée ENSUITE : à aucun instant la table n'est
        # sans garde d'unicité. L'inverse ouvrirait une fenêtre où deux process
        # peuvent créer deux lignes pour la même clé — et un compteur dupliqué ne se
        # répare pas, il se soustrait à la main.
        conn.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {INDEX} ON access_shadow_l7 "
            "(day, connector, org_id, classe, origine) NULLS NOT DISTINCT")
        conn.execute(f"ALTER TABLE access_shadow_l7 DROP CONSTRAINT IF EXISTS {PK}")
    apres = _etat()
    ok = apres["index_neuf"] and not apres["pk_ancienne"]
    print(f"\nvérification : clé étendue {'posée' if apres['index_neuf'] else 'ABSENTE'}, "
          f"ancienne clé {'retirée' if not apres['pk_ancienne'] else 'ENCORE LÀ'}.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(apply="--apply" in sys.argv))
