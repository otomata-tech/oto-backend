"""La LECTURE du journal des révisions de ligne (oto#273, jalon M3).

Le journal est écrit par un déclencheur (`db/journal_revisions.py`) et estampillé par
le serveur (`db/estampille.py`). Ce module est le seul à le lire. Il sert :

- l'historique d'une ligne, `GET …/rows/{row_id}/history` et `data_row_history`
  (`capabilities/datastore/history.py`) ;
- le parcours d'une ligne, `GET …/rows/{row_id}/activity`, qui y prend les écritures
  avec leurs valeurs (`capabilities/datastore/activity.py`).

Un module à part, et pas une fonction de plus dans `journal_revisions.py` : celui-là est
importé par `_conn.py` (l'interrupteur se lit à l'ouverture du pool), il ne peut donc
pas lui-même ouvrir une connexion.

⚠️ **Ce que le journal ne sait pas, il le dit** (`COUVERTURE`) : il ne couvre que les
écritures faites depuis sa mise en service. Une ligne sans révision n'est pas une ligne
jamais modifiée.
"""
from __future__ import annotations

from typing import Optional

from ._conn import _connect
from .journal_revisions import TABLE, VARIABLE

# La MISE EN SERVICE du journal sur la base partagée (prod et préprod) : le jour où les
# déclencheurs ont été posés. Commit M1 `4119391a` sur le tronc le 24/09/2026 à 07:42Z,
# prod `v1.341.0` à 08:26Z ; la préprod, qui partage la base, a pu les poser entre les
# deux.
#
# Une constante, et pas `min(at)` de la table : la rétention décidée dans oto#273 (90
# jours) déplacera ce minimum sans que le début de la couverture ait changé. Un chiffre
# lu dans une donnée qu'une purge taille finit par décrire la purge. Le JOUR, pas
# l'heure : c'est la précision que l'on sait garantir.
MISE_EN_SERVICE = "2026-09-24"

# Servie telle quelle, à l'agent comme à l'écran. Sans elle, une ligne sans révision se
# lirait « jamais modifiée ».
COUVERTURE = (
    f"Le journal ne couvre que les écritures faites depuis sa mise en service, le "
    f"{MISE_EN_SERVICE} au matin (UTC) : ce qui a été écrit avant n'y figure pas, et une "
    f"ligne sans révision n'est PAS une ligne jamais modifiée. Il peut avoir des trous : "
    f"coupé par `{VARIABLE}=off`, il n'écrit rien. `rev` 0 est une insertion ; sous un "
    f"même `row_id`, elle revient si la ligne a été supprimée puis recréée. Une "
    f"suppression est une révision (`suppression: true`, toutes les valeurs en "
    f"`avant`) depuis que le journal les enregistre : une ligne absente sans révision "
    f"de suppression a pu être supprimée quand même, avant.")

_COLONNES = ("id, rev, at, acteur, run_id, source, geste_id, suppression, "
             "outil, fournisseur")


def revisions_de_ligne(ns_id: int, row_id: str, *, champ: Optional[str] = None,
                       avant_id: Optional[int] = None, limit: int = 50) -> list[dict]:
    """Les révisions d'une ligne, la plus récente d'abord (`id` décroissant), au plus
    `limit + 1` : la ligne de trop dit à l'appelant qu'il reste une page.

    L'ordre suit l'`id`, pas la `rev` : une ligne supprimée puis recréée sous le même
    `row_id` repart de `rev` 0, seul l'`id` est monotone. `champ` ne garde que les
    révisions qui touchent cette colonne, et ne rend que sa partie du diff."""
    clauses = ["ns_id = %s", "row_id = %s"]
    params: list = [int(ns_id), str(row_id)]
    diff, params_diff = "diff", []
    if champ is not None:
        clauses.append("diff ? %s")
        params.append(champ)
        diff, params_diff = "jsonb_build_object(%s::text, diff -> %s::text)", [champ, champ]
    if avant_id is not None:
        clauses.append("id < %s")
        params.append(int(avant_id))
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            f"SELECT {_COLONNES}, {diff} AS diff FROM {TABLE} "
            f"WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT %s",
            (*params_diff, *params, int(limit) + 1)).fetchall()]


def bilan_de_ligne(ns_id: int, row_id: str) -> dict:
    """Ce que le journal sait d'une ligne, tous champs confondus : combien de
    révisions, la date de la première, si une insertion y figure, et `suppression` :
    la plus récente révision quand c'est une suppression (sans son diff), sinon `None`.

    Une écriture en `rev` 0 n'est portée que par une insertion : le déclencheur de
    révision avance `rev` à toute mise à jour de `data`, avant que le journal ne la
    lise. Une suppression recopie la `rev` de la ligne qui part, 0 compris : elle ne
    compte pas pour une insertion."""
    params = (int(ns_id), str(row_id))
    with _connect() as conn:
        bilan = dict(conn.execute(
            f"SELECT count(*) AS revisions, min(at) AS premiere, "
            f"COALESCE(bool_or(rev = 0 AND NOT suppression), false) AS insertion "
            f"FROM {TABLE} WHERE ns_id = %s AND row_id = %s", params).fetchone())
        derniere = conn.execute(
            f"SELECT {_COLONNES} FROM {TABLE} WHERE ns_id = %s AND row_id = %s "
            f"ORDER BY id DESC LIMIT 1", params).fetchone()
    bilan["suppression"] = dict(derniere) if derniere and derniere["suppression"] else None
    return bilan


def dernier_id() -> int:
    """L'`id` de la révision la plus récente (0 sur un journal vide) : un repère posé
    AVANT un geste, pour relire ensuite ses seules révisions par la clé primaire."""
    with _connect() as conn:
        row = conn.execute(f"SELECT COALESCE(max(id), 0) AS id FROM {TABLE}").fetchone()
    return int(row["id"])


def revisions_du_geste(geste_id: str, depuis_id: int, *, limit: int = 1001) -> list[dict]:
    """Les révisions qu'un GESTE a laissées après le repère `depuis_id`, dans l'ordre
    d'écriture : `ns_id`, `row_id`, `rev`, `diff`, `suppression`. Lu par l'agent intégré
    pour défaire ce qu'il a écrit (`capabilities/copilot/annulation.py`). Parcours par
    la clé primaire depuis le repère, borné : le geste vient d'avoir lieu."""
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            f"SELECT id, ns_id, row_id, rev, diff, suppression FROM {TABLE} "
            f"WHERE id > %s AND geste_id = %s ORDER BY id LIMIT %s",
            (int(depuis_id), str(geste_id), int(limit))).fetchall()]


# ── QUI A TROUVÉ chaque colonne — l'en-tête d'une colonne (« Jev, Hunter 62 % ») ──
#
# Deux lectures, chacune proportionnelle à la TAILLE du tableau, jamais à son histoire :
# - les cases ATTRIBUÉES (`datastore_cell_sources`, par sa clé primaire) groupées par
#   outil / fournisseur / face — `db/attribution_cellules.py` dit ce qui y entre ;
# - les cases TENUES par colonne (ligne vivante, valeur ni absente ni `null`, le jumeau
#   SQL d'`unwrap`), pour les parts. Bornée : si elle dépasse, elle est omise
#   (`tenues: None`) — les parts se lisent alors entre cases attribuées seulement.


def sources_des_colonnes(ns_id: int) -> dict:
    """`{"colonnes": {champ: [{source, outil, fournisseur, cellules}]},
    "tenues": {champ: n} | None}` — les groupes d'une colonne du plus fourni au moins."""
    from .attribution_cellules import TABLE as CASES
    from .lecture_bornee import LectureTropLongue, lecture_d_agregat
    from .paths import _valeur_jsonb
    with lecture_d_agregat("sources des colonnes") as conn:
        lignes = conn.execute(
            f"SELECT champ, source, outil, fournisseur, count(*) AS cellules "
            f"FROM {CASES} WHERE ns_id = %s GROUP BY 1, 2, 3, 4",
            (int(ns_id),)).fetchall()
    colonnes: dict[str, list[dict]] = {}
    for r in lignes:
        colonnes.setdefault(r["champ"], []).append({
            "source": r["source"], "outil": r["outil"],
            "fournisseur": r["fournisseur"], "cellules": int(r["cellules"])})
    for groupes in colonnes.values():
        groupes.sort(key=lambda g: (-g["cellules"], g["outil"] or "", g["fournisseur"] or ""))
    tenues: Optional[dict] = None
    if colonnes:
        v = _valeur_jsonb("e.v")
        try:
            with lecture_d_agregat("cases tenues par colonne") as conn:
                tenues = {r["champ"]: int(r["n"]) for r in conn.execute(
                    f"SELECT e.k AS champ, count(*) AS n "
                    f"FROM datastore_rows r, jsonb_each(r.data) AS e(k, v) "
                    f"WHERE r.ns_id = %s AND jsonb_typeof(r.data) = 'object' "
                    f"AND e.k = ANY(%s) AND {v} IS NOT NULL AND {v} <> 'null'::jsonb "
                    f"GROUP BY 1", (int(ns_id), list(colonnes))).fetchall()}
        except LectureTropLongue:
            tenues = None
    return {"colonnes": colonnes, "tenues": tenues}
