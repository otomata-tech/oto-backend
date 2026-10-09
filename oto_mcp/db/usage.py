"""Boucle d'usage : compteurs, journal d'appels MCP, runs/déroulés, signaux, projections, prune.

Extrait de l'ex-monolithe `db.py` (barreau final). Fonctions de domaine — la
plomberie est dans `_conn`. Ré-exporté par `db/__init__`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
from datetime import date, datetime, timezone
from typing import Any, Iterator, NamedTuple, Optional

import psycopg

logger = logging.getLogger(__name__)

from .. import deprecations
from . import journal_calls, journal_jour
from ._conn import _connect
from .index_releve import PREDICAT_OUVERTURE
from .lecture_bornee import lecture_d_agregat



def _agregat(objet: str, **kw):
    """La connexion d'une lecture d'agrégat de ce module, bornée
    (`lecture_bornee.lecture_d_agregat`, #1145). Un seul point d'entrée : un banc qui
    lit le SQL de ces lectures sur une connexion simulée remplace CE nom, et la borne
    elle-même se juge dans `tests/test_lecture_bornee.py`."""
    return lecture_d_agregat(objet, **kw)

def increment_usage(sub: str, tool: str, amount: int = 1) -> int:
    """Incrémente le compteur (sub, tool, today) de `amount`. Retourne la nouvelle valeur.

    `amount` existe pour qu'un appel qui consomme N unités coûte UNE requête et non N.
    Sans lui, l'appelant n'avait d'autre choix que de boucler : un bulk de 100 contacts
    prenait 100 connexions du pool et 100 transactions, sur le chemin chaud d'un serveur
    mono-loop — et un recensement Maps métré en crédits Serper en aurait pris 81, jusqu'à
    2 000 sur une grille dense. Le compteur écrit vaut exactement la même chose qu'après
    N incréments : c'est la même somme, en une écriture.

    `EXCLUDED.count` reprend le pas de l'INSERT tenté, ce qui évite de passer `amount`
    deux fois. Borné à 1 au minimum : un pas nul ou négatif n'a pas de sens ici, et
    laisserait un appel réussi ne rien débiter."""
    amount = max(1, int(amount))
    with _connect() as conn:
        row = conn.execute(
            """
            INSERT INTO usage (sub, tool, day, count)
            VALUES (%s, %s, CURRENT_DATE, %s)
            ON CONFLICT(sub, tool, day) DO UPDATE SET count = usage.count + EXCLUDED.count
            RETURNING count
            """,
            (sub, tool, amount),
        ).fetchone()
        return int(row["count"]) if row else 0


def insert_tool_call(row: dict) -> None:
    """Sink calllog (middleware inliné oto_mcp/calllog.py) : insère un row canonique (server, sub, email, tool,
    args, ok, error, duration_ms) + corrélation OTO-LOCALE (session_id, run_id ;
    ADR 0017, absents du contrat canonique → enrichis par le sink). `kind` discrimine
    l'événement ('mcp' défaut / 'rest' / 'connector', ADR 0017 « un seul flux »).
    Best-effort côté middleware — jamais bloquant."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO tool_calls
                (server, kind, sub, email, tool, args, ok, error, duration_ms, session_id,
                 run_id, org_id, client_id, sentry_event_id,
                 request_id, call_uid, effective_sub, error_kind,
                 token_id, token_kind, result_size, result_shape, quantity, key_mode,
                 view_as_sub)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                row.get("server") or "oto", row.get("kind") or "mcp",
                row.get("sub"), row.get("email"),
                row["tool"], json.dumps(row.get("args")) if row.get("args") is not None else None,
                bool(row.get("ok")), row.get("error"), row.get("duration_ms"),
                row.get("session_id"), row.get("run_id"), row.get("org_id"),
                row.get("client_id"), row.get("sentry_event_id"),
                # #117 — discriminant par appel. `request_id` et `effective_sub` sont
                # absents des gestes REST ; `call_uid` y porte le geste de la requête
                # (oto#273), NULL hors capacité. La ligne les porte à NULL, sans branche.
                row.get("request_id"), row.get("call_uid"), row.get("effective_sub"),
                # oto#25 lot (b1) — résultat de la taxonomie sur échec, NULL sur
                # succès et sur les gestes REST (calllog._error_kind ne les touche pas).
                row.get("error_kind"),
                row.get("token_id"), row.get("token_kind"),
                # oto-backend#340 — taille du texte servi. NULL partout où elle n'a
                # pas été mesurée : les échecs (le middleware ne la calcule que sur le
                # chemin heureux) et les gestes REST, qui ne passent pas par lui.
                row.get("result_size"),
                # oto-backend#644 — forme du résultat servi, même point d'écriture et
                # même règle de NULL que la taille.
                row.get("result_shape"),
                # Métrage par unité (facturation du partenaire) — NULL = non tracé pour ce
                # tool, un consommateur doit le traiter comme 1, pas 0.
                row.get("quantity"),
                # Mode du credential (facturation du partenaire) — NULL = non attribuable,
                # donc non facturable ; l'inverse de la règle de `quantity`.
                row.get("key_mode"),
                # #572 point 4 — cible du « voir en tant que » (REST uniquement,
                # `RestCallLogger` seul la pose). NULL = pas de consultation en
                # cours, ou ligne antérieure à cette colonne (non reconstructible).
                row.get("view_as_sub"),
            ),
        )


# ── Le run : UNE source, et c'est le JOURNAL ─────────────────────────────────
#
# Verdict du 12/08 (chantier du run, arbitrage J-a ; #289) : **la table `runs` n'est
# pas le run**. Un run est un ASSEMBLAGE À LA LECTURE (ADR 0058-D2) — le journal des
# appels porte le déroulé, les sorties sont des nœuds de l'arbre de contenu, et la
# page de run ne se stocke jamais. La ligne `runs` n'est au mieux qu'un **index**.
#
# Ce que ça ferme : deux reconstructions concurrentes du MÊME objet cohabitaient — la
# table (bloc C du handshake, pastille de procédure, activité datastore) et le journal
# (lentilles admin et org). Elles peuvent rendre deux issues différentes du même run :
# `finish_run` est un UPDATE qui NO-OPE quand la ligne n'existe pas (`_persist_open`
# est best-effort) ou quand le `sub` ne matche pas, pendant que le fait `run_finish`,
# lui, est toujours journalisé. La table peut donc annoncer « en cours » ce que le
# journal a clos — et l'inverse ne peut plus arriver.
#
# CE QUI RESTE CRÉDIBLE DANS LA TABLE, champ par champ :
#   • `run_id`     — la clé de jointure. C'est la seule raison de lire cette table.
#   • `project_id` — le SEUL fait qu'aucune ligne de journal ne porte (`tool_calls`
#                    n'a pas de colonne projet) : c'est ce qui garde la table en vie.
#   • `sub`, `org_id` — redondants avec la ligne `run_start` (mêmes seams à l'écriture,
#                    `current_user_sub_from_token` / `current_org`) ; ils servent
#                    `idx_runs_sub_org`, ils ne sont jamais RENDUS.
#   • `label`, `doctrine`, `outcome`, `note`, `started_at`, `finished_at` — DOUBLONS
#                    **non autoritatifs**. Ils continuent d'être écrits (retirer les
#                    colonnes est une migration, pas ce lot), et plus aucun lecteur ne
#                    les lit : dès qu'ils divergent du journal, c'est le journal qui
#                    dit vrai.
#
# D'où les deux fragments ci-dessous : ils sont la SEULE façon de lire un run. Rejoindre
# `runs` ailleurs pour un label, un guide ou une issue, c'est rouvrir la 2ᵉ vérité.


def _run_closure(start: str = "s") -> str:
    """LATERAL de la CLÔTURE d'un run, lue du journal (la ligne `run_finish`).

    Trois prédicats, un mode d'erreur chacun :
    - `args->>'run_id'` est le lien qui existe pour TOUTES les lignes. `_run_id=` n'est
      pas advertisé sur `run_finish` (`call_axes._is_run_correlatable_tool` exclut les
      verbes de run), donc la colonne `tool_calls.run_id` d'une clôture ne se remplit
      que depuis le stamp que `run_finish` pose lui-même : l'historique de la fenêtre
      de rétention ne l'a pas, et se fier à la colonne perdrait ces clôtures-là ;
    - `created_at >= {start}.created_at` : une clôture ne précède pas son ouverture
      (et le prédicat borne le parcours) ;
    - `sub IS NOT DISTINCT FROM {start}.sub` : la MÊME règle que `finish_run`, qui scope
      sa clôture au propriétaire. Sans elle, un `run_finish` tapé par un tiers sur un
      run_id deviné donnerait au journal une issue que la table refuse — c'est-à-dire
      très exactement la deuxième vérité qu'on est en train de fermer.
    La DERNIÈRE clôture gagne : un agent peut rejouer `run_finish`.

    ⚠️ Chercher par `args->>'run_id'` n'est indexable que par EXPRESSION :
    `idx_tool_calls_run_finish_ref` (`_init.py`) est ce qui rend ce LATERAL
    exécutable — sans lui il parcourt le journal ENTIER à chaque run, et
    l'incident du 2026-08-27 (185 s de boucle tenue) revient tel quel."""
    return f"""
            LEFT JOIN LATERAL (
                SELECT created_at, args
                  FROM tool_calls
                 WHERE tool = 'run_finish'
                   AND args->>'run_id' = {start}.run_id
                   AND created_at >= {start}.created_at
                   AND sub IS NOT DISTINCT FROM {start}.sub
                 ORDER BY created_at DESC
                 LIMIT 1
            ) f ON TRUE"""


# La clé d'`args` sous laquelle un fait `run_start` nomme la procédure déroulée.
# Nommée plutôt qu'écrite deux fois : elle est SERVIE (elle voyage dans le journal,
# des lignes vieilles de trente jours la portent), donc elle ne se renomme pas d'un
# côté sans l'autre — et le vocabulaire du produit, lui, dit « guide » ou
# « procédure » (ADR 0042, cf. tests/test_vocabulaire_guide.py).
_ARG_PROCEDURE = "doctrine"
# Ce qu'`instruction_usage` accepte comme clé de filtre. Fermée, et lue nulle part
# ailleurs : la valeur atterrit dans du SQL interpolé.
_ARGS_PROCEDURE_OK = ("slug", _ARG_PROCEDURE)
# Les verbes que l'usage d'une procédure lit, chacun avec LA clé d'`args` qui y nomme
# la procédure (oto-backend#1146) : un chargement (`oto_procedure`) la nomme `slug`, un
# déroulé (`run_start`) `_ARG_PROCEDURE`. Liste FERMÉE de couples : la clé est
# interpolée dans le SQL, et un verbe lu sous la clé d'un autre rendrait zéro en silence.
_VERBES_USAGE = {"oto_procedure": "slug", "run_start": _ARG_PROCEDURE}


def _runs_from_journal(extra: str = "") -> str:
    """Le run RECONSTRUIT depuis ses faits : l'ouverture (`run_start`) porte label,
    guide, acteur, org et date de début ; la clôture porte l'issue et la date de fin.
    `outcome` NULL = pas de fait de clôture = run ouvert.

    `last_seen_at` = le dernier signe de vie du run (son appel le plus récent, à
    défaut son ouverture). C'est ce qui permet de distinguer un travail EN COURS d'une
    conversation partie sans clore : sans lui, « pas d'issue » s'affichait « en cours »
    jusqu'à la fin des temps. Dérivé ici plutôt que dans chaque surface — la
    dérivation elle-même vit dans `run_status`, et toutes les lentilles en héritent.
    L'index `idx_tool_calls_run` sert le LATERAL.

    `extra` = prédicats supplémentaires sur l'alias `s` (la ligne d'ouverture),
    TOUJOURS des littéraux de ce module — jamais une entrée d'appelant."""
    return f"""
            SELECT s.run_id, s.sub, s.org_id,
                   s.args->>'label'             AS label,
                   s.args->>'{_ARG_PROCEDURE}'          AS doctrine,
                   s.args->>'doctrine_version'  AS doctrine_version,
                   s.created_at                 AS started_at,
                   f.created_at                 AS finished_at,
                   f.args->>'outcome'           AS outcome,
                   GREATEST(s.created_at,
                            COALESCE(v.last_call_at, s.created_at)) AS last_seen_at
              FROM tool_calls s{_run_closure("s")}
              LEFT JOIN LATERAL (
                  SELECT max(c.created_at) AS last_call_at
                    FROM tool_calls c WHERE c.run_id = s.run_id
              ) v ON TRUE
             WHERE s.tool = 'run_start' AND s.run_id IS NOT NULL{extra}"""


def _derniers_runs(portee: str = "") -> str:
    """Prédicat d'ouverture (`extra` de `_runs_from_journal`) qui ne garde que les `%s`
    DERNIÈRES ouvertures de la portée, choisies AVANT toute reconstruction (infra#9).

    Sans lui, une liste « les N derniers runs » reconstruisait TOUS les runs de sa
    portée — la clôture et le dernier signe de vie de chacun, deux LATERAL par run —
    puis n'en gardait que N : le `LIMIT` du SELECT extérieur ne descend pas sous les
    LATERAL. Le coût suivait l'historique de la portée (les ouvertures ne sont jamais
    archivées), pas la page demandée. Ici, la page se choisit dans le journal, sur les
    seules lignes `run_start`, et seules ces N-là se reconstruisent : même résultat
    (même prédicat d'ouverture, même ordre), sans repli sur la table `runs` — un run
    sans ligne d'index reste listé.

    `portee` = prédicats sur l'alias `d` (l'ouverture candidate), TOUJOURS des
    littéraux de ce module ; ses `%s` précèdent celui du `LIMIT`. ⚠️ Des ÉGALITÉS (ou
    `IS NULL`), jamais `IS NOT DISTINCT FROM` : c'est ce qui laisse les index
    `index_releve.OUVERTURES` (révision 0049) servir la page dans son ordre, par un
    parcours d'index seul arrêté au `LIMIT`. Leur prédicat est `PREDICAT_OUVERTURE`."""
    ouverture = " AND ".join(f"d.{c.strip()}" for c in PREDICAT_OUVERTURE.split(" AND "))
    return ("\n               AND s.id IN (SELECT d.id FROM tool_calls d"
            f" WHERE {ouverture}" + portee +
            " ORDER BY d.created_at DESC, d.id DESC LIMIT %s)")


def insert_run(
    run_id: str, *, sub: Optional[str], org_id: Optional[int], label: str,
    guide: Optional[str] = None, project_id: Optional[int] = None,
) -> None:
    """Pose l'INDEX d'un run (best-effort, idempotent sur `run_id`).

    ⚠️ Ce n'est pas « persister le run » : le run est ses faits (cf. le bloc ci-dessus).
    Cette ligne existe pour porter `project_id` — le projet actif gelé à l'ouverture
    (ADR 0032 §5/§6, B3), qu'aucune colonne de `tool_calls` ne porte ; NULL hors projet.
    `label`/`doctrine` y sont écrits par héritage et ne sont plus lus.

    `lignes_reservees` part à 0 : un run né après la mesure est MESURÉ dès sa naissance,
    et la file l'incrémente à chaque ligne rendue (`rowlock.datastore_claim_next`)."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO runs (run_id, sub, org_id, project_id, label, doctrine, "
            "lignes_reservees) "
            "VALUES (%s, %s, %s, %s, %s, %s, 0) ON CONFLICT (run_id) DO NOTHING",
            (run_id, sub, org_id, project_id, label, guide),
        )


def finish_run(run_id: str, outcome: str, note: Optional[str] = None,
               sub: Optional[str] = None) -> None:
    """Marque la clôture sur l'INDEX (outcome + note + finished_at).

    ⚠️ Écriture de confort, **jamais lue** : l'issue d'un run se lit du fait `run_finish`
    (`_runs_from_journal`). No-op si run_id inconnu (index jamais posé, ou déjà prune) —
    c'était précisément la source de la divergence : l'UPDATE rate en silence, le fait,
    lui, est écrit. `sub` (≠ None) SCOPE la clôture au propriétaire (un run_id d'autrui,
    #108, n'est pas clôturable) ; `sub=None` (stdio local) matche les runs sans sub. La
    reconstruction applique la MÊME règle de propriété."""
    with _connect() as conn:
        conn.execute(
            "UPDATE runs SET outcome = %s, note = %s, finished_at = NOW() "
            "WHERE run_id = %s AND sub IS NOT DISTINCT FROM %s",
            (outcome, note, run_id, sub),
        )


def run_closed_at(run_id: str) -> Optional[datetime]:
    """Quand ce run a été CLOS, ou `None` — encore ouvert, ou inconnu du journal.

    Une seule question : celle du titre. ⚠️ **Plus aucun appelant depuis le
    07/09/2026** — elle en avait un seul, le refus de `@claimed`, qui décrivait un ÉTAT
    (« aucune réservation active ») là où le problème était un MOMENT : l'appel arrivait
    APRÈS `run_finish`, qui avait libéré les baux (#645). Le pronom retiré, ce refus
    n'existe plus. La fonction reste, et c'est dit ici plutôt que corrigé en la
    supprimant : « quand ce run a-t-il été clos » est une question du journal, sa réponse
    se lit du FAIT et non d'une colonne de confort (ci-dessous), et une lecture juste qui
    se voit se redemande — une lecture retirée se réécrit de travers.

    ⚠️ Lu du FAIT (`run_finish`), jamais de `runs.finished_at` : cette colonne est une
    écriture de confort que `finish_run` rate **en silence** quand l'index n'a pas été
    posé (cf. le bloc ci-dessus, et `_run_closure`). Un refus qui annoncerait une
    clôture d'après une colonne manquée mentirait exactement dans le cas qu'il est
    censé expliquer — la faute qu'on est en train de corriger, d'un cran plus bas.
    D'où la réutilisation de `_run_closure` : ses trois prédicats (corrélation par
    `args`, clôture postérieure à l'ouverture, propriété du `sub`) valent ici tels
    quels, et une seconde formulation en serait une seconde vérité.

    `None` couvre « ouvert » ET « jamais vu » : les deux se disent « pas clos », et
    affirmer une clôture qu'on n'a pas lue serait pire que se taire. Chemin d'ÉCHEC
    seulement (le nominal résout un bail sans passer ici), deux prédicats indexés —
    `idx_tool_calls_run` sur l'ouverture, `idx_tool_calls_run_finish_ref` sur le
    LATERAL de clôture.
    """
    with _connect() as conn:
        row = conn.execute(
            f"""
            SELECT f.created_at AS finished_at
              FROM tool_calls s{_run_closure("s")}
             WHERE s.tool = 'run_start' AND s.run_id = %s
             ORDER BY s.created_at DESC
             LIMIT 1
            """,
            (run_id,),
        ).fetchone()
    return row["finished_at"] if row else None


def run_pour_en_tete(run_id: str, sub: str) -> Optional[dict]:
    """Ce qu'un en-tête REST `X-Oto-Run` doit savoir de son run — en UNE requête (oto#227).

    `None` : le run est inconnu de `runs`. Sinon `{sub, org_id, org_role, role, clos}` :
    le PROPRIÉTAIRE et l'org du run (index `runs`), le rôle du porteur dans cette org
    (`org_members`, ce que lit `org_store.get_org_role`), son rôle plateforme (`users`,
    l'escalade super_admin de `roles.effective_org_role`) et la CLÔTURE — lue du fait
    `run_finish` par `_run_closure`, jamais de `runs.finished_at`.

    ⚠️ Une requête et pas quatre : ce contrôle se paie sur CHAQUE requête REST qui porte
    l'en-tête (réserver, écrire, libérer), et `get_run_head` + rôle d'org + rôle
    plateforme + `run_closed_at` en coûtaient quatre. Bornée au run : `runs` par sa clé,
    l'ouverture par `idx_tool_calls_run`, la clôture par `idx_tool_calls_run_finish_ref`.
    Le JUGEMENT (propriétaire, membre, clos) reste à l'appelant, avec les règles de
    `roles` — un banc confronte les deux."""
    with _connect() as conn:
        row = conn.execute(
            f"""
            SELECT r.sub, r.org_id, u.role,
                   -- Le compte de service d'une org est `org_member` de la sienne sans
                   -- ligne de membre (`org_store.members`, même dérivation).
                   COALESCE(m.org_role, CASE WHEN EXISTS (
                       SELECT 1 FROM org_service_accounts sa
                        WHERE sa.org_id = r.org_id AND sa.sub = %s)
                       THEN 'org_member' END) AS org_role,
                   (f.created_at IS NOT NULL) AS clos
              FROM runs r
              LEFT JOIN org_members m ON m.org_id = r.org_id AND m.sub = %s
              LEFT JOIN users u ON u.sub = %s
              LEFT JOIN LATERAL (
                  SELECT o.run_id, o.created_at, o.sub
                    FROM tool_calls o
                   WHERE o.tool = 'run_start' AND o.run_id = r.run_id
                   ORDER BY o.created_at DESC
                   LIMIT 1
              ) s ON TRUE{_run_closure("s")}
             WHERE r.run_id = %s
            """,
            (sub, sub, sub, run_id),
        ).fetchone()
    return dict(row) if row else None


def recent_runs(sub: str, org_id: Optional[int], limit: int = 5) -> list[dict]:
    """Les `limit` derniers runs d'un (sub, org), plus récent d'abord — l'anticipation
    du contexte injecté (#50 bloc C) + la boucle d'usage.

    Lus du JOURNAL (le run est ses faits) ; la table n'est jointe que pour `project_id`,
    le seul champ qu'elle sait. Conséquence assumée : un run dont l'ouverture n'a pas
    été journalisée n'apparaît plus au handshake — mieux qu'une étiquette sans déroulé.

    Seules les `limit` dernières ouvertures se reconstruisent (`_derniers_runs`,
    infra#9) : lu à chaque session, ce bloc reconstruisait tous les runs du compte."""
    if org_id is None:
        portee, params = " AND d.sub = %s AND d.org_id IS NULL", (sub, limit, limit)
    else:
        portee, params = " AND d.sub = %s AND d.org_id = %s", (sub, org_id, limit, limit)
    with _connect() as conn:
        rows = conn.execute(
            f"""
            WITH j AS ({_runs_from_journal(_derniers_runs(portee))})
            SELECT j.run_id, j.label, j.doctrine, j.outcome, x.project_id,
                   j.started_at, j.finished_at, j.last_seen_at
              FROM j LEFT JOIN runs x ON x.run_id = j.run_id
             ORDER BY j.started_at DESC LIMIT %s
            """,
            params,
        ).fetchall()
    return list(rows)


def my_runs(sub: str, limit: int = 20, *, open_only: bool = False) -> list[dict]:
    """MES déroulés, avec leur `run_id` — de quoi refermer ce qu'on a ouvert (#473).

    Un agent qui perd le fil ne peut plus le clore : `run_finish` exige un `run_id`
    que rien ne lui rend. Le bloc de contexte annonce bien « derniers déroulés », mais
    par leur INTITULÉ ; `list_runs` porte l'id et reste réservé aux lentilles
    d'opérateur (plateforme, ou org_admin) ; et un run ouvert HORS projet n'est
    énumérable nulle part. Un déroulé sans identifiant reste donc ouvert pour
    toujours — et c'est le régime dominant, pas le cas rare.

    ⚠️ **Volontairement PAS scopé à une org**, à la différence de `recent_runs` et de
    `list_runs`. Un run s'ouvre dans l'org active, et l'agent en change en cours de
    route : borner à l'org courante rendrait inaccessible exactement le run qu'on ne
    retrouve plus. Le scope de propriété, lui, est dur — `s.sub = %s`, la MÊME règle
    que `finish_run` : on ne liste que ce qu'on aurait le droit de clore, donc lister
    n'ouvre aucun accès qui n'existait pas.

    `open_only` = les runs sans fait de clôture (`outcome IS NULL`), c'est-à-dire ceux
    qui restent à refermer. Le silence (24 h depuis #666) n'est PAS filtré ici : il se
    dérive à la lecture (`run_status`), et un run muet est justement un run à clore.
    """
    limit = max(1, min(int(limit), 200))
    # Le filtre porte sur la COLONNE DÉRIVÉE de la CTE (`j.outcome`), jamais sur les
    # alias internes de `_runs_from_journal` : son `extra` est contractuellement un
    # prédicat sur l'ouverture (`s`), et s'y glisser un prédicat sur la clôture ferait
    # dépendre cette lecture de la forme interne d'un helper partagé.
    ouverts = "\n             WHERE j.outcome IS NULL" if open_only else ""
    with _connect() as conn:
        rows = conn.execute(
            f"""
            WITH j AS ({_runs_from_journal(" AND s.sub = %s")})
            SELECT j.run_id, j.label, j.doctrine, j.doctrine_version, j.org_id,
                   x.project_id, j.started_at, j.finished_at, j.outcome, j.last_seen_at
              FROM j LEFT JOIN runs x ON x.run_id = j.run_id{ouverts}
             ORDER BY j.started_at DESC LIMIT %s
            """,
            (sub, limit),
        ).fetchall()
    return list(rows)


#: Les lectures dérivées des runs d'UN projet ne portent que sur ses
#: `PROJET_RUNS_RECENTS` derniers runs (oto-backend#1145). Mesuré en production : un
#: projet porte environ 86 000 runs, et reconstruire chacun depuis le journal lisait
#: plus d'un million de lignes (`idx_tool_calls_run`) à chaque ouverture du projet.
#: Le coût suit désormais cette borne, plus l'historique du projet.
PROJET_RUNS_RECENTS = 500

_PROJECT_SCOPE = (
    " AND s.run_id IN (SELECT run_id FROM runs WHERE project_id = %s"
    " ORDER BY started_at DESC LIMIT %s)")
"""Prédicat d'ouverture qui borne `_runs_from_journal` aux runs RÉCENTS d'UN projet.

⚠️ Le `WHERE x.project_id` du SELECT extérieur ne suffit pas : il filtre le RÉSULTAT
d'un CTE qui a déjà reconstruit **tous** les runs de la plateforme — un LATERAL
`max(created_at)` par `run_start`, plus la clôture, sur les ~900 k lignes du journal.
Le coût suivait donc le journal entier, pas le projet : incident du 2026-08-27, où
chaque `oto_project` tenait la boucle 185 s (les appels DB de ce module sont
synchrones, cf. `docs/event-loop-perf.md`) et gelait la plateforme entière —
tenants tiers compris. Poussé dans le CTE, le semi-join part d'`idx_runs_project`.

⚠️ Et borné aux `PROJET_RUNS_RECENTS` derniers (#1145) : poussé seul, il
reconstruisait encore TOUS les runs du projet — 86 000 pour le plus gros.

Littéral de ce module, comme tout `extra` (les `%s` sont liés, jamais interpolés)."""


def project_run_tools(project_id: int, limit: int = 200) -> list[str]:
    """Outils réellement APPELÉS par les runs RÉCENTS d'un projet — la part « usage
    observé » de l'inventaire dérivé (ADR 0035 B4 : surface d'un projet = refs des
    procédures liées ∪ slots×bindings ∪ runs). Distincts, plus fréquents d'abord ; brut
    (spine/méta inclus — le consommateur cure).

    Les `PROJET_RUNS_RECENTS` derniers runs seulement (#1145) : tous les appels de tous
    les runs, c'était 1,36 M lignes lues pour le plus gros projet. Un outil que seuls
    des runs plus anciens ont appelé ne figure plus dans la suggestion.

    Seul usage LÉGITIME de la table `runs` en jointure : on ne lui demande que
    `project_id` (son unique champ crédible), la matière vient du journal."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT tc.tool, count(*) AS n FROM tool_calls tc "
            "JOIN (SELECT run_id FROM runs WHERE project_id = %s "
            "      ORDER BY started_at DESC LIMIT %s) r ON r.run_id = tc.run_id "
            "WHERE tc.kind = 'mcp' "
            "GROUP BY tc.tool ORDER BY n DESC, tc.tool LIMIT %s",
            (project_id, PROJET_RUNS_RECENTS, limit),
        ).fetchall()
    return [r["tool"] for r in rows]


def project_runs(project_id: int, guide: Optional[str] = None,
                 limit: int = 20) -> list[dict]:
    """Derniers runs d'un projet (plus récent d'abord), optionnellement filtrés sur une
    `doctrine` (slug) — alimente la pastille ok/échec du viewer de procédure (refonte UX,
    ADR 0032/0017). `outcome` NULL = run ouvert / non clôturé.

    L'axe PROJET vient de l'index (`runs.project_id`), tout le reste du journal — le
    filtre `doctrine` inclus : filtrer sur la colonne de la table ferait apparaître dans
    la pastille d'une procédure un run que le journal rattache à une autre. Parmi les
    `PROJET_RUNS_RECENTS` derniers runs du projet (#1145) : une procédure qu'aucun
    d'eux n'a déroulée rend une liste vide."""
    guide_clause = " AND j.doctrine = %s" if guide is not None else ""
    params: list = ([project_id, PROJET_RUNS_RECENTS, project_id]
                    + ([guide] if guide is not None else []) + [limit])
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            f"""
            WITH j AS ({_runs_from_journal(_PROJECT_SCOPE)})
            SELECT j.run_id, j.label, j.doctrine, j.outcome, j.started_at,
                   j.finished_at, j.last_seen_at
              FROM runs x JOIN j ON j.run_id = x.run_id
             WHERE x.project_id = %s{guide_clause}
             ORDER BY j.started_at DESC LIMIT %s
            """,
            tuple(params),
        ).fetchall()]


def project_run_stats(project_id: int) -> dict:
    """Nombre de runs d'un projet + slugs de guides déroulés (distincts) — sert
    l'inertie de l'audit de liens (ADR 0035 B5 : procédure liée jamais déroulée).

    Compte les ouvertures JOURNALISÉES (`run_start`) des `PROJET_RUNS_RECENTS` derniers
    runs du projet (#1145) — un index sans déroulé journalisé ne compte pas, la question
    posée étant « cette procédure a-t-elle SERVI » ; `runs` vaut donc au plus cette
    borne, et une procédure déroulée seulement avant elle se lit inerte. Ni la clôture
    ni le dernier signe de vie ne servent ici : la lecture ne reconstruit pas le run
    (`_runs_from_journal`), elle lit son ouverture — deux LATERAL de moins par run."""
    with _connect() as conn:
        row = conn.execute(
            f"""
            SELECT count(*) AS n,
                   array_agg(DISTINCT s.args->>'{_ARG_PROCEDURE}')
                       FILTER (WHERE s.args->>'{_ARG_PROCEDURE}' IS NOT NULL) AS doctrines
              FROM tool_calls s
             WHERE s.tool = 'run_start' AND s.run_id IS NOT NULL{_PROJECT_SCOPE}
            """,
            (project_id, PROJET_RUNS_RECENTS),
        ).fetchone()
    return {"runs": int(row["n"] or 0), "doctrines": list(row["doctrines"] or [])}


#: Fenêtre pendant laquelle un signalement STRICTEMENT identique du même auteur
#: est tenu pour un REJEU, pas pour une seconde occurrence. Dix minutes : un rejeu
#: se produit dans la seconde ou la minute (coupure réseau, réponse non vue,
#: relance d'un pas de procédure), tandis qu'un même défaut qui se reproduit
#: vraiment ne se raconte pas deux fois mot pour mot en si peu de temps.
_REJEU_SIGNAL_MINUTES = 10


class DepotSignal(NamedTuple):
    """Ce que le dépôt d'un signal a produit.

    - `deja` : un REJEU — le même texte, du même auteur, sur la même clé, il y a moins
      de `_REJEU_SIGNAL_MINUTES` minutes. Rien n'a été écrit ; `id` est l'existant.
    - `rattache` : une NOUVELLE occurrence d'un signal de même clé encore en attente
      d'arbitrage. Écrite dans `usage_signal_occurrences` ; `id` est ce signal-là.
    - `precedent` : la clé avait un signal CLOS (resolved | declined) et aucun en
      attente — le problème revient. Un signal neuf est créé ; `precedent` décrit le
      dernier clos (`id`, `status`, `resolution`).
    `status`, `resolution` et `occurrences` décrivent le signal `id` tel qu'il est
    APRÈS le dépôt."""
    id: int
    deja: bool = False
    rattache: bool = False
    status: str = "open"
    resolution: Optional[str] = None
    occurrences: int = 1
    precedent: Optional[dict] = None


# La CLÉ d'un sujet : même org, même type, même cible. Le genre (`kind`) n'en est pas :
# deux agents qui butent sur la même valeur absente la disent l'un « bug », l'autre
# « wrong_result » — c'est le même sujet, et chaque occurrence garde son genre.
_CLE_SIGNAL = """
       org_id IS NOT DISTINCT FROM %(org_id)s
   AND signal = %(signal)s
   AND lower(btrim(target)) = lower(btrim(%(target)s))
"""


def insert_usage_signal(
    *, sub: Optional[str], org_id: Optional[int], signal: str, kind: str,
    target: Optional[str], body: Optional[str], session_id: Optional[str],
    source: str = "agent",
) -> DepotSignal:
    """Dépose un signalement → `DepotSignal`.

    **Un sujet en attente ne se redépose pas, il se RATTACHE.** Un signal de même org,
    même type et même cible, déposé alors qu'un signal de cette clé attend un arbitrage
    (open | acknowledged), devient une occurrence de celui-ci
    (`usage_signal_occurrences`) — mesuré le 06/10/2026 : une quarantaine de redites
    sur 338 signaux en attente, la même valeur absente déposée douze fois. Rien n'est
    perdu : chaque occurrence garde son auteur, sa session, son genre et son texte ; la
    pile, elle, compte des sujets.

    ⚠️ **Un sujet CLOS qui revient crée un signal neuf** : une régression doit se voir,
    pas s'enterrer sous un arbitrage déjà rendu. Le dépôt décrit alors le dernier clos
    (`precedent`).

    ⚠️ **Sans cible, rien ne se rattache** : une cible vide ne désigne aucun sujet.

    ⚠️ **L'ORGANISATION fait partie de la clé** (#684/#685) : le même texte sur deux
    organisations est une correction d'adresse, pas une redite — les fusionner
    laisserait le signalement classé au mauvais endroit, définitivement.

    Le REJEU reste ce qu'il était : le même texte, du même auteur, sur la même clé,
    en moins de dix minutes, ne s'écrit pas du tout — ni signal, ni occurrence.

    ⚠️ Deux dépôts SIMULTANÉS d'une clé neuve peuvent créer deux signaux : la course
    n'est pas fermée par un verrou, elle ne coûte qu'un doublon que l'arbitrage voit."""
    p = {"sub": sub, "org_id": org_id, "signal": signal, "kind": kind,
         "target": target, "body": body, "session_id": session_id,
         "source": source, "minutes": str(_REJEU_SIGNAL_MINUTES)}
    with _connect() as conn:
        vu = conn.execute(
            """
            SELECT id FROM usage_signals
             WHERE sub IS NOT DISTINCT FROM %(sub)s
               AND org_id IS NOT DISTINCT FROM %(org_id)s
               AND signal = %(signal)s AND kind = %(kind)s
               AND target IS NOT DISTINCT FROM %(target)s
               AND body IS NOT DISTINCT FROM %(body)s
               AND created_at > NOW() - (%(minutes)s || ' minutes')::interval
            UNION ALL
            SELECT s.id FROM usage_signal_occurrences o
              JOIN usage_signals s ON s.id = o.signal_id
             WHERE o.sub IS NOT DISTINCT FROM %(sub)s
               AND s.org_id IS NOT DISTINCT FROM %(org_id)s
               AND s.signal = %(signal)s AND o.kind = %(kind)s
               AND s.target IS NOT DISTINCT FROM %(target)s
               AND o.body IS NOT DISTINCT FROM %(body)s
               AND o.created_at > NOW() - (%(minutes)s || ' minutes')::interval
             ORDER BY id DESC LIMIT 1
            """, p).fetchone()
        if vu is not None:
            etat = _etat_signal(conn, int(vu["id"]))
            return DepotSignal(int(vu["id"]), deja=True, **etat)
        if target is not None and target.strip():
            ouvert = conn.execute(
                f"""
                SELECT id FROM usage_signals
                 WHERE {_CLE_SIGNAL} AND status <> ALL(%(clos)s)
                 ORDER BY created_at DESC LIMIT 1
                """, {**p, "clos": list(SIGNAL_TERMINAL)}).fetchone()
            if ouvert is not None:
                conn.execute(
                    """
                    INSERT INTO usage_signal_occurrences
                        (signal_id, sub, kind, body, session_id, source)
                    VALUES (%(id)s, %(sub)s, %(kind)s, %(body)s, %(session_id)s, %(source)s)
                    """, {**p, "id": int(ouvert["id"])})
                etat = _etat_signal(conn, int(ouvert["id"]))
                return DepotSignal(int(ouvert["id"]), rattache=True, **etat)
            clos = conn.execute(
                f"""
                SELECT id, status, resolution FROM usage_signals
                 WHERE {_CLE_SIGNAL} AND status = ANY(%(clos)s)
                 ORDER BY COALESCE(resolved_at, created_at) DESC LIMIT 1
                """, {**p, "clos": list(SIGNAL_TERMINAL)}).fetchone()
        else:
            clos = None
        row = conn.execute(
            """
            INSERT INTO usage_signals
                (sub, org_id, signal, kind, target, body, session_id, source)
            VALUES (%(sub)s, %(org_id)s, %(signal)s, %(kind)s, %(target)s, %(body)s,
                    %(session_id)s, %(source)s) RETURNING id
            """, p).fetchone()
        precedent = ({"id": int(clos["id"]), "status": clos["status"],
                      "resolution": clos["resolution"]} if clos is not None else None)
        return DepotSignal(int(row["id"]), precedent=precedent)


def _etat_signal(conn, signal_id: int) -> dict:
    """`status`, `resolution` et `occurrences` (1 + les rattachées) d'un signal."""
    r = conn.execute(
        """
        SELECT s.status, s.resolution,
               1 + (SELECT count(*) FROM usage_signal_occurrences o
                     WHERE o.signal_id = s.id) AS occurrences
          FROM usage_signals s WHERE s.id = %s
        """, (signal_id,)).fetchone()
    return {"status": r["status"], "resolution": r["resolution"],
            "occurrences": int(r["occurrences"])}


# Les quatre états d'ARBITRAGE d'un signal (#450). Deux ne suffisaient pas :
# « ouvert » confondait ce que personne n'a lu avec ce qu'on a lu sans savoir qu'en
# faire, et il n'existait aucune façon de dire non. Un stock où le refus est
# indicible ne peut que monter — on n'y distingue plus le retard du désaccord.
SIGNAL_STATUSES = ("open", "acknowledged", "declined", "resolved")

# Ce qui est ARBITRÉ — donc ce qui sort de la pile. `declined` en fait partie : un
# refus est une décision, pas un abandon, et c'est exactement ce que l'ancien modèle
# ne savait pas exprimer.
SIGNAL_TERMINAL = ("declined", "resolved")

# Filtre de commodité, PAS un état : « ce qui reste à arbitrer ». Il existe parce que
# c'est la seule question qu'un opérateur pose vraiment en ouvrant la pile, et que
# depuis qu'il y a quatre états, `open` seul n'y répond plus.
SIGNAL_PENDING = "pending"


def list_usage_signals(
    signal: Optional[str] = None, target: Optional[str] = None, limit: int = 200,
    status: Optional[str] = None, org_id: Optional[int] = None,
) -> list[dict]:
    """Signaux récents (récent d'abord), filtrables par type / cible / statut —
    base des projections (qualité d'outil, manques) du barreau 4.

    `status` : l'un des `SIGNAL_STATUSES`, ou `'pending'` (= tout ce qui n'est pas
    arbitré : open ∪ acknowledged), ou None (tous). Joint l'email/nom du rapporteur
    (LEFT JOIN users) pour l'UI admin.

    ⚠️ Le filtre lit la COLONNE `status`, jamais `resolved_at IS NULL` : depuis
    #450 un signal arbitré peut l'être en `declined`, qui porte lui aussi une date
    — la dériver de la date rendrait un refus indistinguable d'un traitement."""
    limit = max(1, min(int(limit), 1000))
    # `occurrences` = 1 + les dépôts rattachés ; `last_seen_at` = le plus récent. Le tri
    # suit la DERNIÈRE occurrence : un sujet ancien qui revient remonte en tête.
    sql = ("SELECT s.id, s.created_at, s.sub, u.email, u.name, s.org_id, s.signal, "
           "s.kind, s.target, s.body, s.session_id, s.source, s.status, "
           "s.resolved_at, s.resolved_by, s.resolution, s.notified_at, "
           "1 + COALESCE(oc.n, 0) AS occurrences, "
           "GREATEST(s.created_at, oc.dernier) AS last_seen_at "
           "FROM usage_signals s LEFT JOIN users u ON u.sub = s.sub "
           "LEFT JOIN LATERAL (SELECT count(*) AS n, max(o.created_at) AS dernier "
           "FROM usage_signal_occurrences o WHERE o.signal_id = s.id) oc ON true")
    clauses, params = [], []
    if signal:
        clauses.append("s.signal = %s"); params.append(signal)
    if target:
        clauses.append("s.target = %s"); params.append(target)
    if org_id is not None:
        # Scope ORG : les signaux ÉMIS SOUS cette org (`usage_signals.org_id`, seam
        # `current_org` au moment du signalement) — jamais l'appartenance de leur
        # auteur, exactement comme le journal d'audit. Un même rapporteur travaillant
        # pour trois clients ne verse donc pas ses retours dans les trois.
        clauses.append("s.org_id = %s"); params.append(int(org_id))
    if status == SIGNAL_PENDING:
        clauses.append("s.status <> ALL(%s)"); params.append(list(SIGNAL_TERMINAL))
    elif status:
        clauses.append("s.status = %s"); params.append(status)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY last_seen_at DESC, s.id DESC LIMIT %s"
    params.append(limit)
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]


def set_usage_signal_status(
    signal_id: int, *, status: str, by: Optional[str], note: Optional[str] = None,
) -> Optional[dict]:
    """Pose l'arbitrage d'un signal. Renvoie la row à jour, ou None si l'id n'existe pas.

    Remplace `resolve_usage_signal` (#450) : le verbe « résoudre » ne pouvait dire
    ni « je l'ai lu » ni « je ne le ferai pas », les deux gestes qui manquaient.

    Le retour à `open` EFFACE la trace d'arbitrage — un signal remis dans la pile n'a
    plus été arbitré, et garder l'ancienne note ferait lire une décision qui n'a plus
    cours. Tout autre état la POSE : c'est le dernier arbitrage qui compte, pas le
    premier."""
    if status not in SIGNAL_STATUSES:
        raise ValueError(
            f"statut inconnu {status!r} — les états sont {', '.join(SIGNAL_STATUSES)}")
    with _connect() as conn:
        if status == "open":
            row = conn.execute(
                """
                UPDATE usage_signals
                   SET status = 'open', resolved_at = NULL, resolved_by = NULL,
                       resolution = NULL, notified_at = NULL
                 WHERE id = %s
                RETURNING id, signal, kind, target, status, resolved_at, resolved_by,
                          resolution
                """,
                (signal_id,),
            ).fetchone()
        else:
            row = conn.execute(
                """
                UPDATE usage_signals
                   SET status = %s, resolved_at = NOW(), resolved_by = %s,
                       resolution = %s, notified_at = NULL
                 WHERE id = %s
                RETURNING id, signal, kind, target, status, resolved_at, resolved_by,
                          resolution
                """,
                (status, by, note, signal_id),
            ).fetchone()
        return dict(row) if row else None


def reroute_usage_signal(signal_id: int, *, org_id: Optional[int]) -> Optional[dict]:
    """Change l'ORGANISATION d'un signal. Rend la row à jour, ou None si l'id est inconnu.

    Le défaut réparé (#471) : un signal écrit AU SUJET d'un espace et déposé sur un
    AUTRE, parce qu'un appel avait omis son jeton d'org. `feedback` écrit et ne relit
    rien, l'arbitrage pose un état et pas une adresse — la ligne restait donc là pour
    toujours, comptée dans les lentilles d'un espace qui n'aurait jamais dû la voir.

    **Déplacer, pas supprimer.** Un signal est un fait — l'agent a réellement buté sur
    ce manque — et cette ligne en est l'unique copie. C'est son ADRESSE qui est fausse,
    pas son existence : on la corrige, ce qui retire le signal du mauvais espace ET le
    rend au bon (les deux lentilles d'org comptent par `org_id`). Une suppression ferait
    la première moitié et perdrait la seconde, en plus de rouvrir la porte que
    `set_usage_signal_status` referme — une pile où des lignes disparaissent sans
    qu'on sache pourquoi.

    `org_id=None` est légitime : un signal qui ne concerne aucun espace client remonte
    au niveau plateforme. **L'existence de l'org cible se vérifie chez l'appelant**
    (capacité) : ici on écrit, on ne juge pas — et une FK ne dirait pas au demandeur
    quel espace il vient de nommer par erreur.

    ⚠️ Le CORPS n'est jamais touché : ré-aiguiller déplace une adresse. Une réécriture
    du texte serait une réécriture de l'histoire, ce que ce module refuse par ailleurs.
    ⚠️ L'ARBITRAGE non plus : un signal déjà tranché reste tranché après son
    déplacement — le changer d'espace ne change pas la décision prise à son sujet.
    """
    with _connect() as conn:
        # L'org d'AVANT est rendue avec la ligne, dans la MÊME instruction : sans elle
        # un ré-aiguillage ne se relit pas — ni pour le vérifier, ni pour le défaire si
        # c'est la destination qu'on a tapée de travers. La lire en deux temps la
        # laisserait dériver entre les deux.
        row = conn.execute(
            """
            UPDATE usage_signals s SET org_id = %s
              FROM (SELECT id, org_id FROM usage_signals WHERE id = %s) avant
             WHERE s.id = avant.id
            RETURNING s.id, s.created_at, s.sub, s.org_id, s.signal, s.kind, s.target,
                      s.body, s.session_id, s.source, s.status, s.resolved_at,
                      s.resolved_by, s.resolution, avant.org_id AS previous_org_id
            """,
            (int(org_id) if org_id is not None else None, signal_id),
        ).fetchone()
        return dict(row) if row else None


def pending_signal_notices() -> list[dict]:
    """Ce qui a été ARBITRÉ sans que son auteur l'ait appris — la matière du retour.

    Seuls les états TERMINAUX comptent : `acknowledged` n'est pas une réponse, et
    annoncer « on l'a lu » userait le canal avant d'avoir rien dit. Un signal
    ré-arbitré revient ici (le changement d'état efface `notified_at`), sinon un
    « traité » corrigé en « refusé » resterait su sous sa première version.

    Rendu à plat, trié par destinataire puis par date : le regroupement se fait chez
    l'appelant, qui est aussi celui qui décide d'envoyer. ⚠️ On joint l'email ICI
    plutôt que de le résoudre plus tard : un compte supprimé depuis le signalement
    n'a plus d'adresse, et il vaut mieux le voir dans la file que découvrir un envoi
    silencieusement perdu. `u.locale` suit le même join (oto-backend#700) : c'est
    une propriété du DESTINATAIRE, pas du signal — inutile de la relire par un
    aller-retour séparé côté appelant.

    ⚠️ **Exclut qui s'est désinscrit du DIGEST** (`signal_digest_optouts`, oto#150),
    en amont — même patron que `_AUDIENCE_SQL` côté relances (`db/outreach.py`) : le
    refus vit dans la requête, pas dans une consigne côté appelant. Les signaux d'un
    compte désinscrit restent `notified_at IS NULL` (ils restent DUS, comme ceux d'un
    compte sans adresse) — se réinscrire les fait réapparaître ici, jamais les perdre."""
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            """
            SELECT s.id, s.sub, u.email, u.name, u.locale, s.signal, s.kind, s.target,
                   s.body, s.created_at, s.status, s.resolution, s.resolved_at
            FROM usage_signals s LEFT JOIN users u ON u.sub = s.sub
            WHERE s.status = ANY(%s) AND s.notified_at IS NULL AND s.sub IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM signal_digest_optouts o WHERE o.sub = s.sub)
            ORDER BY s.sub, s.created_at
            """,
            (list(SIGNAL_TERMINAL),),
        ).fetchall()]


def mark_signals_notified(signal_ids: list) -> int:
    """Marque ces signaux comme annoncés à leur auteur. Rend le nombre de lignes.

    Appelé APRÈS un envoi réussi, jamais avant : un mail qui échoue doit rester dû.
    L'inverse — marquer puis envoyer — ferait disparaître le retour au premier
    hoquet du mailer, et personne ne le saurait."""
    ids = [int(i) for i in signal_ids or []]
    if not ids:
        return 0
    with _connect() as conn:
        return conn.execute(
            "UPDATE usage_signals SET notified_at = NOW() WHERE id = ANY(%s)",
            (ids,),
        ).rowcount


def opt_out_signal_digest(sub: str, *, source: str = "link") -> None:
    """Désinscrit `sub` du DIGEST de signaux (`signal_digest_optouts`, oto#150).

    Idempotent (`ON CONFLICT DO NOTHING`), comme `db.outreach.desinscrire` — même
    forme, table DISTINCTE : ce refus ne touche jamais `outreach_optouts` (les
    relances de plateforme), et réciproquement. Aucune vérification que le compte
    existe : la FK s'en charge, et un jeton signé qui nomme un compte disparu ne
    doit pas fabriquer une erreur au destinataire."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO signal_digest_optouts (sub, source) VALUES (%s, %s) "
            "ON CONFLICT (sub) DO NOTHING", (sub, source))


def count_usage_signals_by_status() -> dict:
    """`{état: n}` sur TOUTE la table, plus `pending` (open ∪ acknowledged).

    Rendu à chaque `op=list` : sans lui, une page de 200 lignes ne dit pas si la pile
    en compte 203 ou 2 000, et c'est précisément le chiffre qu'on vient chercher.
    Les états à zéro figurent — un état absent de la réponse se lit « pas encore
    implémenté », pas « personne ne l'a utilisé »."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT status, count(*) AS n FROM usage_signals GROUP BY status"
        ).fetchall()
    par_etat = {s: 0 for s in SIGNAL_STATUSES}
    for r in rows:
        par_etat[str(r["status"])] = int(r["n"])
    par_etat[SIGNAL_PENDING] = sum(
        n for s, n in par_etat.items()
        if s in SIGNAL_STATUSES and s not in SIGNAL_TERMINAL)
    return par_etat


def list_runs(limit: int = 100, *, org_id: Optional[int] = None) -> list[dict]:
    """Runs récents (un par run_id ouvert via run_start) avec label/doctrine, version de
    procédure exécutée, acteur, bornes, outcome (si fermé) et nb d'appels du déroulé.
    `slug` (alias = doctrine sinon label) conservé pour compat dashboard.

    `org_id` (si fourni) borne aux déroulés OUVERTS sous cette org (`tool_calls.org_id`
    de la ligne `run_start`, seam `current_org`) — scope de la lentille org (org_admin),
    même règle exacte que le journal d'audit. Sans lui : plateforme-wide (défaut admin).

    **Légère par construction** (infra#9) : les `limit` dernières ouvertures d'abord
    (`_derniers_runs`), puis, pour elles seules, la reconstruction et le compte des
    appels (un LATERAL par run, servi par `idx_tool_calls_run`). L'ancienne forme
    groupait TOUT le journal par run (`GROUP BY run_id` sur chaque ligne qui en porte
    un, toutes orgs confondues) et reconstruisait tous les runs de la portée, à chaque
    affichage de la liste. Lecture bornée (`_agregat`, 10 s) en filet."""
    limit = max(1, min(int(limit), 500))
    portee = " AND d.org_id = %s" if org_id is not None else ""
    params: list[Any] = ([int(org_id)] if org_id is not None else []) + [limit, limit]
    with _agregat("liste des déroulés") as conn:
        return [dict(r) for r in conn.execute(
            f"""
            WITH j AS ({_runs_from_journal(_derniers_runs(portee))})
            SELECT j.run_id,
                   COALESCE(j.doctrine, j.label) AS slug,
                   j.label, j.doctrine, j.doctrine_version,
                   j.sub, u.email, u.name,
                   j.started_at, j.finished_at, j.outcome, j.last_seen_at,
                   c.n_calls
              FROM j
              LEFT JOIN users u ON u.sub = j.sub
              LEFT JOIN LATERAL (
                  SELECT count(*) AS n_calls FROM tool_calls t WHERE t.run_id = j.run_id
              ) c ON TRUE
             ORDER BY j.started_at DESC LIMIT %s
            """,
            tuple(params),
        ).fetchall()]


def get_run(run_id: str, *, org_id: Optional[int] = None) -> list[dict]:
    """Timeline d'un déroulé : tous les appels du run, dans l'ordre.

    `org_id` (si fourni) ne rend que les appels émis SOUS cette org — un run_id
    deviné depuis une autre org rend une timeline VIDE, que l'appelant traduit en
    404 (pas de lecture cross-org par id devinable)."""
    clauses = ["run_id = %s"]
    params: list[Any] = [run_id]
    if org_id is not None:
        clauses.append("org_id = %s")
        params.append(int(org_id))
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            f"""
            SELECT id, created_at, tool, args, ok, error, duration_ms
            FROM tool_calls WHERE {" AND ".join(clauses)} ORDER BY created_at
            """,
            tuple(params),
        ).fetchall()]


def run_content_archived(run_id: str, *, org_id: Optional[int] = None) -> Optional[dict]:
    """Ce que l'archive du journal a pris au corps de ce run, ou `None` (#665).

    Arbitrage du 23/09/2026, option B : passé la rétention, un run garde ses bornes
    (`run_start`/`run_finish` ne sont jamais archivés) et perd son corps, parti au
    froid par mois calendaire entier. Sa page doit alors le DIRE — « contenu archivé
    le … » — et non servir deux lignes de bornes sous une issue « done », qui est la
    page vide de #289 revenue à une autre borne.

    Lu dans le registre `journal_archives` (écrit par l'archive elle-même, avant de
    supprimer), jamais déduit d'un corps absent : un run sans appel entre ses bornes
    n'est pas un run archivé. Les mois consultés vont de l'ouverture du run à sa
    clôture — à AUJOURD'HUI s'il est resté ouvert, puisque son corps a pu continuer
    après la dernière ligne qui reste. Même scope d'org que `get_run`."""
    clauses = ["run_id = %s"]
    params: list[Any] = [run_id]
    if org_id is not None:
        clauses.append("org_id = %s")
        params.append(int(org_id))
    with _connect() as conn:
        rows = conn.execute(
            f"""
            WITH b AS (
                SELECT date_trunc('month', min(created_at)) AS lo,
                       CASE WHEN bool_or(tool = 'run_finish')
                            THEN date_trunc('month', max(created_at))
                            ELSE date_trunc('month', NOW()) END AS hi
                  FROM tool_calls WHERE {" AND ".join(clauses)}
            )
            SELECT a.mois, a.archived_at
              FROM journal_archives a, b
             WHERE a.mois BETWEEN to_char(b.lo, 'YYYY-MM') AND to_char(b.hi, 'YYYY-MM')
             ORDER BY a.mois
            """,
            tuple(params),
        ).fetchall()
    if not rows:
        return None
    mois = [r["mois"] for r in rows]
    archived_at = max(r["archived_at"] for r in rows)
    return {
        "archived_at": archived_at,
        "months": mois,
        "message": (
            f"Contenu archivé le {archived_at:%d/%m/%Y} : les appels de ce déroulé "
            f"({', '.join(mois)}) ont quitté la base pour l'archive froide, passé la "
            "rétention du journal. Ses bornes — ouverture, clôture, issue — restent "
            "ici ; le détail s'obtient auprès de l'exploitant de la plateforme."),
    }


def _signal_agg(signal: str, group_by: str, label: str, days: int,
                org_id: Optional[int]) -> list[dict]:
    """Corps commun des deux agrégats de `usage_signals` (manques / qualité d'outil) :
    même fenêtre, même `users` (emails distincts des rapporteurs, repli sub si compte
    inconnu), même scope `org_id` optionnel — seuls le signal et l'axe de groupement
    changent. `group_by`/`label` sont des littéraux du module, jamais une entrée."""
    org_clause = " AND s.org_id = %s" if org_id is not None else ""
    params: list[Any] = [signal, int(days)] + ([int(org_id)] if org_id is not None else [])
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            f"""
            SELECT {group_by}, s.kind, count(*) AS n, max(s.created_at) AS last_at,
                   array_remove(array_agg(DISTINCT coalesce(u.email, s.sub)), NULL) AS users
            FROM usage_signals s
            LEFT JOIN users u ON u.sub = s.sub
            WHERE s.signal = %s AND s.created_at > NOW() - make_interval(days => %s){org_clause}
            GROUP BY {label}, s.kind ORDER BY n DESC, last_at DESC
            """,
            tuple(params),
        ).fetchall()]


def aggregate_gaps(days: int = 30, *, org_id: Optional[int] = None) -> list[dict]:
    """Manques agrégés (cas d'usage non couverts) — backlog produit dérivé.

    `users` = emails distincts des rapporteurs (repli sub si compte inconnu) —
    l'UI admin montre QUI a signalé, pas seulement combien. `org_id` = les manques
    remontés PAR les membres de cette org (lentille org_admin) ; sans lui, plateforme."""
    return _signal_agg("gap", "s.target AS intent", "s.target", days, org_id)


def aggregate_tool_feedback(days: int = 30, *, org_id: Optional[int] = None) -> list[dict]:
    """Qualité d'outil agrégée : feedback par (outil, kind). `users`/`org_id` :
    cf. aggregate_gaps."""
    return _signal_agg("tool_feedback", "s.target AS tool", "s.target", days, org_id)


def agents_en_echec(days: int = 1, *, org_id: Optional[int] = None,
                    seuil: int = 2) -> list[dict]:
    """Les agents dont les travaux MEURENT, groupés par agent et par motif.

    ⚠️ **La panne qu'on ne voit pas.** Un agent déclenché casse en silence : sa
    file se vide (les travaux morts n'attendent plus), son écran le dit « actif »,
    et son dernier déroulé peut très bien être vert. Trois travaux d'un même agent
    de production sont morts sur quatre jours — neuf tentatives — et la panne
    s'est découverte en regardant pour autre chose. Personne n'avait été prévenu,
    parce que rien n'était chargé de prévenir.

    Ce que cette lentille rend disponible : « quel agent a perdu au moins `seuil`
    travaux dans la fenêtre, et pour quel motif ». C'est ce qu'un relevé nocturne
    lit pour en faire UNE ligne.

    ⚠️ **Groupé par MOTIF, pas seulement par agent.** « Trois fois la même panne »
    est un défaut à réparer ; « trois pannes différentes » est un agent qu'on a
    mal réglé, ou un amont instable. Les confondre sous un compte unique ferait
    lire le second comme le premier.

    ⚠️ **Le `motif` rendu est une CLÉ DE REGROUPEMENT, pas le message.** Deux
    choses lui sont faites, et le message entier reste sur le travail :

    - les suites de **4 chiffres ou plus** deviennent `N`. C'est ce qui fait que
      « le travail 23324 est arrivé sans… » et « le travail 23325 est arrivé
      sans… » sont UNE panne. Tronquer la tête ne suffisait pas : l'identifiant
      qui varie est au douzième caractère, donc dans ce qu'on garde (banc ③).
    - ⚠️ **Trois chiffres et moins sont laissés INTACTS**, délibérément : `429`
      et `400` sont deux refus différents, et les fondre en « HTTP N » ferait
      lire un throttle et un contrat cassé comme une seule panne. La frontière
      est à quatre parce que les identifiants de cette base en portent cinq et
      les codes HTTP trois — elle sépare ce que l'on veut séparer.
    - puis on tronque à `_MOTIF_TETE`, pour qu'un message long ne se scinde pas
      sur sa queue.

    `org_id` scope à une org (lentille org_admin) ; sans lui, plateforme.
    """
    ou = "AND j.org_id = %s" if org_id is not None else ""
    params: list = [days]
    if org_id is not None:
        params.append(org_id)
    params.append(max(1, int(seuil)))
    with _connect() as conn:
        rows = conn.execute(
            rf"""
            SELECT j.org_id,
                   (j.payload->>'trigger_id')::bigint    AS trigger_id,
                   j.payload->>'label'                   AS label,
                   LEFT(regexp_replace(j.last_error, '\d{{4,}}', 'N', 'g'),
                        {_MOTIF_TETE})                    AS motif,
                   COUNT(*)::int                         AS travaux,
                   SUM(j.attempts)::int                  AS tentatives,
                   MIN(j.finished_at)                    AS depuis,
                   MAX(j.finished_at)                    AS dernier
              FROM runner_jobs j
             WHERE j.status = 'failed'
               AND j.finished_at > NOW() - make_interval(days => %s)
               {ou}
             GROUP BY 1, 2, 3, 4
            HAVING COUNT(*) >= %s
             ORDER BY travaux DESC, dernier DESC
             LIMIT 100
            """,
            tuple(params),
        ).fetchall()
    return [dict(r) for r in rows]


#: Ce qu'on garde du motif après normalisation — assez long pour qu'une cause
#: reste lisible dans l'alerte, assez court pour qu'une queue variable ne scinde
#: pas un groupe. La normalisation des identifiants, elle, se fait AVANT la
#: troncature : cf. `agents_en_echec`, l'ordre importe.
_MOTIF_TETE = 80


def list_tool_calls(
    limit: int = 200,
    sub: Optional[str] = None,
    tool_name: Optional[str] = None,
    errors_only: bool = False,
    since_days: Optional[int] = None,
    org_id: Optional[int] = None,
    run_id: Optional[str] = None,
    session_id: Optional[str] = None,
    min_duration_ms: Optional[int] = None,
    error_contains: Optional[str] = None,
) -> list[dict]:
    """Derniers appels MCP (récent d'abord), joints à l'email user pour l'UI.

    `org_id` (si fourni) scope les appels émis SOUS cette org (colonne `tool_calls.org_id`
    stampée par le seam `current_org` au moment de l'appel, ADR 0023) — l'activité « la
    mienne » du dashboard doit refléter l'org chargée, pas l'union de toutes mes orgs.

    Axes d'investigation : `run_id`/`session_id` = tous les appels d'un déroulé /
    d'une conversation ; `min_duration_ms` = appels lents (chasse aux gels mono-loop) ;
    `error_contains` = recherche substring insensible à la casse dans le message.

    La ligne ne porte PAS `args` (le contenu est la fiche, `get_tool_call`) mais
    `arg_keys` : les clés des arguments journalisés, triées, `[]` sans argument
    (`journal_calls.ARG_KEYS_SQL`, #634) — de quoi savoir QUELS arguments un appel
    portait sans ouvrir sa fiche, et sans jamais rendre une valeur — et, à côté,
    `result_shape` (#644) : la FORME de ce que l'outil a rendu, jamais son contenu.
    L'ÉMETTEUR déclaré (`client_name`, `client_version`, `token_kind` —
    `journal_calls.EMITTER_SQL`, oto#187) : quel logiciel client, par quel jeton."""
    limit = max(1, min(int(limit), 1000))
    # Les filtres de la PAGE et ceux de son plancher (#630) sortent de la même
    # construction — c'est ce qui rend les deux comptes comparables.
    clauses, params = journal_calls.call_filter_clauses(
        sub=sub, tool_name=tool_name, errors_only=errors_only, since_days=since_days,
        run_id=run_id, session_id=session_id, min_duration_ms=min_duration_ms,
        error_contains=error_contains)
    clauses = ["l.kind = 'mcp'", *clauses]
    if org_id is not None:
        clauses.append("l.org_id = %s")
        params.append(int(org_id))
    where = " WHERE " + " AND ".join(clauses)
    params.append(limit)
    # Bornée (`_agregat`, infra#9) : un filtre sélectif (`error_contains`, `sub`…) sans
    # `days` parcourt la portée entière à rebours en quête de `limit` lignes.
    with _agregat("journal des appels") as conn:
        # Alias tool_name/called_at : compat avec l'UI admin existante.
        rows = conn.execute(
            f"""
            SELECT l.id, l.sub, u.email, u.name, l.tool AS tool_name, l.created_at AS called_at,
                   l.duration_ms, l.ok, l.error, l.session_id, l.run_id, l.org_id,
                   l.sentry_event_id, {journal_calls.ARG_KEYS_SQL} AS arg_keys,
                   l.result_shape, l.quantity, l.key_mode, {journal_calls.EMITTER_SQL}
            FROM tool_calls l
            LEFT JOIN users u ON u.sub = l.sub
            {where}
            ORDER BY l.created_at DESC, l.id DESC
            LIMIT %s
            """,
            tuple(params),
        ).fetchall()
        return list(rows)


def get_tool_call(call_id: int) -> Optional[dict]:
    """Fiche d'UN appel (investigation plateforme) : la ligne complète, args inclus
    (bornés à l'écriture par `truncated_args`, toute coupe déclarée dans
    `args._truncated` — #413) + forme du résultat (`result_shape`, #644) +
    axes de corrélation (session_id, run_id, org_id + nom, client_id) + émetteur déclaré
    (`client_name`/`client_version`/`token_kind`, oto#187) et le jeton nommé
    (`token_id`, jamais sa valeur)."""
    with _connect() as conn:
        row = conn.execute(
            f"""
            SELECT l.id, l.kind, l.server, l.sub, COALESCE(u.email, l.email) AS email,
                   u.name, l.tool, l.args, l.ok, l.error, l.error_kind, l.duration_ms,
                   l.created_at,
                   l.session_id, l.run_id, l.org_id, o.name AS org_name, l.client_id,
                   l.sentry_event_id, l.result_shape, l.quantity, l.key_mode,
                   {journal_calls.EMITTER_SQL}, l.token_id
            FROM tool_calls l
            LEFT JOIN users u ON u.sub = l.sub
            LEFT JOIN orgs o ON o.id = l.org_id
            WHERE l.id = %s
            """,
            (int(call_id),),
        ).fetchone()
        return dict(row) if row else None


# ── Journal d'audit d'une org : la page ET son total, d'une seule lecture ────
#
# ⚠️ **Ces deux nombres ne doivent jamais pouvoir décrire deux jeux différents.**
# Un total bâti sur d'autres clauses que la page est PIRE que pas de total : il a
# l'air d'attester, et le lecteur n'a aucun moyen de s'en apercevoir. C'est la
# faute corrigée le 2026-09-01 sur `node_rows` (#621), où le compte portait sur
# des noms de colonnes NON résolus alors que la page les résolvait.
#
# Trois mécanismes le garantissent ici, et aucun n'est une intention :
#
#   1. **une seule construction de clauses** (`_audit_window_clauses`), appelée
#      par les deux requêtes — deux constructions divergent en silence, c'est le
#      motif déjà retenu pour `journal_calls.call_filter_clauses` (#630) ;
#   2. **une seule transaction, en REPEATABLE READ** : les deux lectures partagent
#      le même snapshot, donc aucun appel ne peut se glisser entre le compte et la
#      page ;
#   3. **une borne haute TOUJOURS posée** — celle du demandeur, ou l'instant gelé
#      au premier appel et reporté par le curseur. La fenêtre est donc CLOSE : le
#      total ne bouge pas d'une page à l'autre, et la concaténation des pages vaut
#      exactement son total. Sans ce gel, un export paginé servirait deux vérités
#      successives, le journal étant alimenté en continu et trié récent d'abord.

# Horodatage ISO en UTC, à la MICROSECONDE. ⚠️ Le curseur ne peut pas se bâtir sur
# le `created_at` servi : le row factory le tronque à la seconde
# (`_conn._normalize_value`, `microsecond=0`). Un keyset bâti dessus sauterait, en
# silence, toutes les lignes de la même seconde que la dernière de la page.
_ISO_US = "'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"'"
_AUDIT_KEYSET_AT = f"to_char(l.created_at AT TIME ZONE 'UTC', {_ISO_US})"


def _audit_window_clauses(
    org_id: int, since: Optional[str], until: str,
) -> tuple[list[str], list[Any]]:
    """Les clauses de la FENÊTRE d'un export d'audit, alias `l` — sans le curseur.

    La page y ajoute sa position, le total non : c'est exactement ce qui les sépare,
    et tout le reste leur est commun par construction."""
    clauses = ["l.kind = 'mcp'", "l.org_id = %s", "l.created_at <= %s::timestamptz"]
    params: list[Any] = [int(org_id), until]
    if since:
        clauses.append("l.created_at >= %s::timestamptz")
        params.append(since)
    return clauses, params


def export_tool_calls_for_org(
    org_id: int, *, since: Optional[str] = None, until: Optional[str] = None,
    limit: int = 1000, before: Optional[tuple[str, int]] = None,
) -> dict:
    """Journal d'audit org-scopé (export #67, complétude #770). Rend
    `{until_effectif, total, calls, next}`.

    Appels émis **sous** `org_id` (colonne `tool_calls.org_id`, stampée par le seam
    `current_org` au moment de l'appel — scope EXACT, pas l'appartenance des
    membres), récent d'abord, fenêtre `[since, until_effectif]` (ISO timestamptz,
    bornes incluses). JAMAIS d'args ni de secret (garantie calllog) — seul l'ÉMETTEUR
    déclaré en est extrait (`journal_calls.EMITTER_SQL`, oto#187).

    - `total` — la population de la FENÊTRE, indépendante de `limit` et de `before`.
    - `calls` — au plus `limit` lignes.
    - `next` — `(horodatage ISO µs, id)` de la dernière ligne rendue quand il en
      reste après elle, sinon `None`. Position pure : l'appelant l'emballe dans son
      curseur opaque avec la fenêtre.
    - `until_effectif` — la borne haute réellement appliquée. Quand l'appelant n'en
      donne pas, l'instant est GELÉ ici et rendu : c'est ce qui fait de l'export une
      période FERMÉE, donc une pièce qui peut attester de sa complétude.

    ⚠ Les appels antérieurs à la colonne `org_id` (NULL) n'apparaissent dans aucun
    export d'org — non reconstructibles a posteriori. ⚠ La rétention du journal
    (`OTO_JOURNAL_RETENTION_DAYS`, 90 j) peut effacer des lignes ENTRE deux pages
    d'un même export : le total reste celui du premier appel, la concaténation peut
    alors en compter moins. C'est le seul écart possible, et il retire des lignes,
    il n'en invente pas."""
    limit = max(1, min(int(limit), 5000))
    # REPEATABLE READ posé en TÊTE de la transaction par `_agregat` (`SET TRANSACTION` est
    # refusé après une requête), avec la borne de durée des lectures d'agrégat
    # (infra#9) : sans `since`, le compte parcourt toute la rétention de l'org.
    with _agregat("export du journal d'une org", isolation="REPEATABLE READ") as conn:
        if not until:
            # `now()` = l'instant d'ouverture de la transaction, donc cohérent avec
            # le snapshot que les deux lectures partagent.
            until = conn.execute(
                f"SELECT to_char(now() AT TIME ZONE 'UTC', {_ISO_US}) AS t"
            ).fetchone()["t"]
        clauses, params = _audit_window_clauses(org_id, since, until)
        total = int(conn.execute(
            f"SELECT count(*) AS n FROM tool_calls l WHERE {' AND '.join(clauses)}",
            tuple(params),
        ).fetchone()["n"])

        page_clauses, page_params = list(clauses), list(params)
        if before is not None:
            # Keyset sur le couple ordonné : intercaler une ligne pendant qu'on
            # pagine ne fait ni sauter ni répéter de ligne, là où un OFFSET le ferait.
            page_clauses.append("(l.created_at, l.id) < (%s::timestamptz, %s)")
            page_params += [before[0], int(before[1])]
        # `limit + 1` : la ligne en trop n'est pas servie, elle DIT qu'il en reste.
        rows = conn.execute(
            f"""
            SELECT l.id, l.created_at, l.sub, u.email, l.tool, l.ok, l.error,
                   l.duration_ms, {journal_calls.EMITTER_SQL},
                   {_AUDIT_KEYSET_AT} AS _keyset_at
            FROM tool_calls l
            LEFT JOIN users u ON u.sub = l.sub
            WHERE {' AND '.join(page_clauses)}
            ORDER BY l.created_at DESC, l.id DESC
            LIMIT %s
            """,
            tuple(page_params + [limit + 1]),
        ).fetchall()

    encore = len(rows) > limit
    rows = [dict(r) for r in rows[:limit]]
    cles = [r.pop("_keyset_at") for r in rows]
    return {"until_effectif": until, "total": total, "calls": rows,
            "next": (cles[-1], rows[-1]["id"]) if encore and rows else None}


# Les noms d'argument qui DÉSIGNENT un job fournisseur relevé par un appel — liste
# FERMÉE de littéraux de ce module, interpolée dans le SQL, jamais fournie par un
# appelant. `job_id` de la lentille de facturation en est la seule sortie : aucun
# autre argument ne quitte le journal par elle (ceux d'un enrichissement portent des
# personnes). Un identifiant de job court passe `calllog.truncated_args` intact
# (scalaire sous la borne, nom non déclaré secret), sur l'appel direct comme sur la
# ligne CIBLE d'un `oto_call` (même fabrique d'arguments).
BILLABLE_JOB_ARGS: tuple[str, ...] = ("enrichment_id",)
_BILLABLE_JOB_ID_SQL = "COALESCE(" + ", ".join(
    f"l.args->>'{nom}'" for nom in BILLABLE_JOB_ARGS) + ")"

# Ce qu'un relevé de job a TROUVÉ, par sorte : clé servie par la lentille (`found`)
# → nom d'argument journalisé (posé par l'outil via `note_call_trace`, versé dans
# `args` par `server._TRACED_ARGS`). Liste FERMÉE de littéraux, comme ci-dessus.
BILLABLE_FOUND_ARGS: dict[str, str] = {
    "work_emails": "found_work_emails",
    "personal_emails": "found_personal_emails",
    "phones": "found_phones",
}
_BILLABLE_FOUND_SQL = ", ".join(
    f"l.args->>'{arg}' AS {arg}" for arg in BILLABLE_FOUND_ARGS.values())


def _found_from_row(row: dict) -> Optional[dict]:
    """Retire de la ligne les trois colonnes `found_*` et rend `found` : un dict des
    trois comptes, ou `None` si l'un manque ou n'est pas un entier `>= 0`. Tout ou
    rien — un compte partiel ferait lire « 0 trouvé » là où rien n'a été mesuré."""
    found: dict[str, int] = {}
    for cle, arg in BILLABLE_FOUND_ARGS.items():
        brut = row.pop(arg, None)
        if not isinstance(brut, str) or not brut.isdigit():
            found = None  # type: ignore[assignment]
            continue
        if found is not None:
            found[cle] = int(brut)
    return found


#: La fenêtre la plus large qu'un relevé de consommation lit (oto-backend#1145). Elle
#: couvre la rétention du journal (`OTO_JOURNAL_RETENTION_DAYS`, 90 j par défaut) : une
#: fenêtre sans borne basse ne perd donc rien en la recevant par défaut, et une fenêtre
#: plus large que ce que le journal garde ne lit que plus longtemps, pour le même résultat.
RELEVE_FENETRE_MAX_JOURS = 92
#: La page la plus longue d'un relevé, curseur pour la suite.
RELEVE_LIMITE_MAX = 5000


class FenetreDeReleveRefusee(ValueError):
    """Une fenêtre de relevé plus large que `RELEVE_FENETRE_MAX_JOURS` : refus NOMMÉ,
    jamais une fenêtre rognée en silence — le total rendu ne serait plus celui demandé."""


def _fenetre_du_releve(conn, since: Optional[str], until: Optional[str]) -> tuple[str, str]:
    """La fenêtre CLOSE d'un relevé : `(since_effectif, until_effectif)`.

    Borne haute absente = l'instant d'ouverture de la transaction, GELÉ (même règle que
    l'export d'audit). Borne basse absente = la borne haute moins
    `RELEVE_FENETRE_MAX_JOURS`. La largeur est mesurée par PostgreSQL, qui lit les
    horodatages tels que la requête les lira : aucun second analyseur à tenir d'accord.
    Une borne fournie revient telle quelle — c'est elle que le curseur reporte."""
    row = conn.execute(
        f"""
        SELECT to_char(b.u AT TIME ZONE 'UTC', {_ISO_US}) AS u_iso,
               to_char(b.s AT TIME ZONE 'UTC', {_ISO_US}) AS s_iso,
               b.u - b.s > make_interval(days => %s) AS trop_large
          FROM (SELECT a.u, COALESCE(%s::timestamptz,
                                     a.u - make_interval(days => %s)) AS s
                  FROM (SELECT COALESCE(%s::timestamptz, now()) AS u) a) b
        """,
        (RELEVE_FENETRE_MAX_JOURS, since, RELEVE_FENETRE_MAX_JOURS, until),
    ).fetchone()
    if row["trop_large"]:
        raise FenetreDeReleveRefusee(
            f"fenêtre de plus de {RELEVE_FENETRE_MAX_JOURS} jours : resserrer `since`/`until` "
            "et lire en plusieurs fenêtres.")
    return (since or row["s_iso"], until or row["u_iso"])


def list_billable_calls_for_org(
    org_id: int, tool: Optional[str] = None, *, run_ids: Optional[list[str]] = None,
    since: Optional[str] = None,
    until: Optional[str] = None, limit: int = 1000,
    before: Optional[tuple[str, int]] = None,
) -> dict:
    """Les appels FACTURABLES d'un outil et/ou d'un run sous une org — la lentille membre du
    relevé de consommation (`org.usage.calls`). Rend
    `{until_effectif, total, calls, next, unknown_run_ids}`, **même contrat que
    `export_tool_calls_for_org`**, et pour les mêmes raisons :

    - une seule construction de clauses (`_audit_window_clauses` + l'outil),
      partagée par le compte et la page ;
    - une transaction REPEATABLE READ, donc un seul snapshot pour les deux ;
    - une borne haute TOUJOURS posée (gelée au premier appel, reportée par le
      curseur) — la fenêtre est CLOSE, la concaténation des pages vaut son total ;
    - une fenêtre d'au plus `RELEVE_FENETRE_MAX_JOURS`, borne basse comprise quand
      l'appelant n'en donne pas (`_fenetre_du_releve`, oto-backend#1145) — au-delà,
      `FenetreDeReleveRefusee` ; une page d'au plus `RELEVE_LIMITE_MAX` lignes.

    ⚠️ C'est ce `total` qui rend un relevé VÉRIFIABLE : le consommateur compare
    ce qu'il a lu à ce que la fenêtre contenait. `list_tool_calls` plafonne à
    1000 en silence et sans curseur — une page tronquée y a l'air complète, et
    sous-facture sans lever d'erreur. Le relevé ne doit JAMAIS repasser par là.

    Projection ÉTROITE par construction : id, outil, date, quantité, mode de
    clé, l'identité du job relevé (`job_id`, lue pour la seule liste fermée
    `BILLABLE_JOB_ARGS` — `None` pour tout autre outil) et ce qu'il a trouvé
    (`found`, contacts par sorte, lu pour `BILLABLE_FOUND_ARGS` — `None` quand
    rien n'a été tracé). Ni `sub`, ni `email`,
    ni `error`, ni aucun autre argument — la lentille est lisible par tout
    membre, et ce qu'il lit est ce que son org consomme, pas qui a fait quoi.
    Seuls les appels `ok` : un échec n'a rien consommé chez le fournisseur.

    `run_ids` (et la colonne rendue `run_id`) : ce qu'un ou plusieurs runs ont
    consommé, tous outils confondus — `idx_tool_calls_run` sert le filtre. Il reste
    sous `org_id` : nommer le run d'une autre org ne rend rien. Au moins un de
    `tool`/`run_ids`.

    `unknown_run_ids` : ceux des `run_ids` qui ne désignent aucun run de CETTE org —
    ni ligne `runs` à son `org_id`, ni appel journalisé sous elle (un run d'avant la
    table `runs` n'a que le journal) —, dans l'ordre demandé. C'est ce qui distingue
    un run inconnu d'un run qui n'a rien consommé, les deux rendant `total: 0`. Le run
    d'une autre org y figure comme un id inexistant : aucun oracle. Même snapshot que
    le reste."""
    if not tool and not run_ids:
        raise ValueError("list_billable_calls_for_org : `tool` ou `run_ids` requis")
    if not 1 <= int(limit) <= RELEVE_LIMITE_MAX:
        raise ValueError(f"list_billable_calls_for_org : `limit` hors de 1..{RELEVE_LIMITE_MAX}")
    with _agregat("relevé des appels d'une org",
                  isolation="REPEATABLE READ") as conn:
        since, until = _fenetre_du_releve(conn, since, until)
        clauses, params = _audit_window_clauses(org_id, since, until)
        clauses.append("l.ok = TRUE")
        if tool:
            clauses.append("l.tool = %s")
            params.append(tool)
        if run_ids:
            clauses.append("l.run_id = ANY(%s)")
            params.append(list(run_ids))
        total = int(conn.execute(
            f"SELECT count(*) AS n FROM tool_calls l WHERE {' AND '.join(clauses)}",
            tuple(params),
        ).fetchone()["n"])

        page_clauses, page_params = list(clauses), list(params)
        if before is not None:
            page_clauses.append("(l.created_at, l.id) < (%s::timestamptz, %s)")
            page_params += [before[0], int(before[1])]
        rows = conn.execute(
            f"""
            SELECT l.id, l.tool, l.quantity, l.key_mode, l.run_id,
                   {_BILLABLE_JOB_ID_SQL} AS job_id,
                   {_BILLABLE_FOUND_SQL},
                   {_AUDIT_KEYSET_AT} AS created_at
            FROM tool_calls l
            WHERE {' AND '.join(page_clauses)}
            ORDER BY l.created_at DESC, l.id DESC
            LIMIT %s
            """,
            tuple(page_params + [limit + 1]),
        ).fetchall()

        inconnus = [r["run_id"] for r in conn.execute(
            """
            SELECT d.run_id
              FROM unnest(%s::text[]) WITH ORDINALITY AS d(run_id, rang)
             WHERE NOT EXISTS (SELECT 1 FROM runs r
                                WHERE r.run_id = d.run_id AND r.org_id = %s)
               AND NOT EXISTS (SELECT 1 FROM tool_calls t
                                WHERE t.run_id = d.run_id AND t.org_id = %s)
             ORDER BY d.rang
            """,
            (list(run_ids), org_id, org_id),
        ).fetchall()] if run_ids else []

    encore = len(rows) > limit
    rows = [dict(r) for r in rows[:limit]]
    for r in rows:
        r["found"] = _found_from_row(r)
    return {"since_effectif": since, "until_effectif": until, "total": total, "calls": rows,
            "next": (rows[-1]["created_at"], rows[-1]["id"]) if encore and rows else None,
            "unknown_run_ids": inconnus}


def billable_usage_by_tool_for_org(
    org_id: int, *, tools: Optional[list[str]] = None,
    since: Optional[str] = None, until: Optional[str] = None,
) -> dict:
    """Le relevé AGRÉGÉ d'une org sur une fenêtre close, en UNE passe (oto-backend#1145) :
    par outil et par mode de clé, le nombre d'appels facturables, la quantité, et le
    nombre de jobs distincts. Remplace N lectures `list_billable_calls_for_org`, une par
    outil, qu'un consommateur rejouait à chaque rafraîchissement.

    Mêmes appels que la lentille par appel — `kind='mcp'`, sous `org_id`, `ok`, dans la
    fenêtre — donc la somme des `calls` d'un outil égale le `total` de sa lentille sur
    la même fenêtre. `tools` restreint aux outils nommés (servi par
    `idx_tool_calls_org_tool_ok`) ; absent, tous les outils de l'org.

    - `quantity` : la somme des quantités, une ligne sans compte valant 1 (cf. le
      commentaire DDL de `tool_calls.quantity` : NULL se lit 1, jamais 0) ;
    - `key_mode` : `None` = non attribuable, à NE PAS facturer ;
    - `jobs` : les jobs fournisseur DISTINCTS relevés (`BILLABLE_JOB_ARGS`) — un job
      relevé plusieurs fois compte une fois ici ; 0 pour un outil sans job.

    Rend `{since_effectif, until_effectif, tools: [{tool, key_mode, calls, quantity,
    jobs}]}`, trié par outil puis mode de clé.

    **Lu sur les totaux par jour** (oto-backend#1147) : les jours entiers de la fenêtre
    que le registre porte, plus le journal direct pour le reste (les bouts de jour aux
    bornes, la veille avant la maintenance, le jour courant) — `journal_jour.source`.
    Les jobs distincts se comptent sur l'UNION des clés des jours et du direct. Même
    réponse qu'au journal seul (`tests/db/test_journal_jour_lecteurs.py`) ; un jour
    clos manquant au registre lève `journal_jour.AgregatIncomplet`."""
    objet = "relevé par outil d'une org"
    with _agregat(objet) as conn:
        since, until = _fenetre_du_releve(conn, since, until)
        fenetre = {"depuis": since, "jusqu_a": until, "haute_incluse": True}
        src, params = journal_jour.source(
            conn, objet, kinds=("mcp",), mesures=("appels", "quantite"),
            filtres={"org_id": int(org_id), "ok": True,
                     "tools": list(tools) if tools else None}, **fenetre)
        jobs, pj = journal_jour.source_jobs(
            conn, objet, filtres={"org_id": int(org_id),
                                  "tools": list(tools) if tools else None}, **fenetre)
        rows = conn.execute(
            f"""
            WITH s AS ({src}),
                 j AS ({jobs}),
                 g AS (SELECT tool, key_mode, sum(appels)::bigint AS calls,
                              sum(quantite)::bigint AS quantity
                         FROM s GROUP BY tool, key_mode),
                 nj AS (SELECT tool, key_mode, count(*) AS jobs FROM j GROUP BY tool, key_mode)
            SELECT g.tool, g.key_mode, g.calls, g.quantity, COALESCE(nj.jobs, 0) AS jobs
              FROM g LEFT JOIN nj ON nj.tool = g.tool
                                 AND nj.key_mode IS NOT DISTINCT FROM g.key_mode
             ORDER BY g.tool, g.key_mode NULLS LAST
            """,
            {**params, **pj},
        ).fetchall()
    return {"since_effectif": since, "until_effectif": until,
            "tools": [{"tool": r["tool"], "key_mode": r["key_mode"],
                       "calls": int(r["calls"]), "quantity": int(r["quantity"]),
                       "jobs": int(r["jobs"])} for r in rows]}


#: Le premier instant de la fenêtre des lectures d'usage d'une procédure : minuit UTC
#: il y a `%s` jours (le paramètre lié vaut `days - 1`), donc `days` jours UTC
#: aujourd'hui compris — la même fenêtre que la série densifiée côté capacité.
_DEBUT_FENETRE_UTC = ("(date_trunc('day', now() AT TIME ZONE 'UTC')"
                      " - make_interval(days => %s)) AT TIME ZONE 'UTC'")


def _verifier_lectures(lectures: dict[str, str]) -> None:
    """Refus d'un verbe hors de `_VERBES_USAGE`, ou lu sous une autre clé que la sienne."""
    if not lectures:
        raise ValueError("lectures d'usage : aucun verbe demandé")
    for outil, cle in lectures.items():
        if _VERBES_USAGE.get(outil) != cle:
            raise ValueError(f"verbe d'usage non supporté: {outil!r} / {cle!r}")


def instruction_usage(
    org_id: int, slug: Optional[str], *, lectures: dict[str, str], days: int = 30,
) -> dict[str, dict]:
    """Usage d'un guide dérivé de `tool_calls` (ADR 0014, « guide = process = log
    d'usage »), pour PLUSIEURS verbes en UNE passe : par verbe, combien de fois, par
    qui, et la distribution journalière sur les `days` derniers jours UTC (aujourd'hui
    compris). Lecture pure ; rend `{tool: {count, callers, daily{date:str -> n}}}`.

    `lectures` = `{tool: clé d'args qui porte la procédure}`. Un CHARGEMENT
    (`oto_procedure`) la nomme sous `slug` ; un DÉROULÉ (`run_start`) sous
    `_ARG_PROCEDURE`, celle-là même que `_runs_from_journal` lit pour reconstruire les
    runs depuis la MÊME table. `slug=None` (guide de base) ne filtre pas la procédure.
    Clés en liste FERMÉE de littéraux de ce module : elles sont interpolées dans le
    SQL, jamais fournies par un appelant.

    **Bornée et sous l'org** (oto-backend#1145, mesuré en production : 172 à 192 s par
    lecture). L'ancienne forme comptait sans borne de temps les appels des membres
    ACTUELS, toutes orgs confondues (`sub = ANY(membres)`), en quatre requêtes : un
    `BitmapAnd` de l'index par outil (124 k lignes) et de l'index par compte (1,4 M),
    puis l'extraction JSON de chaque ligne. Ici : les appels émis SOUS `org_id` — l'org
    dont le guide est lu —, réussis, dans la fenêtre, servis par
    `idx_tool_calls_org_tool_ok`. `count` porte donc sur la même fenêtre que `daily` :
    `count == sum(daily)`."""
    _verifier_lectures(lectures)
    days = max(1, min(int(days), 365))
    outils = list(lectures)
    params: list[Any] = [int(org_id), outils, days - 1]
    filtre_slug = ""
    if slug is not None:
        par_outil = []
        for outil, cle in lectures.items():
            par_outil.append(f"(l.tool = %s AND l.args->>'{cle}' = %s)")
            params += [outil, slug]
        filtre_slug = " AND (" + " OR ".join(par_outil) + ")"
    with _agregat("usage d'une procédure") as conn:
        rows = conn.execute(
            f"""
            SELECT l.tool, (l.created_at AT TIME ZONE 'UTC')::date AS d, u.email,
                   COUNT(*) AS n
              FROM tool_calls l LEFT JOIN users u ON u.sub = l.sub
             WHERE l.org_id = %s AND l.tool = ANY(%s) AND l.ok
               AND l.created_at >= {_DEBUT_FENETRE_UTC}
                   {filtre_slug}
             GROUP BY l.tool, d, u.email
            """,
            tuple(params),
        ).fetchall()
    rendu: dict[str, dict] = {}
    for outil in outils:
        lignes = [r for r in rows if r["tool"] == outil]
        par_appelant: dict[str, int] = {}
        daily: dict[str, int] = {}
        for r in lignes:
            daily[str(r["d"])] = daily.get(str(r["d"]), 0) + int(r["n"])
            if r["email"]:
                par_appelant[r["email"]] = par_appelant.get(r["email"], 0) + int(r["n"])
        rendu[outil] = {
            "count": sum(daily.values()),
            "callers": sorted(par_appelant, key=lambda e: (-par_appelant[e], e)),
            "daily": daily,
        }
    return rendu


def instructions_usage_by_slug(
    org_id: int, *, lectures: dict[str, str], days: int = 30,
) -> dict[str, dict[str, dict]]:
    """L'usage de TOUTES les procédures d'une org en UNE requête (oto-backend#1146) :
    par verbe et par procédure nommée, le nombre d'appels et le dernier, sur les `days`
    derniers jours UTC. Ce qu'une LISTE affiche par ligne, là où elle appelait
    `instruction_usage` une fois par procédure.

    Même périmètre qu'`instruction_usage`, dont c'est la lecture groupée : sous
    `org_id`, appels réussis, même fenêtre, servie par `idx_tool_calls_org_tool_ok` ;
    chaque verbe lu sous SA clé d'`args` (`_VERBES_USAGE`). Un appel qui ne nomme pas
    de procédure (une liste, un guide de base) n'est rattaché à aucune.

    Rend `{tool: {slug: {"count", "last_at"}}}` ; une procédure absente n'a été ni
    chargée ni déroulée sur la fenêtre."""
    _verifier_lectures(lectures)
    days = max(1, min(int(days), 365))
    nommee = ("CASE l.tool " + " ".join(
        f"WHEN '{outil}' THEN l.args->>'{cle}'" for outil, cle in lectures.items())
        + " END")
    with _agregat("usage des procédures d'une org") as conn:
        rows = conn.execute(
            f"""
            SELECT l.tool, {nommee} AS slug, COUNT(*) AS n, MAX(l.created_at) AS dernier
              FROM tool_calls l
             WHERE l.org_id = %s AND l.tool = ANY(%s) AND l.ok
               AND l.created_at >= {_DEBUT_FENETRE_UTC}
               AND {nommee} IS NOT NULL
             GROUP BY l.tool, 2
            """,
            (int(org_id), list(lectures), days - 1),
        ).fetchall()
    rendu: dict[str, dict[str, dict]] = {outil: {} for outil in lectures}
    for r in rows:
        rendu[r["tool"]][r["slug"]] = {"count": int(r["n"]), "last_at": r["dernier"]}
    return rendu


def tool_call_stats(since_days: int = 7, *, org_id: Optional[int] = None,
                    sub: Optional[str] = None) -> dict:
    """Agrégats pour le dashboard de monitoring sur les `since_days` derniers jours :
    total, échecs, ventilation par tool / par user / par jour.

    Défaut = PLATEFORME-wide (console `/platform/monitoring`). `org_id`/`sub`
    RESTREIGNENT la fenêtre : `org_id` = l'activité d'UN workspace,
    `sub` = celle d'UN membre — la vue « activité de CE workspace / de moi » de
    l'overview ne fuite plus le trafic des autres orgs/users (oto/#5.2).

    ⚠️ **La taille servie (#340) ne remonte qu'à partir du 2026-09-05**, date où la
    colonne a été posée. Les lignes antérieures la portent à NULL et sont exclues des
    agrégats de taille — d'où `sized` par outil et `sized_calls` au total : ils disent
    sur COMBIEN d'appels la mesure porte. Une fenêtre qui couvre l'avant rend des
    moyennes justes sur un échantillon partiel, et un `total_chars` qui sous-déclare
    la période. On ne réécrit pas un journal ; on l'étiquette."""
    since_days = max(1, min(int(since_days), 365))
    # **Lu sur les totaux par jour** (oto-backend#1147) : la fenêtre glissante se lit sur
    # les jours consolidés qu'elle couvre en entier, et au journal direct pour le reste
    # (le bout du premier jour, la veille avant la maintenance, le jour courant) —
    # `journal_jour.source`, qui lève `AgregatIncomplet` plutôt que de lire au journal
    # un jour clos que le registre n'a pas. Chaque ventilation se recalcule sur ces
    # lignes : les sommes s'additionnent, `sub` est une dimension (comptes distincts
    # exacts), et les p95 se recalculent sur les VALEURS gardées (`durees`, `tailles`) —
    # la même réponse qu'au journal seul (`tests/db/test_journal_jour_lecteurs.py`).
    objet = "agrégats d'appels"
    with _agregat(objet) as conn:
        src, params = journal_jour.source(
            conn, objet, kinds=("mcp",),
            mesures=("appels", "duree_n", "duree_somme", "durees", "taille_n",
                     "taille_somme", "tailles"),
            filtres={"org_id": org_id, "sub": sub}, jours=since_days)
        agregats = conn.execute(
            f"""
            WITH f AS MATERIALIZED ({src}),
                 pd AS (SELECT f.tool, percentile_cont(0.95) WITHIN GROUP (ORDER BY v) AS p
                          FROM f, unnest(f.durees) AS u(v) GROUP BY f.tool),
                 pt AS (SELECT f.tool, percentile_cont(0.95) WITHIN GROUP (ORDER BY v) AS p
                          FROM f, unnest(f.tailles) AS u(v) GROUP BY f.tool)
            SELECT
              (SELECT json_build_object(
                          'total', COALESCE(SUM(appels), 0)::bigint,
                          'errors', COALESCE(SUM(appels) FILTER (WHERE NOT ok), 0)::bigint,
                          'users', COUNT(DISTINCT sub),
                          'served_chars', COALESCE(SUM(taille_somme), 0)::bigint,
                          'sized_calls', COALESCE(SUM(taille_n), 0)::bigint,
                          'emitter_named', COALESCE(SUM(appels) FILTER (
                                               WHERE client_name IS NOT NULL), 0)::bigint)
                 FROM f) AS totals,
              (SELECT COALESCE(json_agg(t ORDER BY t.calls DESC), '[]'::json) FROM (
                  SELECT g.tool AS tool_name,
                         g.calls,
                         g.errors,
                         ROUND(g.duree_somme::numeric / NULLIF(g.duree_n, 0))::int AS avg_ms,
                         ROUND(pd.p)::int AS p95_ms,
                         -- #340 : ce que l'outil coûte à la FENÊTRE de l'agent, en
                         -- caractères de texte servis — la durée ne l'a jamais dit.
                         -- `total_chars` est le chiffre qui CLASSE : un outil appelé 500
                         -- fois à 2 000 caractères pèse plus qu'un appelé deux fois à
                         -- 200 000.
                         g.taille_somme AS total_chars,
                         ROUND(g.taille_somme::numeric / NULLIF(g.taille_n, 0))::int AS avg_chars,
                         ROUND(pt.p)::int AS p95_chars,
                         -- ⚠️ L'étiquette, sans laquelle les trois précédents se lisent
                         -- faux : ils ne portent QUE sur les appels mesurés — ni les
                         -- échecs, ni les lignes antérieures à la colonne. Sur un outil
                         -- où `sized` est loin sous `calls`, la moyenne est exacte et ne
                         -- dit rien de la période.
                         g.taille_n AS sized
                    FROM (SELECT tool, SUM(appels)::bigint AS calls,
                                 COALESCE(SUM(appels) FILTER (WHERE NOT ok), 0)::bigint AS errors,
                                 SUM(duree_n)::bigint AS duree_n,
                                 SUM(duree_somme)::bigint AS duree_somme,
                                 SUM(taille_n)::bigint AS taille_n,
                                 SUM(taille_somme)::bigint AS taille_somme
                            FROM f GROUP BY tool) g
                    LEFT JOIN pd ON pd.tool = g.tool
                    LEFT JOIN pt ON pt.tool = g.tool
                   ORDER BY g.calls DESC LIMIT 100) t) AS by_tool,
              (SELECT COALESCE(json_agg(t ORDER BY t.calls DESC), '[]'::json) FROM (
                  SELECT f.sub, u.email, u.name,
                         SUM(f.appels)::bigint AS calls,
                         COALESCE(SUM(f.appels) FILTER (WHERE NOT f.ok), 0)::bigint AS errors
                    FROM f LEFT JOIN users u ON u.sub = f.sub
                   GROUP BY f.sub, u.email, u.name ORDER BY calls DESC LIMIT 100) t) AS by_user,
              -- oto#187 — les ÉMETTEURS déclarés de la fenêtre (logiciel client), du
              -- plus actif au moins actif ; `NULL` = ligne sans émetteur (antérieure).
              (SELECT COALESCE(json_agg(t ORDER BY t.calls DESC), '[]'::json) FROM (
                  SELECT client_name, SUM(appels)::bigint AS calls
                    FROM f GROUP BY 1 ORDER BY calls DESC LIMIT 50) t) AS by_emitter,
              (SELECT COALESCE(json_agg(t ORDER BY t.day), '[]'::json) FROM (
                  SELECT to_char(jour, 'YYYY-MM-DD') AS day,
                         SUM(appels)::bigint AS calls,
                         COALESCE(SUM(appels) FILTER (WHERE NOT ok), 0)::bigint AS errors
                    FROM f GROUP BY jour) t) AS by_day
            """,
            params,
        ).fetchone()
    totals = agregats["totals"]
    by_tool, by_user = agregats["by_tool"], agregats["by_user"]
    by_emitter, by_day = agregats["by_emitter"], agregats["by_day"]
    return {
        "since_days": since_days,
        "total_calls": int((totals or {}).get("total") or 0),
        "error_count": int((totals or {}).get("errors") or 0),
        "active_users": int((totals or {}).get("users") or 0),
        # #340 — le volume servi, et sur combien d'appels il est mesuré. Les deux
        # ensemble : un total sans son dénominateur invite à le diviser par
        # `total_calls`, qui compte aussi les appels non mesurés.
        "served_chars": int((totals or {}).get("served_chars") or 0),
        "sized_calls": int((totals or {}).get("sized_calls") or 0),
        # oto#187 — la COUVERTURE de l'émetteur : sur `total_calls`, combien portent un
        # logiciel client nommé. Déclaré par le client : lisible, jamais opposable.
        "emitter_named_calls": int((totals or {}).get("emitter_named") or 0),
        "by_emitter": list(by_emitter),
        "by_tool": list(by_tool),
        "by_user": list(by_user),
        "by_day": list(by_day),
    }


# `kind='rest'` porte DEUX natures de ligne (ADR 0046 b4) : la **route** posée par
# `api.routes.RestCallLogger` (`tool='PATCH /api/datastore/…'`, avec durée) et le
# **geste métier** posé par `calllog.log_rest_call` (`tool='data_write'`, sans durée).
# Cette lentille-ci est une télémétrie de SURFACE → elle ne compte que les routes,
# sinon chaque mutation du cockpit double-compterait et `by_route` listerait des
# pseudo-routes `data_write`/`data_delete_row` à latence nulle. Une ligne de route
# est toujours `MÉTHODE /chemin` — le ' /' est le discriminant.
_REST_ROUTE_SHAPE = "position(' /' in tool) > 0"


def _rest_status(error: Optional[str]) -> Optional[int]:
    """`'HTTP 503'` → 503 ; `None` → None (aucune réponse journalisée, cf.
    `rest_call_stats`). Toute autre forme est un journal qui a changé de contrat :
    on lève plutôt que de ranger la ligne dans une case qui mentirait."""
    if error is None:
        return None
    m = re.fullmatch(r"HTTP (\d{3})", error)
    if m is None:
        raise ValueError(f"erreur REST hors contrat `HTTP <code>` : {error!r}")
    return int(m.group(1))


def rest_call_stats(since_days: int = 7, *, org_id: Optional[int] = None,
                    sub: Optional[str] = None, route: Optional[str] = None) -> dict:
    """Lentille REST (ADR 0017, kind='rest') : volume + erreurs + latence des appels
    `/api/*`, **par route** normalisée. `ok` = 2xx/3xx ; les ≥400 sont comptés erreurs.
    Les lignes SÉMANTIQUES du journal datastore (même `kind`, `tool` = nom de geste)
    sont exclues — cf. `_REST_ROUTE_SHAPE`.

    Défaut = PLATEFORME-wide. `sub`/`org_id`/`route` RESTREIGNENT la fenêtre (#451 : la
    console acceptait `sub`/`org_id` et les JETAIT — on croyait lire l'activité d'un
    compte, on lisait celle de toute la plateforme).

    ⚠️ `route` répond à une question que `by_route` ne peut PAS trancher : celui-ci est
    borné à `LIMIT 100`, donc une route à faible volume (oto-dashboard#125 : mesurer un
    chemin de fédération OAuth candidat au retrait) peut être invisible sans que rien ne
    le dise. `total_calls`/`error_count`/`last_call_at` filtrés par `route`, eux, sont un
    COMPTE exact, jamais tronqué. Préfixe (`LIKE route || '%'`), pas exact : une valeur
    complète reste un préfixe d'elle-même, donc les deux usages passent par le même
    paramètre.

    ⚠️ Les axes n'ont pas la même solidité, et la réponse le DIT quand ils sont posés :
    `sub` vient du principal RÉSOLU PAR L'AUTHENTIFICATION (fiable, cf. ci-dessous) ;
    `org_id` vient de l'org de CONSULTATION revendiquée en en-tête par le client
    (`RestCallLogger`, best-effort) — une requête sans cet en-tête ne porte aucune org
    et sort donc du filtre. Un total à 0 sous `org_id` ne prouve pas l'inactivité de
    l'org.

    ⚠️ **`sub` n'est fiable que DEPUIS le 2026-09-05** (#882), et cette phrase disait
    « fiable » avant de l'être. Jusque-là, le middleware le DÉDUISAIT de l'en-tête via
    `_claimed_sub`, qui ne décode qu'un JWT : tout appel par jeton API (`oto_…`) ou par
    jeton de délégation s'écrivait **sans compte**. Filtrer par `sub` ne rendait donc
    que les gestes faits depuis le dashboard, et l'écart entre le total d'une route et
    la somme par compte passait pour normal.

    Les lignes ANTÉRIEURES restent anonymes — on ne réécrit pas un journal. Un filtre
    `sub` sur une fenêtre qui les couvre sous-déclare, et c'est l'HISTORIQUE qui
    manque, pas l'activité. `token_kind` (`user` / `delegation`) distingue en outre,
    depuis la même date, un geste fait par quelqu'un d'un travail exécuté en son nom.

    `by_status` (oto#179) VENTILE les erreurs par code HTTP, sur la même fenêtre et les
    mêmes filtres (`route` compris) : « ce 500 a-t-il mordu ? » se lit sans confondre
    les 4xx attendus avec les pannes. Le code vient de `error = 'HTTP <code>'`, écrit
    par `RestCallLogger`. ⚠️ `status: null` = AUCUNE réponse n'a traversé le journal :
    exception non rattrapée (le 500 est servi PLUS HAUT, par Starlette — c'est la forme
    que prend un plantage, pas un 500 explicite) ou client parti. Toujours un défaut à
    lire, jamais un zéro.

    Index : les trois requêtes partagent la même clause, servie par
    `idx_tool_calls_kind (kind, created_at DESC)` sur une fenêtre bornée (≤ 365 j)."""
    since_days = max(1, min(int(since_days), 365))

    def _where() -> tuple[str, list]:
        clauses = [f"kind = 'rest' AND {_REST_ROUTE_SHAPE}",
                   "created_at >= NOW() - make_interval(days => %s)"]
        params: list = [since_days]
        if org_id is not None:
            clauses.append("org_id = %s"); params.append(int(org_id))
        if sub is not None:
            clauses.append("sub = %s"); params.append(sub)
        if route is not None:
            clauses.append("tool LIKE %s"); params.append(f"{route}%")
        return " AND ".join(clauses), params

    w, wp = _where()
    with _agregat("agrégats des appels REST") as conn:
        totals = conn.execute(
            f"""
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE NOT ok) AS errors,
                   COUNT(DISTINCT sub) AS users,
                   MAX(created_at) AS last_call_at
            FROM tool_calls WHERE {w}
            """,
            tuple(wp),
        ).fetchone() or {}
        by_route = conn.execute(
            f"""
            SELECT tool AS route,
                   COUNT(*) AS calls,
                   COUNT(*) FILTER (WHERE NOT ok) AS errors,
                   ROUND(AVG(duration_ms))::int AS avg_ms,
                   ROUND(percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms))::int AS p95_ms
            FROM tool_calls WHERE {w}
            GROUP BY tool
            ORDER BY calls DESC
            LIMIT 100
            """,
            tuple(wp),
        ).fetchall()
        by_error = conn.execute(
            f"""
            SELECT error, COUNT(*) AS calls
            FROM tool_calls WHERE {w} AND NOT ok
            GROUP BY error
            ORDER BY calls DESC
            """,
            tuple(wp),
        ).fetchall()
    out = {
        "since_days": since_days,
        "total_calls": int((totals or {}).get("total") or 0),
        "error_count": int((totals or {}).get("errors") or 0),
        "active_users": int((totals or {}).get("users") or 0),
        "last_call_at": (totals or {}).get("last_call_at"),
        "by_route": list(by_route),
        "by_status": [{"status": _rest_status(r["error"]), "calls": int(r["calls"])}
                      for r in by_error],
    }
    if org_id is not None or sub is not None or route is not None:
        # Le filtre APPLIQUÉ est rendu : c'est ce qui distingue « restreint à ce
        # compte/cette route » de « toute la plateforme » quand les deux rendent le
        # même total.
        out["filters"] = {k: v for k, v in (("org_id", org_id), ("sub", sub),
                                            ("route", route)) if v is not None}
    if org_id is not None:
        out["org_id_caveat"] = (
            "`org_id` du journal REST = l'org de consultation revendiquée en en-tête "
            "par le client (best-effort) : les requêtes sans cet en-tête ne portent "
            "aucune org et sont EXCLUES de ce filtre. Un 0 ici ne prouve pas "
            "l'inactivité de l'org — recoupe avec `sub`.")
    return out


def list_rest_calls(
    limit: int = 200,
    days: Optional[int] = None,
    sub: Optional[str] = None,
    org_id: Optional[int] = None,
    route: Optional[str] = None,
) -> list[dict]:
    """Lentille PLATEFORME du journal REST, LIGNE PAR LIGNE (kind='rest') — le pendant
    de `list_tool_calls` (MCP) pour l'audit `/api/*` (oto-backend#962).

    Sert `view_as_sub` : la cible du « voir en tant que » APPLIQUÉE par `ViewAsMiddleware`
    (opérateur vérifié, cible existante ≠ soi ; ADR 0023) — `null` sinon, jamais l'en-tête
    brut. ⚠️ Les lignes d'avant le 2026-09-21 portent l'en-tête REVENDIQUÉ, non attesté. C'est ce qui
    répond « cet opérateur a consulté au nom de qui », absent de `rest_call_stats`
    (agrégats seulement) et de `list_tool_calls` (MCP seulement, `view_as_sub` y serait
    toujours NULL puisque le champ n'existe que côté REST).

    Mêmes axes que `rest_call_stats` (`days`/`org_id`/`sub`/`route`, mêmes réserves :
    `org_id` = org de consultation revendiquée en en-tête, best-effort ; `route` =
    préfixe de `MÉTHODE /route`, ex. `GET /api/orgs`). `days` par défaut 7, plafonné à
    365 comme `rest_call_stats` ; `limit` plafonné à 200, le plafond de la console."""
    limit = max(1, min(int(limit), 200))
    since_days = max(1, min(int(days or 7), 365))
    clauses = [f"l.kind = 'rest' AND {_REST_ROUTE_SHAPE}",
               "l.created_at >= NOW() - make_interval(days => %s)"]
    params: list = [since_days]
    if org_id is not None:
        clauses.append("l.org_id = %s"); params.append(int(org_id))
    if sub is not None:
        clauses.append("l.sub = %s"); params.append(sub)
    if route is not None:
        clauses.append("l.tool LIKE %s"); params.append(f"{route}%")
    where = " WHERE " + " AND ".join(clauses)
    params.append(limit)
    with _agregat("journal des appels REST") as conn:
        rows = conn.execute(
            f"""
            SELECT l.id, l.sub, COALESCE(u.email, l.email) AS email, l.tool AS route,
                   l.created_at AS called_at, l.duration_ms, l.ok, l.error, l.org_id,
                   l.view_as_sub
            FROM tool_calls l
            LEFT JOIN users u ON u.sub = l.sub
            {where}
            ORDER BY l.created_at DESC, l.id DESC
            LIMIT %s
            """,
            tuple(params),
        ).fetchall()
        return list(rows)


def list_view_as_writes(org_id: int, days: Optional[int] = None,
                        limit: int = 200) -> list[dict]:
    """Les ÉCRITURES faites AU NOM d'un membre de l'org par un opérateur plateforme
    (« voir en tant que » + geste d'acceptation), plus récentes d'abord.

    Ce que l'org doit pouvoir voir : QUI a écrit (`operator_*` = le porteur réel du
    bearer, `tool_calls.sub`), EN TANT QUE qui (`target_*` = `view_as_sub`), quelle
    route, quand, avec quel résultat. Seules les lignes marquées `args.view_as_write`
    (posé par `RestCallLogger` sur une écriture ACCEPTÉE) — une consultation n'y
    figure pas. Jamais d'arguments ni de secret : le journal REST n'écrit pas le corps.
    `days` défaut 30, plafonné à 365 ; `limit` plafonné à 200."""
    limit = max(1, min(int(limit), 200))
    since_days = max(1, min(int(days or 30), 365))
    with _agregat("écritures en « voir en tant que »") as conn:
        rows = conn.execute(
            f"""
            SELECT l.id, l.created_at AS called_at, l.tool AS route, l.ok, l.error,
                   l.sub AS operator_sub, COALESCE(uo.email, l.email) AS operator_email,
                   l.view_as_sub AS target_sub, ut.email AS target_email
            FROM tool_calls l
            LEFT JOIN users uo ON uo.sub = l.sub
            LEFT JOIN users ut ON ut.sub = l.view_as_sub
            WHERE l.kind = 'rest' AND {_REST_ROUTE_SHAPE}
              AND l.org_id = %s
              AND l.view_as_sub IS NOT NULL
              AND (l.args ->> 'view_as_write') = 'true'
              AND l.created_at >= NOW() - make_interval(days => %s)
            ORDER BY l.created_at DESC, l.id DESC
            LIMIT %s
            """,
            (int(org_id), since_days, limit),
        ).fetchall()
        return list(rows)


def connector_failure_stats(since_days: int = 7, *, org_id: Optional[int] = None) -> dict:
    """Lentille santé connecteurs (ADR 0017, kind='connector') : échecs de résolution
    de credential par provider — combien, combien d'users distincts touchés, dernier
    échec. C'est le signal « ce connecteur ne résout pas » (compte actif sans clé valide).

    `org_id` = les échecs subis SOUS cette org (lentille org_admin : « quel connecteur
    bloque MES membres »). Sans lui : plateforme-wide."""
    since_days = max(1, min(int(since_days), 365))
    # Lu sur les totaux par jour (oto-backend#1147), `kind='connector'` : `sub` y est une
    # dimension, le nombre de comptes touchés reste un DISTINCT exact.
    objet = "échecs de connecteurs"
    with _agregat(objet) as conn:
        src, params = journal_jour.source(
            conn, objet, kinds=("connector",), mesures=("appels", "dernier_at"),
            filtres={"org_id": int(org_id) if org_id is not None else None},
            jours=since_days)
        by_provider = conn.execute(
            f"""
            SELECT f.tool AS provider,
                   SUM(f.appels)::bigint AS failures,
                   COUNT(DISTINCT f.sub) AS users_affected,
                   MAX(f.dernier_at) AS last_at
            FROM ({src}) f
            GROUP BY f.tool
            ORDER BY failures DESC
            LIMIT 100
            """,
            params,
        ).fetchall()
    return {
        "since_days": since_days,
        "total_failures": sum(int(r["failures"]) for r in by_provider),
        "by_provider": list(by_provider),
    }


def transport_refusal_stats(since_days: int = 7) -> dict:
    """Lentille des refus du TRANSPORT MCP (`kind='transport'`, cf.
    `oto_mcp/transport_refusals.py`) : ce qu'on refuse AVANT tout dispatch de session,
    ventilé par cause, par code HTTP et par environnement.

    ⚠️ **Ventilé par environnement, et ce n'est pas un luxe** : préproduction et
    production écrivent dans la MÊME base et `tool_calls.server` est un littéral
    constant, donc sans cet axe les deux trafics s'additionneraient en silence. La
    ligne le porte dans `args->>'env'` (posé à l'écriture depuis `OTO_SENTRY_ENV`).

    La cause vit dans `tool`, préfixée `refus:` — un `GROUP BY` la rend directement,
    sans toucher au JSON. Aucune ligne ne porte de `sub` : à cette couche il n'y a pas
    encore d'identité."""
    since_days = max(1, min(int(since_days), 365))
    with _agregat("refus du transport") as conn:
        par_cause = conn.execute(
            """
            SELECT COALESCE(l.args->>'env', 'inconnu') AS env,
                   substring(l.tool from 7)            AS cause,
                   (l.args->>'statut')::int            AS statut,
                   COUNT(*)                            AS n,
                   MAX(l.created_at)                   AS last_at
            FROM tool_calls l
            WHERE l.kind = 'transport'
              AND l.created_at >= NOW() - make_interval(days => %s)
            GROUP BY 1, 2, 3
            ORDER BY n DESC
            LIMIT 100
            """,
            (since_days,),
        ).fetchall()
        versions = conn.execute(
            """
            SELECT COALESCE(l.args->>'env', 'inconnu') AS env,
                   l.args->>'version_demandee'         AS version_demandee,
                   COUNT(*)                            AS n
            FROM tool_calls l
            WHERE l.kind = 'transport'
              AND l.args->>'version_demandee' IS NOT NULL
              AND l.created_at >= NOW() - make_interval(days => %s)
            GROUP BY 1, 2
            ORDER BY n DESC
            LIMIT 50
            """,
            (since_days,),
        ).fetchall()
    return {
        "since_days": since_days,
        "total": sum(int(r["n"]) for r in par_cause),
        "by_cause": list(par_cause),
        "protocol_versions_refused": list(versions),
    }


def activation_funnel(active_window_days: int = 30) -> dict:
    """Funnel d'activation (ADR 0017) : distingue COMPTE de USAGE. Un compte avec 0
    appel d'outil n'a jamais rien déclenché (idle, ou handshake OAuth jamais réussi) —
    invisible au monitoring d'outils, détecté ici. `active_window_days` borne « actif »,
    « REST seul » et « bloqué » : les trois comptes portent sur la même fenêtre."""
    active_window_days = max(1, min(int(active_window_days), 365))
    with _agregat("entonnoir d'activation") as conn:
        total = int((conn.execute("SELECT COUNT(*) AS n FROM users").fetchone() or {}).get("n") or 0)
        # Comptes ayant déclenché ≥1 outil MCP dans la fenêtre = vraiment actifs.
        active = int((conn.execute(
            "SELECT COUNT(DISTINCT sub) AS n FROM tool_calls "
            "WHERE kind = 'mcp' AND sub IS NOT NULL "
            "AND created_at >= NOW() - make_interval(days => %s)",
            (active_window_days,),
        ).fetchone() or {}).get("n") or 0)
        # Comptes ayant touché la plateforme (REST) mais SANS aucun appel d'outil, sur
        # la MÊME fenêtre que `active` : connectés-mais-idle (ont ouvert le dashboard,
        # jamais invoqué Claude). ⚠️ Sans fenêtre (jusqu'au 08/10/2026, infra#9), les
        # deux branches lisaient le journal ENTIER par `kind` — la lecture la plus
        # lourde de l'entonnoir, coupée à 10 s en production.
        rest_only = int((conn.execute(
            """
            SELECT COUNT(*) AS n FROM (
                SELECT sub FROM tool_calls WHERE kind = 'rest' AND sub IS NOT NULL
                   AND created_at >= NOW() - make_interval(days => %s)
                EXCEPT
                SELECT sub FROM tool_calls WHERE kind = 'mcp' AND sub IS NOT NULL
                   AND created_at >= NOW() - make_interval(days => %s)
            ) q
            """,
            (active_window_days, active_window_days),
        ).fetchone() or {}).get("n") or 0)
        # Comptes ayant subi ≥1 échec de connecteur dans la fenêtre = bloqués/à débloquer.
        blocked = int((conn.execute(
            "SELECT COUNT(DISTINCT sub) AS n FROM tool_calls "
            "WHERE kind = 'connector' AND sub IS NOT NULL "
            "AND created_at >= NOW() - make_interval(days => %s)",
            (active_window_days,),
        ).fetchone() or {}).get("n") or 0)
    return {
        "window_days": active_window_days,
        "total_accounts": total,
        "active": active,
        "rest_only": rest_only,
        "never_active": max(0, total - active),
        "blocked_by_connector": blocked,
    }


# Plafond de la LISTE nominative d'adoption (les compteurs, eux, portent sur toute
# la population — cf. org_adoption).
_ADOPTION_LIST_CAP = 500


def org_members_by_seniority(org_id: int) -> list[dict]:
    """Les membres de l'org PAR ANCIENNETÉ (`joined_at`, puis `sub`), avec leur email et
    leur dernier appel d'outil émis sous CETTE org (`None` s'il n'y en a jamais eu) —
    la lecture du service commerce (`service.org.members`) : il désigne les membres
    payants dans cet ordre et écrit ses relances à partir de la dernière activité.

    Toute la population, sans plafond : une liste tronquée ferait retomber au gratuit
    des membres qui paient."""
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            """
            SELECT m.sub, u.email, m.org_role, m.is_active, m.joined_at,
                   a.last_activity_at
            FROM org_members m
            LEFT JOIN users u ON u.sub = m.sub
            LEFT JOIN LATERAL (
                SELECT MAX(c.created_at) AS last_activity_at
                FROM tool_calls c
                WHERE c.kind = 'mcp' AND c.sub = m.sub AND c.org_id = m.org_id
            ) a ON TRUE
            WHERE m.org_id = %s
            ORDER BY m.joined_at, m.sub
            """,
            (int(org_id),),
        ).fetchall()]


def org_usage_by_person(org_id: int, since, until) -> list[dict]:
    """Par personne, les appels d'outil RÉUSSIS émis sous l'org sur `[since, until)`, et
    ceux passés sur une clé de plateforme (`key_mode = 'platform'`) — la lecture du
    service commerce (`service.org.usage`). Un échec n'a rien consommé ; une personne
    sans appel n'y figure pas."""
    objet = "consommation par personne d'une org"
    with _connect() as conn:
        # Lu sur les totaux par jour (oto-backend#1147) — consommation FACTURÉE : même
        # réponse qu'au journal seul, bornes `[since, until)` comprises
        # (`tests/db/test_journal_jour_lecteurs.py`).
        src, params = journal_jour.source(
            conn, objet, kinds=("mcp",), mesures=("appels",),
            filtres={"org_id": int(org_id), "ok": True, "sub_non_nul": True},
            depuis=since, jusqu_a=until, haute_incluse=False)
        return [dict(r) for r in conn.execute(
            f"""
            SELECT c.sub, SUM(c.appels)::bigint AS calls,
                   COALESCE(SUM(c.appels) FILTER (WHERE c.key_mode = 'platform'), 0)::bigint
                       AS platform_calls
            FROM ({src}) c
            GROUP BY c.sub
            ORDER BY calls DESC, c.sub
            """,
            params,
        ).fetchall()]


def org_adoption(org_id: int, active_window_days: int = 30) -> dict:
    """Adoption d'une org, membre par membre (pendant org du funnel plateforme).

    Le funnel plateforme distingue COMPTE et USAGE sur toute la base ; ici la même
    question se pose à l'échelle d'une équipe : **qui s'en sert vraiment**. On part de
    la population connue (`org_members` — jamais des appels, sinon un membre à 0 appel
    resterait invisible, ce qui est justement l'information cherchée) et on lui
    raccroche son activité ÉMISE SOUS CETTE ORG (`tool_calls.org_id`, seam
    `current_org`) : un membre actif dans une autre org compte comme inactif ICI.

    Trois lectures dans un seul passage : appels + erreurs dans la fenêtre,
    dernier appel (hors fenêtre, pour dater un décrochage), et échecs de connecteur
    (le membre a essayé mais rien ne résolvait — ≠ jamais essayé, deux actions
    opposées pour l'org_admin). La LISTE nominative est bornée à `_ADOPTION_LIST_CAP`
    (`truncated` le dit) ; les compteurs, eux, couvrent toute la population.
    """
    days = max(1, min(int(active_window_days), 365))
    # Lu sur les totaux par jour (oto-backend#1147) : deux sources sous l'org — la
    # fenêtre (appels, échecs, échecs de connecteur) et tout l'historique consolidé plus
    # le direct (le dernier appel, hors fenêtre).
    objet = "adoption d'une org"
    with _agregat(objet) as conn:
        fen, pf = journal_jour.source(
            conn, objet, prefixe="fen", kinds=("mcp", "connector"), mesures=("appels",),
            filtres={"org_id": int(org_id)}, jours=days)
        tout, pt = journal_jour.source(
            conn, objet, prefixe="tout", kinds=("mcp",), mesures=("dernier_at",),
            filtres={"org_id": int(org_id)})
        rows = [dict(r) for r in conn.execute(
            f"""
            WITH fen AS ({fen}),
                 a AS (SELECT sub,
                              SUM(appels) FILTER (WHERE kind = 'mcp') AS n_calls,
                              SUM(appels) FILTER (WHERE kind = 'mcp' AND NOT ok) AS n_errors,
                              SUM(appels) FILTER (WHERE kind = 'connector') AS n_failures
                         FROM fen GROUP BY sub),
                 d AS (SELECT sub, MAX(dernier_at) AS last_call_at
                         FROM ({tout}) t GROUP BY sub)
            SELECT m.sub, u.email, u.name, m.org_role,
                   COALESCE(a.n_calls, 0)::bigint    AS calls,
                   COALESCE(a.n_errors, 0)::bigint   AS errors,
                   d.last_call_at,
                   COALESCE(a.n_failures, 0)::bigint AS connector_failures
            FROM org_members m
            LEFT JOIN users u ON u.sub = m.sub
            LEFT JOIN a ON a.sub = m.sub
            LEFT JOIN d ON d.sub = m.sub
            WHERE m.org_id = %(org_id)s
            ORDER BY calls DESC, u.email
            """,
            {**pf, **pt, "org_id": int(org_id)},
        ).fetchall()]
    active = sum(1 for r in rows if int(r["calls"] or 0) > 0)
    return {
        "org_id": int(org_id),
        "window_days": days,
        # Compteurs calculés sur TOUTE la population — seule la liste nominative est
        # tronquée. Des agrégats faux sont pires qu'une liste courte.
        "total_members": len(rows),
        "active": active,
        "never_active": len(rows) - active,
        "blocked_by_connector": sum(1 for r in rows if int(r["connector_failures"] or 0) > 0),
        "truncated": len(rows) > _ADOPTION_LIST_CAP,
        "members": rows[:_ADOPTION_LIST_CAP],
    }


# Rétention de l'ÉTIQUETTE d'un run, alignée sur celle de ses faits (#289).
# Deux gardes, une par mode de panne — retirer l'une ou l'autre casse un cas réel :
#   ① l'ÂGE (`finished_at` sinon `started_at`) protège le run fraîchement ouvert dont
#      la journalisation a échoué (`_persist_open` est best-effort) : sans faits dès la
#      première seconde, il ne doit pas s'effacer pour autant ;
#   ② `NOT EXISTS` protège le run ANCIEN toujours vivant (ouvert il y a 40 jours, appels
#      d'hier) : tant qu'il lui reste un fait, sa page n'est pas vide — on ne touche pas
#      à son étiquette.
# À jouer APRÈS la purge du journal (le prédicat lit l'état d'après), dans la MÊME
# transaction (sinon une fenêtre où l'étiquette survit à ce qu'elle étiquette).
_PRUNE_ORPHAN_RUNS = """
    DELETE FROM runs r
     WHERE COALESCE(r.finished_at, r.started_at) < NOW() - make_interval(days => %s)
       AND NOT EXISTS (SELECT 1 FROM tool_calls tc WHERE tc.run_id = r.run_id)
"""


def prune_tool_calls(keep_days: int = 30) -> int:
    """Rétention du journal — **et des runs qui n'ont plus de faits** (#289, ADR 0058-D2).

    Un run EST ses faits : sa ligne `runs` n'est qu'une étiquette (label, doctrine,
    outcome) posée sur les lignes `tool_calls` qui portent son déroulé, et sa page est
    ASSEMBLÉE à la lecture depuis ces faits — on ne la stocke pas. Effacer les faits en
    gardant l'étiquette rendait donc, au 31ᵉ jour, une page de run VIDE sous une ligne
    qui annonçait toujours « prospection Q3 → done ». Les deux partent désormais
    ensemble, dans la même transaction.

    Ce que ça décide côté produit : **un déroulé se garde `keep_days` jours, entier**
    (faits + étiquette), et un run encore actif ne perd jamais son étiquette (garde ②
    ci-dessus). Cela borne aussi ce que rendent les lectures dérivées de `runs`
    (`project_runs`, `project_run_stats`, la pastille de procédure) : l'historique d'un
    projet est celui de la fenêtre de rétention, pas l'éternité.

    La duplication de source, elle, est tranchée depuis (arbitrage J-a du 12/08, cf. le
    bloc « UNE source » plus haut) : le journal EST le run, `runs` n'est qu'un index.
    Cette purge en devient le corollaire naturel — l'index ne survit pas à ce qu'il
    indexe.

    ⚠️ **N'est plus appelée au boot** (ADR 0065, lot 0). Elle supprimait le journal
    à 30 jours **sans l'archiver**, ce qui vidait d'avance ce que le timer
    `oto-journal-archive` (posé le 27/08) devait exporter au froid S3 : mesuré le
    2026-08-28, zéro ligne au-delà de 30 j dans une table de 969 314 — l'archive
    n'aurait jamais trouvé un seul mois complet à prendre. La rétention du journal
    appartient désormais à `deploy/archive_tool_calls.py`, qui exporte PUIS supprime ;
    la moitié « runs sans faits » est devenue `prune_orphan_runs`, jouée par
    `oto-mcp maintenance retention`. Cette fonction reste, exercée par
    `tests/test_run_retention.py` et appelable à la main.

    Retourne le nombre de lignes de JOURNAL supprimées (contrat inchangé) ; le compte
    de runs part au log.
    """
    keep_days = max(1, int(keep_days))
    with _connect() as conn:
        n_calls = conn.execute(
            "DELETE FROM tool_calls WHERE created_at < NOW() - make_interval(days => %s)",
            (keep_days,),
        ).rowcount or 0
        n_runs = conn.execute(_PRUNE_ORPHAN_RUNS, (keep_days,)).rowcount or 0
    if n_calls or n_runs:
        logger.info("prune (>%d j) : %d ligne(s) de journal, %d run(s) sans faits",
                    keep_days, n_calls, n_runs)
    return n_calls


def prune_orphan_runs(keep_days: int = 30) -> int:
    """Efface les ÉTIQUETTES de runs dont les faits ont disparu du journal (#289).

    Un run EST ses faits ; sa ligne `runs` n'est qu'un index. Une étiquette sans
    aucun fait annoncerait « prospection Q3 → done » au-dessus d'une page vide. Même
    borne que le journal, donc, mais exécutée APRÈS lui — d'où sa sortie de
    `prune_tool_calls` : les deux moitiés n'ont plus le même exécutant.

    ⚠️ L'archive du journal ne l'alimente PAS : elle exempte `run_start`/`run_finish`,
    donc un run archivé garde ses faits et son étiquette (#665, option B du 23/09/2026)
    — sa page dit « contenu archivé le … » (`run_content_archived`). Ce filet ne
    joue que pour des faits effacés par un autre chemin (`prune_tool_calls`, appelée
    à la main)."""
    with _connect() as conn:
        n = conn.execute(_PRUNE_ORPHAN_RUNS, (max(1, int(keep_days)),)).rowcount or 0
    if n:
        logger.info("runs sans faits (>%d j) : %d effacé(s)", keep_days, n)
    return n


def count_orphan_runs(keep_days: int = 30) -> int:
    """Ce que `prune_orphan_runs` effacerait — la moitié « à blanc » de la commande."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM runs r "
            " WHERE COALESCE(r.finished_at, r.started_at) < NOW() - make_interval(days => %s)"
            "   AND NOT EXISTS (SELECT 1 FROM tool_calls tc WHERE tc.run_id = r.run_id)",
            (max(1, int(keep_days)),),
        ).fetchone()
        return int(row["n"]) if row else 0


def usage_today_map(sub: str) -> dict[str, int]:
    """TOUS les compteurs du jour d'un sub, en UNE lecture : `{tool: count}`.

    `get_usage_today` répond pour UN outil, ce qui est juste sur le chemin d'un appel —
    mais `status_for` le rappelle une fois par connecteur du catalogue. Mesuré le
    21/08 : **48 requêtes, 410 ms, 24 % du coût de `/api/me`**, pour une table dont une
    seule requête rend la totalité des lignes du jour d'une personne.

    Un outil absent de la map n'a pas de compteur aujourd'hui — c'est `0`, et c'est à
    l'appelant de le lire ainsi (`.get(tool, 0)`), comme la lecture unitaire rend 0 sur
    une ligne absente.
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT tool, count FROM usage WHERE sub = %s AND day = CURRENT_DATE",
            (sub,)).fetchall()
    return {r["tool"]: int(r["count"]) for r in rows}


def get_usage_today(sub: str, tool: str) -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT count FROM usage WHERE sub = %s AND tool = %s AND day = CURRENT_DATE",
            (sub, tool),
        ).fetchone()
        return int(row["count"]) if row else 0


# Colonnes lues pour une entrée d'activité datastore (les deux lectures ci-dessous
# servent le MÊME contrat d'entrée → une seule projection).
#
# Le run cité à côté d'un geste vient du JOURNAL, plus de la table (source unique) :
# une ligne de tableau ne doit pas afficher « → done » quand le déroulé qui l'a touchée
# est ouvert (ou l'inverse). Le LATERAL retrouve l'ouverture par la colonne indexée
# `tool_calls.run_id` (`idx_tool_calls_run`, partiel) — `run_start` la stampe lui-même —
# puis sa clôture par `_run_closure`.
_DS_ACTIVITY_SELECT = f"""
            SELECT l.created_at, l.kind, l.tool, l.args, l.ok, l.error, l.sub, l.email,
                   l.run_id, l.call_uid, r.run_label, r.doctrine, r.outcome
            FROM tool_calls l
            LEFT JOIN LATERAL (
                SELECT s.args->>'label'    AS run_label,
                       s.args->>'doctrine' AS doctrine,
                       f.args->>'outcome'  AS outcome
                  FROM tool_calls s{_run_closure("s")}
                 WHERE s.tool = 'run_start' AND s.run_id = l.run_id
                 ORDER BY s.created_at LIMIT 1
            ) r ON l.run_id IS NOT NULL
"""

# Les DEUX surfaces sont journalisées : 'mcp' = appel d'agent, 'rest' = geste fait
# depuis le dashboard (`calllog.log_rest_call`, même vocabulaire de `tool`). Filtrer
# `kind='mcp'` laissait le parcours VIDE pour qui travaille au cockpit.
_DS_ACTIVITY_KINDS = "l.kind IN ('mcp', 'rest')"


def _owner_clause(owner_type: Optional[str], owner_id: Optional[str]):
    """Borne de TENANT d'un tableau → `(sql, param)`, ou None si inconnue.

    Les axes de corrélation « flous » du journal (nom de tableau, valeur de clé
    métier) ne discriminent PAS le propriétaire : un nom de tableau n'est unique que
    par propriétaire (`uq_user_datastores_owner_ns`) et une clé métier est cherchée
    en sous-chaîne. Non bornés, ils feraient lire à une org les gestes d'une autre.
    On borne donc au tenant qui a **pu** résoudre ce tableau : son org (l'org active
    scope `DatastorePg._resolve`) ou l'acteur lui-même pour un tableau perso.
    Propriétaire inconnu ou tableau d'ÉQUIPE ⇒ None = l'axe flou est abandonné
    (sous-couvrir plutôt que sur-matcher)."""
    if not owner_id:
        return None
    if str(owner_type) == "org":
        return "l.org_id = %s", int(owner_id)
    if str(owner_type) == "user":
        return "l.sub = %s", str(owner_id)
    return None


def _ds_activity_entry(r: dict) -> dict:
    """Ligne de `tool_calls` → entrée d'activité (contrat REST).

    Les champs enrichis (`row_id`/`fields`/`from_status`/`to_status`) sont LUS des
    args journalisés : les lignes MCP et les lignes antérieures à cette version ne
    les portent pas → null / [] sans erreur, aucune migration de données. `row_title`
    est laissé à None ici : le libellé se résout côté surface (elle a le store et le
    champ `role="title"` du schéma), pas dans le SQL du journal.
    """
    args = r.get("args") if isinstance(r.get("args"), dict) else {}
    fields = args.get("fields")
    return deprecations.avec_les_deux_noms({
        "created_at": r.get("created_at"),
        "kind": r.get("kind") or "mcp",
        "tool": r.get("tool"),
        "ok": r.get("ok"),
        "error": r.get("error"),
        "sub": r.get("sub"),
        "email": r.get("email"),
        "run_id": r.get("run_id"),
        "run_label": r.get("run_label"),
        "doctrine": r.get("doctrine"),
        "outcome": r.get("outcome"),
        "row_id": args.get("id"),
        "row_title": None,
        "fields": [str(f) for f in fields] if isinstance(fields, list) else [],
        "from_status": args.get("from_status"),
        "to_status": args.get("to_status"),
        # Le geste (oto#273) : le `call_uid` de l'appel, que le journal des révisions
        # porte en `geste_id`. C'est par lui que le parcours d'une ligne rattache à
        # l'appel les révisions qu'il a écrites (`capabilities/datastore/activity.py`),
        # qui y posent aussi `source`, `acteur` et `revisions`. Vides ici : `tool_calls`
        # ne connaît pas les valeurs.
        "geste_id": r.get("call_uid"),
        "source": None,
        "acteur": None,
        "revisions": [],
    })


def datastore_row_activity(row_id: str, key_value: Optional[str] = None,
                           *, ns_id: int,
                           owner_type: Optional[str] = None,
                           owner_id: Optional[str] = None,
                           limit: int = 50) -> list[dict]:
    """Parcours d'UNE row du datastore (ADR 0046 b4) : les gestes `data_*` du calllog
    qui portent son `_id` (update/lecture ciblée/claim) OU la valeur de sa CLÉ MÉTIER
    (append/batch — l'id n'existe pas encore au write), joints au run (label/doctrine/
    outcome, ADR 0017). Appels d'AGENT (kind='mcp') **et** gestes de dashboard
    (kind='rest'). Fenêtre = la rétention du calllog (prune 30 j) : c'est un journal
    de travail, pas un audit permanent.
    Le match clé passe par `args::text ILIKE` (les args sont petits et le scan est
    borné par `tool LIKE 'data_%'` + rétention) — une valeur de clé courte/ambiguë
    peut sur-matcher, assumé pour un journal indicatif. ⚠️ Assumé DANS LE TENANT
    seulement : cet axe est une recherche de SOUS-CHAÎNE, il est donc borné au
    propriétaire du tableau (même raison qu'en dessous — sans borne, une clé métier
    banale ferait remonter les gestes d'une autre org). L'axe `id`, lui, reste nu :
    c'est un uuid4 non devinable et l'appelant a déjà prouvé son accès à CETTE row.

    ⚠️ **Toujours bornée au TABLEAU de la ligne** (`ns_id` résolu serveur, journalisé
    sur les deux faces depuis le 31/07/2026). Incident du 08/10/2026 : sans cette
    borne, aucun des deux axes ne tombait sur un index — `args->>'id'` n'en a pas, la
    sous-chaîne non plus —, et `ORDER BY created_at DESC LIMIT` parcourait le journal
    entier (~12 M lignes) jusqu'à trouver de quoi remplir la page : jusqu'à 302 s par
    appel, douze connexions sur vingt-six prises, la prod MCP par terre. Le `ns_id`
    passe par `idx_tool_calls_ns` (partiel, `data_*` seulement) : on ne lit plus que
    les appels de CE tableau. Prix assumé : un geste antérieur à la journalisation du
    `ns_id` n'y figure plus — il sort de toute façon de la rétention.
    La lecture est en plus bornée en durée (`_agregat`, #1145)."""
    limit = max(1, min(int(limit), 200))
    clauses = [_DS_ACTIVITY_KINDS, "l.tool LIKE 'data\\_%%'", "l.args->>'ns_id' = %s"]
    params: list[Any] = [str(int(ns_id))]
    match = ["l.args->>'id' = %s"]
    params.append(str(row_id))
    key_bound = _owner_clause(owner_type, owner_id)
    if key_value is not None and str(key_value).strip() and key_bound:
        sql, bound = key_bound
        match.append(f"(l.args::text ILIKE %s AND {sql})")
        params += [f"%{str(key_value).strip()}%", bound]
    clauses.append("(" + " OR ".join(match) + ")")
    params.append(limit)
    with _agregat("parcours d'une ligne") as conn:
        rows = conn.execute(
            f"""{_DS_ACTIVITY_SELECT}
            WHERE {' AND '.join(clauses)}
            ORDER BY l.created_at DESC LIMIT %s
            """,
            tuple(params),
        ).fetchall()
        return [_ds_activity_entry(dict(r)) for r in rows]


def datastore_activity(ns_id: int, namespace: Optional[str] = None,
                                 *, owner_type: Optional[str] = None,
                                 owner_id: Optional[str] = None,
                                 limit: int = 50) -> list[dict]:
    """Activité de TOUT un tableau : les gestes `data_*` qui l'ont visé, agent (MCP)
    comme dashboard (REST).

    L'axe de corrélation est le `ns_id` **résolu serveur**, sur les DEUX surfaces : la
    face REST le tient de sa route, la face MCP du relevé d'appel que
    `DatastorePg._resolve` remplit (`session_org.note_call_trace`). Le journal cite donc
    l'entité, quelle que soit la chaîne tapée — `data_write("leads-clients")`,
    `data_write("160")` et `data_write("slot:vivier")` retombent sur la même ligne, et
    un renommage de tableau n'orpheline plus son historique.

    ⚠️ L'axe NOM subsiste en **repli, uniquement pour l'historique** écrit avant que le
    ns_id ne soit journalisé (il s'éteint de lui-même avec la rétention 30 j du calllog).
    Il est **BORNÉ AU PROPRIÉTAIRE, jamais matché nu** (`_owner_clause`) : un nom n'est
    unique que par propriétaire (`uq_user_datastores_owner_ns`), deux orgs ont chacune le
    droit d'avoir un `leads`, et un `args->>'namespace' = 'leads'` sans borne ferait lire
    à l'org B les gestes de l'org A. Résiduel de ce repli, borné dans le temps : un
    homonyme DANS le même tenant reste indistinguable (même tenant, pas une fuite).
    """
    limit = max(1, min(int(limit), 200))
    ns_id = int(ns_id)
    match = ["l.args->>'ns_id' = %s"]
    params: list[Any] = [str(ns_id)]
    # Repli historique : les formes sous lesquelles un agent a pu nommer CE tableau
    # avant que le ns_id résolu ne soit journalisé — son id en texte et son nom canonique.
    names = [str(ns_id)]
    name = (namespace or "").strip()
    if name and name != str(ns_id):
        names.append(name)
    name_bound = _owner_clause(owner_type, owner_id)
    if name_bound:
        sql, bound = name_bound
        # ⚠️ **Les DEUX noms, et ce n'est pas de la complaisance : c'est du STOCKAGE.**
        # `tool_calls.args` garde les arguments tels qu'ils ont été reçus. Au 08/09/2026,
        # 149 379 appels y portent la clé `namespace` et zéro `datastore` — l'ancien nom
        # du paramètre. Après le renommage, les nouveaux appels écriront `datastore` et
        # les anciens garderont `namespace` : chercher un seul des deux rend un journal
        # amputé de la moitié de l'histoire, sans erreur ni trace. Le mode d'échec est
        # un ensemble vide, et un journal vide se lit « rien ne s'est passé » — c'est
        # exactement le symptôme qu'on cherchait à corriger sur cette route.
        # Cette ligne n'a pas de date de péremption : elle en aura une le jour où la
        # rétention aura effacé le dernier appel écrit sous l'ancien nom.
        match.append(
            f"(COALESCE(l.args->>'datastore', l.args->>'namespace') = ANY(%s) "
            f"AND {sql})")
        params += [names, bound]
    params.append(limit)
    with _agregat("activité d'un tableau") as conn:
        rows = conn.execute(
            f"""{_DS_ACTIVITY_SELECT}
            WHERE {_DS_ACTIVITY_KINDS} AND l.tool LIKE 'data\\_%%'
                  AND ({' OR '.join(match)})
            ORDER BY l.created_at DESC LIMIT %s
            """,
            tuple(params),
        ).fetchall()
        return [_ds_activity_entry(dict(r)) for r in rows]


def premiers_appels(org_ids: list[int]) -> dict[int, Optional[str]]:
    """Le PREMIER appel journalisé de chaque org (`tool_calls`, MCP et REST) — `None`
    pour une org sans aucun appel. Une sonde d'index par org (`idx_tool_calls_org`),
    jamais un balayage. ⚠️ Le journal ne garde que ~90 jours : passé ce délai, c'est
    le plus vieux appel CONSERVÉ — l'appelant qui veut une date stable la fige."""
    if not org_ids:
        return {}
    with _connect() as conn:
        rows = conn.execute(
            "SELECT o.id AS org_id, "
            "  (SELECT created_at FROM tool_calls tc WHERE tc.org_id = o.id "
            "   ORDER BY tc.created_at ASC LIMIT 1) AS premier "
            "FROM unnest(%s::bigint[]) AS o(id)",
            ([int(i) for i in org_ids],)).fetchall()
    return {int(r["org_id"]): r["premier"] for r in rows}
