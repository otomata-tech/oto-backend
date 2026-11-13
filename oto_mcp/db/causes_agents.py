"""Le CATALOGUE des causes de panne d'agent : une empreinte rangée une fois, pour toutes
les orgs (13/11/2026).

Une ligne `agent_causes` = une empreinte (`causes_pannes.de`) : sa classe, son côté (qui
peut réparer), son statut — `proposee` (relevée par la boucle, avec la classe que la regex
lui donnait), `approuvee` (un opérateur l'a validée, avec la copie que le client lira),
`refusee` — et ses `gestes` (qui l'a décidée, quand : ajout seul). L'épisode porte la clé
de son empreinte (`agent_episodes.empreinte`) : ce qu'une cause couvre — épisodes, orgs,
classes que la regex leur a données — se LIT par jointure, rien n'est compté à la main.

⚠️ **En ombre** : le catalogue ne décide encore d'aucun geste. La boucle le remplit et dit
au bilan ce qu'il aurait décidé (`medecin_agents.balayer`) ; la regex décide.

⚠️ Une ligne s'INSÈRE une fois (`ON CONFLICT DO NOTHING`, clé texte : aucune séquence
brûlée) : un passage de la boucle ne réécrit pas le catalogue.
"""
from __future__ import annotations

import json
from typing import Any, Iterable, Optional

from ._conn import _connect

STATUTS = ("proposee", "approuvee", "refusee")

#: La colonne de l'épisode, posée par le démarrage ET la révision
#: `20261113_1830_causes_agents` : ordre indifférent.
COLONNE_EMPREINTE = "empreinte"
DDL_COLONNE_EMPREINTE = (
    "ALTER TABLE agent_episodes ADD COLUMN IF NOT EXISTS empreinte TEXT")

_COLONNES = ("cle, empreinte, classe, cote, statut, copie, gestes, vue_premiere_at")


def relever(lot: Iterable[tuple[str, str, str, str]]) -> dict[str, dict[str, Any]]:
    """Range un lot d'empreintes `(cle, empreinte, classe, cote)` vues à ce passage — une
    empreinte neuve entre `proposee` avec la classe et le côté de la regex — et rend la
    ligne du catalogue de CHAQUE clé du lot, neuve ou non, marquée `neuve`. Deux
    requêtes pour tout le passage, quelle que soit la taille du lot."""
    lignes = {c: (c, e, cl, co) for c, e, cl, co in lot}
    if not lignes:
        return {}
    cles = list(lignes)
    with _connect() as conn:
        neuves = {r["cle"] for r in conn.execute(
            """
            INSERT INTO agent_causes (cle, empreinte, classe, cote)
            SELECT * FROM unnest(%s::text[], %s::text[], %s::text[], %s::text[])
            ON CONFLICT (cle) DO NOTHING
            RETURNING cle
            """,
            ([v[0] for v in lignes.values()], [v[1] for v in lignes.values()],
             [v[2] for v in lignes.values()], [v[3] for v in lignes.values()]),
        ).fetchall()}
        rows = conn.execute(
            f"SELECT {_COLONNES} FROM agent_causes WHERE cle = ANY(%s)", (cles,)
        ).fetchall()
    return {r["cle"]: dict(r) | {"neuve": r["cle"] in neuves} for r in rows}


def lire(cle: str) -> Optional[dict[str, Any]]:
    with _connect() as conn:
        r = conn.execute(f"SELECT {_COLONNES} FROM agent_causes WHERE cle = %s",
                         (cle,)).fetchone()
    return dict(r) if r else None


def lister(*, statut: Optional[str] = None, limit: int = 50) -> list[dict[str, Any]]:
    """Les causes, avec ce qu'elles couvrent, lu sur les épisodes : `episodes`,
    `vivants`, `orgs`, `derniere_vue`, et `classes_vues` — les classes que la boucle a
    données à ces épisodes ; une cause approuvée dont les épisodes portent une autre
    classe est un DÉSACCORD entre le catalogue et la regex. Les plus récemment vues
    d'abord."""
    params: dict[str, Any] = {"limit": max(1, min(int(limit), 200)), "statut": statut}
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            f"""
            SELECT {", ".join("c." + x.strip() for x in _COLONNES.split(","))},
                   COUNT(e.id)::int AS episodes,
                   COUNT(e.id) FILTER (WHERE e.statut <> 'clos')::int AS vivants,
                   COUNT(DISTINCT e.org_id)::int AS orgs,
                   MAX(e.vu_at) AS derniere_vue,
                   COALESCE(array_agg(DISTINCT e.classe)
                            FILTER (WHERE e.classe IS NOT NULL), '{{}}') AS classes_vues
              FROM agent_causes c
              LEFT JOIN agent_episodes e ON e.empreinte = c.cle
             WHERE %(statut)s::text IS NULL OR c.statut = %(statut)s
             GROUP BY c.cle
             ORDER BY MAX(e.vu_at) DESC NULLS LAST, c.vue_premiere_at DESC
             LIMIT %(limit)s
            """, params).fetchall()]


def decider(cle: str, *, acteur: str, statut: Optional[str] = None,
            classe: Optional[str] = None, cote: Optional[str] = None,
            copie: Optional[dict] = None) -> Optional[dict[str, Any]]:
    """Le geste d'un opérateur (ou de l'agent médecin) sur une cause : son statut, sa
    classe, son côté, sa copie au client — fusionnée PAR LANGUE : poser `en` garde `fr`.
    Un champ absent ne change pas ; le geste
    s'ajoute à `gestes` avec son auteur et sa date. Rend la ligne, ou None si la clé
    n'existe pas."""
    geste = {"acteur": acteur, "statut": statut, "classe": classe, "cote": cote,
             "copie": bool(copie)}
    with _connect() as conn:
        r = conn.execute(
            f"""
            UPDATE agent_causes
               SET statut = COALESCE(%(statut)s, statut),
                   classe = COALESCE(%(classe)s, classe),
                   cote = COALESCE(%(cote)s, cote),
                   copie = CASE WHEN %(copie)s::jsonb IS NULL THEN copie
                                ELSE COALESCE(copie, '{{}}'::jsonb) || %(copie)s::jsonb END,
                   gestes = gestes || jsonb_build_array(
                       %(geste)s::jsonb || jsonb_build_object(
                           'at', to_char(NOW() AT TIME ZONE 'UTC',
                                         'YYYY-MM-DD"T"HH24:MI:SSZ')))
             WHERE cle = %(cle)s
            RETURNING {_COLONNES}
            """,
            {"statut": statut, "classe": classe, "cote": cote,
             "copie": json.dumps(copie, ensure_ascii=False) if copie else None,
             "geste": json.dumps(geste, ensure_ascii=False), "cle": cle},
        ).fetchone()
    return dict(r) if r else None
