#!/usr/bin/env python3
"""Migre une colonne fantôme `<champ>_<couche>` (tiret bas, littérale) vers sa
couche imbriquée `<champ>.<couche>` (oto-backend#957, scission de #687).

**Pourquoi ce script existe.** Avant le correctif de #687 (`oto_mcp/datastore/
points.py`), des agents écrivaient `effectif_comment` (colonne littérale, tiret bas)
là où il fallait `effectif.comment` (couche imbriquée). `points.py` referme l'aller-
retour export→import pour les écritures À VENIR ; les lignes déjà corrompues avant ce
correctif restent en l'état — c'est ce que ce script nettoie, à la demande (« ni
urgent ni bloquant », Alexis).

**Précondition, non négociable.** Ce script ne vérifie PAS que les cinq portes
d'écriture sont au même régime — c'est acquis depuis `points.py` (câblé le 14/09). Ne
JAMAIS le lancer sur un tableau où une porte pose encore des colonnes fantômes : la
migration se déferait toute seule au prochain écrit, sans que personne ne le
remarque.

**Trois populations, trois gestes — jamais le même traitement.** Sur une ligne qui
porte la clé fantôme `<champ>_<couche>` :

    | fantôme vide ?  | couche déjà posée ? | population              | geste              |
    |-----------------|----------------------|--------------------------|--------------------|
    | oui              | —                    | 3 — fantôme vide         | effaçable          |
    | non              | non (vide/absente)   | 1 — couche vide          | MIGRER             |
    | non              | oui (déjà posée)     | 2 — les deux pleines     | arbitrer, signaler |

Un outil qui traite les trois pareil DÉTRUIT la population 1 : elle porte la SEULE
provenance existante, et un geste qui l'écrase silencieusement (ou la classe comme
« vide » à tort) perd une donnée qui n'existe nulle part ailleurs.

**Classement par différentiel d'inventaire, jamais par un texte formaté pour un
humain (#680).** On lit `data->'<champ>'` (le socle) et `data->>'<champ>_<couche>'`
(le fantôme) directement en JSONB — jamais un résumé, jamais une réponse d'API.

**Population 2 n'est JAMAIS migrée automatiquement.** Les deux valeurs sont
DIFFÉRENTES par construction (sinon la population serait redondante, pas
conflictuelle) : ce script les liste nommément (ligne, valeur de couche, valeur
fantôme) pour un arbitrage humain, et ne touche à AUCUNE des deux.

**Retrait de la colonne fantôme APRÈS la migration, jamais avant** : la clé
`<champ>_<couche>` n'est retirée d'une ligne QUE quand cette ligne vient d'être
migrée (population 1) ou est confirmée vide (population 3, avec `--purger-vides`) —
jamais en bloc sur tout le tableau, jamais sur une ligne de la population 2.

    # sur la box, en lecture seule par défaut :
    ssh -i ~/.ssh/<clé> root@<box> \
      "cd /opt/oto-mcp && ./.venv/bin/python -m scripts.migrer_colonnes_fantomes \
        <ns_id> <champ> <couche>"
    #   ^ dry-run par défaut : classe et rapporte, n'écrit rien
    #     --apply migre la population 1
    #     --purger-vides (avec --apply) efface aussi la clé fantôme de la population 3
    #     population 2 n'est jamais touchée par ce script, quels que soient les drapeaux

Idempotent : un second passage sur un tableau déjà migré trouve 0 ligne à traiter (la
clé fantôme n'existe plus sur les lignes migrées)."""
from __future__ import annotations

import argparse
import sys
from typing import Any, Optional

from oto_mcp.datastore.couches import LAYER_KEYS, VALUE_LAYER
from oto_mcp.db._conn import _connect


def _est_vide(v: Any) -> bool:
    return v is None or v == ""


def _inventaire(ns_id: int, champ: str, couche: str) -> list[dict]:
    """Une ligne par row qui porte encore la clé fantôme `<champ>_<couche>` — lu en
    JSONB, jamais déduit d'un texte servi (#680)."""
    fantome_key = f"{champ}_{couche}"
    with _connect() as conn:
        rows = conn.execute(
            "SELECT row_id, data->%(champ)s::text AS socle, "
            "       data->>%(fk)s::text AS fantome "
            "  FROM datastore_rows "
            " WHERE ns_id = %(ns)s::int AND data ? %(fk)s::text",
            {"champ": champ, "fk": fantome_key, "ns": ns_id},
        ).fetchall()
    return [dict(r) for r in rows]


def _couche_actuelle(socle: Any, couche: str) -> Any:
    if isinstance(socle, dict) and (set(socle) & {VALUE_LAYER, *LAYER_KEYS}):
        return socle.get(couche)
    return None  # socle scalaire (ou absent) : aucune couche posée pour l'instant


def _classer(rows: list[dict], couche: str) -> dict:
    pop1, pop2, pop3 = [], [], []
    for r in rows:
        fantome, socle = r["fantome"], r["socle"]
        actuelle = _couche_actuelle(socle, couche)
        if _est_vide(fantome):
            pop3.append(r)
        elif _est_vide(actuelle):
            pop1.append(r)
        else:
            pop2.append(r)
    return {"pop1": pop1, "pop2": pop2, "pop3": pop3}


def _socle_avec_couche(socle: Any, couche: str, valeur: Any) -> dict:
    """Le socle réécrit, portant maintenant la couche — jamais un remplacement du
    reste : un socle déjà à couches garde ses autres couches, un socle scalaire
    devient `{"valeur": <scalaire>, <couche>: <valeur>}`."""
    if isinstance(socle, dict) and (set(socle) & {VALUE_LAYER, *LAYER_KEYS}):
        nouveau = dict(socle)
    else:
        nouveau = {VALUE_LAYER: socle}
    nouveau[couche] = valeur
    return nouveau


def _migrer_pop1(ns_id: int, champ: str, couche: str, fantome_key: str,
                  rows: list[dict]) -> int:
    n = 0
    with _connect() as conn:
        for r in rows:
            nouveau_socle = _socle_avec_couche(r["socle"], couche, r["fantome"])
            conn.execute(
                "UPDATE datastore_rows "
                "   SET data = (data - %(fk)s::text) "
                "            || jsonb_build_object(%(champ)s::text, %(socle)s::jsonb) "
                " WHERE ns_id = %(ns)s::int AND row_id = %(rid)s::text "
                "   AND data ? %(fk)s::text",
                {"fk": fantome_key, "champ": champ, "socle": _dumps(nouveau_socle),
                 "ns": ns_id, "rid": r["row_id"]},
            )
            n += 1
    return n


def _purger_pop3(ns_id: int, fantome_key: str, rows: list[dict]) -> int:
    if not rows:
        return 0
    ids = [r["row_id"] for r in rows]
    with _connect() as conn:
        return conn.execute(
            "UPDATE datastore_rows SET data = data - %(fk)s::text "
            " WHERE ns_id = %(ns)s::int AND row_id = ANY(%(ids)s::text[]) "
            "   AND data ? %(fk)s::text",
            {"fk": fantome_key, "ns": ns_id, "ids": ids},
        ).rowcount or 0


def _dumps(obj: Any) -> str:
    import json
    return json.dumps(obj)


def main(ns_id: int, champ: str, couche: str, *, apply: bool, purger_vides: bool) -> int:
    if couche not in LAYER_KEYS:
        print(f"couche `{couche}` inconnue — attendu l'une de {LAYER_KEYS}.")
        return 1
    fantome_key = f"{champ}_{couche}"

    rows = _inventaire(ns_id, champ, couche)
    if not rows:
        print(f"rien à faire — aucune ligne du tableau {ns_id} ne porte "
              f"`{fantome_key}`.")
        return 0

    pops = _classer(rows, couche)
    print(f"{len(rows)} ligne(s) portant `{fantome_key}` sur le tableau {ns_id} :")
    print(f"  population 1 (couche vide, fantôme plein — À MIGRER) : {len(pops['pop1'])}")
    print(f"  population 2 (les deux pleines — ARBITRAGE HUMAIN) : {len(pops['pop2'])}")
    print(f"  population 3 (fantôme vide — effaçable) : {len(pops['pop3'])}")

    if pops["pop2"]:
        print("\n⚠️  population 2, jamais migrée automatiquement — à arbitrer :")
        for r in pops["pop2"]:
            actuelle = _couche_actuelle(r["socle"], couche)
            print(f"    ligne {r['row_id']} : couche=`{actuelle}` vs "
                  f"fantôme=`{r['fantome']}`")

    if not apply:
        print(f"\ndry-run : migrerait {len(pops['pop1'])} ligne(s) (population 1)"
              + (f", effacerait {len(pops['pop3'])} ligne(s) vide(s) (population 3)"
                 if purger_vides else
                 f" ; population 3 laissée en l'état (repasse avec --purger-vides "
                 f"pour l'effacer)")
              + ". Rien n'a été écrit (--apply pour exécuter).")
        return 0

    migrees = _migrer_pop1(ns_id, champ, couche, fantome_key, pops["pop1"])
    print(f"\n{migrees} ligne(s) migrée(s) (population 1).")

    if purger_vides:
        effacees = _purger_pop3(ns_id, fantome_key, pops["pop3"])
        print(f"{effacees} ligne(s) vidée(s) de leur colonne fantôme (population 3).")
    elif pops["pop3"]:
        print(f"{len(pops['pop3'])} ligne(s) de population 3 laissée(s) en l'état "
              f"(repasse avec --purger-vides pour les effacer).")

    if pops["pop2"]:
        print(f"\n{len(pops['pop2'])} ligne(s) de population 2 restent en l'état — "
              f"arbitrage humain requis, ce script ne les touche jamais.")

    restantes = _inventaire(ns_id, champ, couche)
    print(f"\nvérification : {len(restantes)} ligne(s) portent encore `{fantome_key}` "
          f"(attendu : population 2 uniquement"
          + ("" if purger_vides else " + population 3 non purgée") + ").")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("ns_id", type=int, help="identifiant numérique du tableau")
    p.add_argument("champ", help="nom du champ déclaré (ex. effectif)")
    p.add_argument("couche", help="couche visée (origine, comment ou link)")
    p.add_argument("--apply", action="store_true", help="exécute (sinon dry-run)")
    p.add_argument("--purger-vides", action="store_true",
                    help="avec --apply : efface aussi la colonne fantôme des lignes "
                         "vides (population 3)")
    a = p.parse_args()
    sys.exit(main(a.ns_id, a.champ, a.couche, apply=a.apply,
                  purger_vides=a.purger_vides))
