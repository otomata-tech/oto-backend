"""Le journal des appels lu par ses filtres — et ce que le scope d'org laisse dehors (#630).

Vécu en production le 29/08/2026 : un `data_write` refusé à 21:11:23, présent dans le
déroulé de son run (`op=run`, 17 appels), absent de la vue filtrée `op=calls
org_id=226` interrogée trois fois avec des motifs que son texte contient. Rejoué contre
la base : avec `org_id=226` la vue rend 1 refus portant le nom du tableau ; sans, 40.
La condition qui l'élimine est le SCOPE de la vue — `tool_calls.org_id = 226` — parce
que l'appel a été RÉSOLU sous l'org maison de l'appelant (2), l'axe `_org` étant absent
(#631). La vue était exacte dans son périmètre ; le lecteur ne savait pas ce que ce
périmètre laissait dehors, et un « zéro » lu là était muet.

Ce module rend le plancher comparable : `count_calls_of_org_runs_elsewhere` compte les
appels des RUNS de l'org (`runs.org_id`, gelé à `run_start`) stampés sous une AUTRE org,
sous LES MÊMES filtres que la page. Pour que ce soit vrai par construction, la
construction des filtres de la page (`usage.list_tool_calls`) vit ici,
`call_filter_clauses`, et les deux l'appellent : deux constructions divergent en silence.

Mesuré sur la base de prod (29/08, 1,17 M de lignes, org de 10 057 runs) : 28 ms sur une
fenêtre d'un jour, merge join `runs` (seq scan, 10 k lignes) ⨝ `idx_tool_calls_run`.
Le coût est linéaire en fenêtre : d'où `since` OBLIGATOIRE — sans borne, le compte
parcourrait tout le journal à chaque rafraîchissement du tableau de bord (une requête
par minute, vue dans le journal REST).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from ._conn import _connect
from .lecture_bornee import lecture_d_agregat

# Les CLÉS des arguments d'un appel, triées — jamais leur contenu (#634, 30/08/2026).
#
# La liste du journal ne sélectionne pas `args` : c'est voulu, une page de 200 lignes
# n'a pas à porter 200 payloads, et le contenu est la fiche (`get_tool_call`). Mais sans
# rien, « cet appel portait-il un numéro d'entreprise ? » obligeait à ouvrir chaque
# fiche — 443 lectures de détail en douze minutes le 29/08 pour lire une clé. Les clés
# répondent à cette question sans exposer une valeur (un secret masqué à l'écriture
# reste masqué : son NOM n'a jamais été le secret).
#
# `[]` quand la ligne n'a pas d'argument (`args` NULL — `truncated_args` rend None sur un
# ensemble vide — ou `{}`) : c'est un FAIT sur l'appel, pas une vue qui manque. Le
# `CASE` évite l'erreur de `jsonb_object_keys` sur autre chose qu'un objet ; la colonne
# n'en porte jamais, mais une lecture ne doit pas dépendre de ce que l'écriture promet.
#
# La clé réservée qui DÉCLARE une coupe (#413, `calllog.truncated_args`) n'est pas un
# argument : elle est exclue des clés rendues. La coupe se lit sur la fiche.
ARGS_TRUNCATED_KEY = "_truncated"
# La clé réservée qui porte l'ÉMETTEUR DÉCLARÉ de l'appel (otomata-tech/oto#187,
# `calllog.poser_emetteur`) : `{"name", "version"}` du logiciel client, tel que sa
# session l'a déclaré à l'`initialize`. Pas un argument non plus : exclue des clés.
ARGS_CLIENT_KEY = "_client"
ARG_KEYS_SQL = (
    "COALESCE((SELECT array_agg(k ORDER BY k) FROM jsonb_object_keys("
    "CASE WHEN jsonb_typeof(l.args) = 'object' THEN l.args ELSE '{}'::jsonb END) k "
    f"WHERE k NOT IN ('{ARGS_TRUNCATED_KEY}', '{ARGS_CLIENT_KEY}')), "
    "ARRAY[]::text[])"
)

# L'émetteur d'une ligne, servi par chaque lecture du journal (liste, fiche, export
# d'audit) : le logiciel client DÉCLARÉ et le mode de jeton. `client_name` NULL = ligne
# antérieure à oto#187, ou appel hors session MCP ; `token_kind` NULL = session OAuth
# (aucun jeton nommé) — ou ligne antérieure. ⚠️ Déclaré par le client : lisible,
# jamais opposable.
EMITTER_SQL = (
    f"l.args->'{ARGS_CLIENT_KEY}'->>'name' AS client_name, "
    f"l.args->'{ARGS_CLIENT_KEY}'->>'version' AS client_version, "
    "l.token_kind"
)


def call_filter_clauses(
    *, sub: Optional[str] = None, tool_name: Optional[str] = None,
    errors_only: bool = False, since_days: Optional[int] = None,
    run_id: Optional[str] = None, session_id: Optional[str] = None,
    min_duration_ms: Optional[int] = None, error_contains: Optional[str] = None,
) -> tuple[list[str], list[Any]]:
    """Les clauses WHERE d'une lecture filtrée du journal, alias `l` — SANS le scope
    d'org ni le `kind`, que chaque lecteur pose lui-même (c'est précisément ce qui
    distingue la page de son plancher)."""
    clauses: list[str] = []
    params: list[Any] = []
    if sub:
        clauses.append("l.sub = %s")
        params.append(sub)
    if tool_name:
        clauses.append("l.tool = %s")
        params.append(tool_name)
    if errors_only:
        clauses.append("l.ok = FALSE")
    if since_days is not None:
        clauses.append("l.created_at >= NOW() - make_interval(days => %s)")
        params.append(int(since_days))
    if run_id:
        clauses.append("l.run_id = %s")
        params.append(run_id)
    if session_id:
        clauses.append("l.session_id = %s")
        params.append(session_id)
    if min_duration_ms is not None:
        clauses.append("l.duration_ms >= %s")
        params.append(int(min_duration_ms))
    if error_contains:
        clauses.append("l.error ILIKE %s")
        params.append(f"%{error_contains}%")
    return clauses, params


def count_org_mcp_calls(org_id: int, *, since: datetime) -> int:
    """Appels d'OUTIL D'AGENT émis sous `org_id` depuis `since`. Le compteur d'usage.

    Deux choix qui font tout, et qu'il ne faut pas défaire :

    - **`kind = 'mcp'`** : on compte ce qu'un agent exécute, pas la navigation dans le
      tableau de bord (`kind='rest'`) ni les handshakes (`'protocol'`). Les deux
      volumes n'ont rien à voir — mélangés, le compteur mesurerait surtout le fait
      d'avoir un onglet ouvert.
    - **`l.org_id`**, le rattachement RÉEL posé par le seam `current_org` au moment de
      l'appel. Jamais un préfixe de nom d'outil : un raccourci de ce genre a déjà
      produit un faux résultat (les noms d'outils ne portent pas l'org, et un tenant
      peut même les voir préfixés autrement, cf. `tool_alias`).

    ⚠️ Plancher connu, hérité du scope d'org (cf. l'en-tête de ce module) : un appel
    d'un run de l'org résolu sous une AUTRE org n'est pas compté ici. Le compteur
    sous-estime donc plutôt qu'il ne surestime — ce qui est le bon sens de l'erreur
    pour un chiffre montré à quelqu'un, et l'écart se mesure avec
    `count_calls_of_org_runs_elsewhere`.

    `since` OBLIGATOIRE, même raison que ci-dessous : le coût est linéaire en fenêtre.

    **Lu sur les totaux par jour** (`journal_jour.source`, `kind='mcp'`, filtre
    `org_id`) : les jours clos consolidés, plus le journal direct pour le reste (la
    veille avant la maintenance, le jour courant). Le compte direct d'un mois relisait
    au heap toutes les lignes du mois de l'org, REST du tableau de bord comprises —
    jusqu'au `statement_timeout` de la fiche d'org, qui l'appelle à chaque vue. Un jour
    clos manquant au registre lève `journal_jour.AgregatIncomplet`.
    """
    # Import différé : `journal_jour` importe ce module à son import.
    from . import journal_jour
    objet = "usage inclus d'une org"
    with lecture_d_agregat(objet) as conn:
        src, params = journal_jour.source(
            conn, objet, kinds=("mcp",), mesures=("appels",),
            filtres={"org_id": int(org_id)}, depuis=since)
        row = conn.execute(
            f"SELECT COALESCE(sum(s.appels), 0)::bigint AS n FROM ({src}) s",
            params).fetchone()
    return int(row["n"]) if row else 0


def count_calls_of_org_runs_elsewhere(org_id: int, *, since: datetime,
                                      **filters: Any) -> int:
    """Combien d'appels des runs de `org_id` ont été résolus sous une autre org depuis
    `since`, sous les mêmes filtres que la page (`call_filter_clauses`).

    « Autre org » inclut l'appel sans org (`org_id NULL`, `IS DISTINCT FROM`). Un run
    d'une autre org ne compte jamais, même si l'appelant est membre ici : le scope de la
    vue d'org, c'est ce qui a été émis SOUS l'org — le plancher garde la même règle, il
    la complète par la seule chose que l'org possède aussi : ses runs."""
    clauses, params = call_filter_clauses(**filters)
    clauses = ["l.kind = 'mcp'", "r.org_id = %s", "l.org_id IS DISTINCT FROM %s",
               "l.created_at >= %s", *clauses]
    params = [int(org_id), int(org_id), since, *params]
    # Bornée comme la page qu'il complète (infra#9) : le même journal, la même fenêtre.
    with lecture_d_agregat("appels des runs de l'org résolus ailleurs") as conn:
        row = conn.execute(
            f"""
            SELECT count(*) AS n
            FROM tool_calls l JOIN runs r ON r.run_id = l.run_id
            WHERE {" AND ".join(clauses)}
            """,
            tuple(params),
        ).fetchone()
    return int(row["n"]) if row else 0
