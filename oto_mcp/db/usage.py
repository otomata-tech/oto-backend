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
from typing import Any, Iterator, Optional

import psycopg

logger = logging.getLogger(__name__)

from .. import deprecations
from . import journal_calls
from ._conn import _connect


def increment_usage(sub: str, tool: str) -> int:
    """Incrémente le compteur (sub, tool, today). Retourne la nouvelle valeur."""
    with _connect() as conn:
        row = conn.execute(
            """
            INSERT INTO usage (sub, tool, day, count)
            VALUES (%s, %s, CURRENT_DATE, 1)
            ON CONFLICT(sub, tool, day) DO UPDATE SET count = usage.count + 1
            RETURNING count
            """,
            (sub, tool),
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
                 request_id, call_uid, effective_sub, error_kind)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s)
            """,
            (
                row.get("server") or "oto", row.get("kind") or "mcp",
                row.get("sub"), row.get("email"),
                row["tool"], json.dumps(row.get("args")) if row.get("args") is not None else None,
                bool(row.get("ok")), row.get("error"), row.get("duration_ms"),
                row.get("session_id"), row.get("run_id"), row.get("org_id"),
                row.get("client_id"), row.get("sentry_event_id"),
                # #117 — discriminant par appel. Absents des gestes REST et des
                # écritures hors MCP : la ligne les porte à NULL, sans branche ici.
                row.get("request_id"), row.get("call_uid"), row.get("effective_sub"),
                # oto#25 lot (b1) — résultat de la taxonomie sur échec, NULL sur
                # succès et sur les gestes REST (calllog._error_kind ne les touche pas).
                row.get("error_kind"),
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


def insert_run(
    run_id: str, *, sub: Optional[str], org_id: Optional[int], label: str,
    guide: Optional[str] = None, project_id: Optional[int] = None,
) -> None:
    """Pose l'INDEX d'un run (best-effort, idempotent sur `run_id`).

    ⚠️ Ce n'est pas « persister le run » : le run est ses faits (cf. le bloc ci-dessus).
    Cette ligne existe pour porter `project_id` — le projet actif gelé à l'ouverture
    (ADR 0032 §5/§6, B3), qu'aucune colonne de `tool_calls` ne porte ; NULL hors projet.
    `label`/`doctrine` y sont écrits par héritage et ne sont plus lus."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO runs (run_id, sub, org_id, project_id, label, doctrine) "
            "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (run_id) DO NOTHING",
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

    Une seule question, un seul appelant : le refus de `@claimed`, qui décrivait un
    ÉTAT (« aucune réservation active ») là où le problème est un MOMENT — l'appel
    arrive APRÈS `run_finish`, qui a libéré les baux (#645). Pour le dire, il faut
    savoir que le run est clos ; c'est tout ce que cette fonction rend.

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


def recent_runs(sub: str, org_id: Optional[int], limit: int = 5) -> list[dict]:
    """Les `limit` derniers runs d'un (sub, org), plus récent d'abord — l'anticipation
    du contexte injecté (#50 bloc C) + la boucle d'usage.

    Lus du JOURNAL (le run est ses faits) ; la table n'est jointe que pour `project_id`,
    le seul champ qu'elle sait. Conséquence assumée : un run dont l'ouverture n'a pas
    été journalisée n'apparaît plus au handshake — mieux qu'une étiquette sans déroulé."""
    with _connect() as conn:
        rows = conn.execute(
            f"""
            WITH j AS ({_runs_from_journal(
                " AND s.sub = %s AND s.org_id IS NOT DISTINCT FROM %s")})
            SELECT j.run_id, j.label, j.doctrine, j.outcome, x.project_id,
                   j.started_at, j.finished_at, j.last_seen_at
              FROM j LEFT JOIN runs x ON x.run_id = j.run_id
             ORDER BY j.started_at DESC LIMIT %s
            """,
            (sub, org_id, limit),
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


def project_run_tools(project_id: int, limit: int = 200) -> list[str]:
    """Outils réellement APPELÉS par les runs d'un projet — la part « usage observé »
    de l'inventaire dérivé (ADR 0035 B4 : surface d'un projet = refs des procédures
    liées ∪ slots×bindings ∪ runs). Distincts, plus fréquents d'abord ; brut (spine/
    méta inclus — le consommateur cure).

    Seul usage LÉGITIME de la table `runs` en jointure : on ne lui demande que
    `project_id` (son unique champ crédible), la matière vient du journal."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT tc.tool, count(*) AS n FROM tool_calls tc "
            "JOIN runs r ON r.run_id = tc.run_id "
            "WHERE r.project_id = %s AND tc.kind = 'mcp' "
            "GROUP BY tc.tool ORDER BY n DESC, tc.tool LIMIT %s",
            (project_id, limit),
        ).fetchall()
    return [r["tool"] for r in rows]


_PROJECT_SCOPE = (
    " AND s.run_id IN (SELECT run_id FROM runs WHERE project_id = %s)")
"""Prédicat d'ouverture qui borne `_runs_from_journal` à UN projet.

⚠️ Le `WHERE x.project_id` du SELECT extérieur ne suffit pas : il filtre le RÉSULTAT
d'un CTE qui a déjà reconstruit **tous** les runs de la plateforme — un LATERAL
`max(created_at)` par `run_start`, plus la clôture, sur les ~900 k lignes du journal.
Le coût suivait donc le journal entier, pas le projet : incident du 2026-08-27, où
chaque `oto_project` tenait la boucle 185 s (les appels DB de ce module sont
synchrones, cf. `docs/event-loop-perf.md`) et gelait la plateforme entière —
tenants tiers compris. Poussé dans le CTE, le semi-join part d'`idx_runs_project` et
seuls les runs du projet sont reconstruits.

Littéral de ce module, comme tout `extra` (le `%s` est lié, jamais interpolé)."""


def project_runs(project_id: int, guide: Optional[str] = None,
                 limit: int = 20) -> list[dict]:
    """Derniers runs d'un projet (plus récent d'abord), optionnellement filtrés sur une
    `doctrine` (slug) — alimente la pastille ok/échec du viewer de procédure (refonte UX,
    ADR 0032/0017). `outcome` NULL = run ouvert / non clôturé.

    L'axe PROJET vient de l'index (`runs.project_id`), tout le reste du journal — le
    filtre `doctrine` inclus : filtrer sur la colonne de la table ferait apparaître dans
    la pastille d'une procédure un run que le journal rattache à une autre."""
    guide_clause = " AND j.doctrine = %s" if guide is not None else ""
    params: list = ([project_id, project_id]
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

    JOIN (pas LEFT JOIN) sur les faits : un index sans déroulé journalisé ne compte pas
    — la question posée est « cette procédure a-t-elle SERVI », et un run sans le
    moindre fait ne le prouve pas."""
    with _connect() as conn:
        row = conn.execute(
            f"""
            WITH j AS ({_runs_from_journal(_PROJECT_SCOPE)})
            SELECT count(*) AS n,
                   array_agg(DISTINCT j.doctrine)
                       FILTER (WHERE j.doctrine IS NOT NULL) AS doctrines
              FROM runs x JOIN j ON j.run_id = x.run_id
             WHERE x.project_id = %s
            """,
            (project_id, project_id),
        ).fetchone()
    return {"runs": int(row["n"] or 0), "doctrines": list(row["doctrines"] or [])}


#: Fenêtre pendant laquelle un signalement STRICTEMENT identique du même auteur
#: est tenu pour un REJEU, pas pour une seconde occurrence. Dix minutes : un rejeu
#: se produit dans la seconde ou la minute (coupure réseau, réponse non vue,
#: relance d'un pas de procédure), tandis qu'un même défaut qui se reproduit
#: vraiment ne se raconte pas deux fois mot pour mot en si peu de temps.
_REJEU_SIGNAL_MINUTES = 10


def insert_usage_signal(
    *, sub: Optional[str], org_id: Optional[int], signal: str, kind: str,
    target: Optional[str], body: Optional[str], session_id: Optional[str],
    source: str = "agent",
) -> tuple[int, bool]:
    """Dépose un signalement → `(id, deja_depose)`.

    Un signalement identique du même auteur dans les dernières minutes rend l'id
    du PREMIER, sans en créer un second (#684/#685 — le même retour déposé deux
    fois à ONZE SECONDES d'écart par le même agent, sur des doublons créés en
    silence dans le CRM d'un client). L'insertion était nue : deux dépôts, deux
    lignes, aucun signe.

    ⚠️ **C'est notre propre boucle de retour qui se faisait le défaut qu'elle
    sert à remonter** — une information produite (« tu m'as déjà dit ça ») que
    personne ne rendait. Elle n'a droit à aucun traitement de faveur.

    ⚠️ **On ne REFUSE pas, on rend l'existant et on le DIT.** Refuser ferait
    perdre son retour à un agent qui redépose de bonne foi ; se taire laisserait
    croire à deux occurrences là où il n'y a qu'un rejeu, et gonflerait la pile
    d'arbitrage de faux volume. Le second appel reçoit donc le même identifiant,
    et sait que c'est le même.

    ⚠️ La comparaison porte sur le CORPS ENTIER, pas sur le sujet : deux
    signalements sur le même outil sont normaux et fréquents — c'est le texte à
    l'identique qui trahit le rejeu.

    ⚠️ **Et sur l'ORGANISATION, faute de quoi ce cran détruirait la donnée qu'il
    prétend ranger.** Les deux dépôts qui ont motivé ce lot n'étaient PAS un
    rejeu : l'auteur avait adressé le premier à la mauvaise organisation et l'a
    redéposé, texte identique, sur la bonne — il le dit lui-même dans le second
    corps. Sans cette clause, le second aurait été fusionné dans le premier et le
    signalement serait resté classé au mauvais endroit, définitivement.

    ⚠️ **Le défaut de fond reste entier et n'est pas ici** : corriger l'adresse
    d'un signalement impose de le redéposer, parce que le réaiguillage n'existe
    que côté administrateur. L'émetteur n'a pas d'autre recours que le doublon.
    Tant que ça dure, ces doublons-là sont légitimes."""
    with _connect() as conn:
        vu = conn.execute(
            """
            SELECT id FROM usage_signals
             WHERE sub IS NOT DISTINCT FROM %s
               AND org_id IS NOT DISTINCT FROM %s
               AND signal = %s AND kind = %s
               AND target IS NOT DISTINCT FROM %s
               AND body IS NOT DISTINCT FROM %s
               AND created_at > NOW() - (%s || ' minutes')::interval
             ORDER BY id DESC LIMIT 1
            """,
            (sub, org_id, signal, kind, target, body,
             str(_REJEU_SIGNAL_MINUTES)),
        ).fetchone()
        if vu is not None:
            return int(vu["id"]), True
        row = conn.execute(
            """
            INSERT INTO usage_signals
                (sub, org_id, signal, kind, target, body, session_id, source)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (sub, org_id, signal, kind, target, body, session_id, source),
        ).fetchone()
        return int(row["id"]), False


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
    sql = ("SELECT s.id, s.created_at, s.sub, u.email, u.name, s.org_id, s.signal, "
           "s.kind, s.target, s.body, s.session_id, s.source, s.status, "
           "s.resolved_at, s.resolved_by, s.resolution, s.notified_at "
           "FROM usage_signals s LEFT JOIN users u ON u.sub = s.sub")
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
    sql += " ORDER BY s.created_at DESC LIMIT %s"
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
    aller-retour séparé côté appelant."""
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            """
            SELECT s.id, s.sub, u.email, u.name, u.locale, s.signal, s.kind, s.target,
                   s.body, s.created_at, s.status, s.resolution, s.resolved_at
            FROM usage_signals s LEFT JOIN users u ON u.sub = s.sub
            WHERE s.status = ANY(%s) AND s.notified_at IS NULL AND s.sub IS NOT NULL
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
    même règle exacte que le journal d'audit. Sans lui : plateforme-wide (défaut admin)."""
    limit = max(1, min(int(limit), 500))
    org_clause = " AND s.org_id = %s" if org_id is not None else ""
    params: list[Any] = ([int(org_id)] if org_id is not None else []) + [limit]
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            f"""
            WITH j AS ({_runs_from_journal(org_clause)})
            SELECT j.run_id,
                   COALESCE(j.doctrine, j.label) AS slug,
                   j.label, j.doctrine, j.doctrine_version,
                   j.sub, u.email, u.name,
                   j.started_at, j.finished_at, j.outcome, j.last_seen_at,
                   COALESCE(c.n_calls, 0) AS n_calls
              FROM j
              LEFT JOIN users u ON u.sub = j.sub
              LEFT JOIN (
                  SELECT run_id, count(*) AS n_calls FROM tool_calls
                   WHERE run_id IS NOT NULL GROUP BY run_id
              ) c ON c.run_id = j.run_id
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
    portait sans ouvrir sa fiche, et sans jamais rendre une valeur."""
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
    with _connect() as conn:
        # Alias tool_name/called_at : compat avec l'UI admin existante.
        rows = conn.execute(
            f"""
            SELECT l.id, l.sub, u.email, u.name, l.tool AS tool_name, l.created_at AS called_at,
                   l.duration_ms, l.ok, l.error, l.session_id, l.run_id, l.org_id,
                   l.sentry_event_id, {journal_calls.ARG_KEYS_SQL} AS arg_keys
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
    (TRONQUÉS à l'écriture par `truncated_args` — jamais le payload intégral) +
    axes de corrélation (session_id, run_id, org_id + nom, client_id)."""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT l.id, l.kind, l.server, l.sub, COALESCE(u.email, l.email) AS email,
                   u.name, l.tool, l.args, l.ok, l.error, l.error_kind, l.duration_ms,
                   l.created_at,
                   l.session_id, l.run_id, l.org_id, o.name AS org_name, l.client_id,
                   l.sentry_event_id
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
    bornes incluses). JAMAIS d'args ni de secret (garantie calllog).

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
    with _connect() as conn:
        # PREMIÈRE commande de la transaction — `SET TRANSACTION` est refusé après
        # une requête. Sa portée est cette transaction seule : la connexion revient
        # au pool en `read committed` (vérifié sur un PostgreSQL réel, 2026-09-01).
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
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
                   l.duration_ms, {_AUDIT_KEYSET_AT} AS _keyset_at
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


def instruction_usage(
    subs: list[str], tool: str, slug: Optional[str], days: int = 30,
    *, slug_key: str = "slug",
) -> dict:
    """Usage d'un guide dérivé de `tool_calls` (ADR 0014, « guide = process
    = log d'usage ») : combien de fois elle a été chargée par l'agent, par qui,
    et la distribution journalière sur `days` jours.

    `tool` = le tool de lecture de guide (oto_procedure ; slug=None pour la base, sinon filtré par
    `args->>'slug'` pour une skill). Scopé aux `subs` (membres de
    l'org). Lecture pure ; renvoie {count, callers, daily{date:str -> n}}.

    `slug_key` = la CLÉ de `args` qui porte la procédure. C'est ce qui rend cette
    fonction réutilisable pour compter autre chose que des chargements : un
    **déroulé** est le fait `run_start`, et il nomme sa procédure sous une AUTRE clé
    que `slug` — `_ARG_PROCEDURE`, celle-là même que `_runs_from_journal` lit pour
    reconstruire les runs depuis la MÊME table. Une seule requête, deux lectures,
    plutôt qu'un second chemin qui dériverait du premier.

    Liste FERMÉE de littéraux de ce module : la clé est interpolée dans le SQL,
    jamais fournie par un appelant."""
    if not subs:
        return {"count": 0, "callers": [], "daily": {}}
    if slug_key not in _ARGS_PROCEDURE_OK:
        raise ValueError(f"slug_key non supporté: {slug_key!r}")
    days = max(1, min(int(days), 365))
    slug_clause = f" AND l.args->>'{slug_key}' = %s" if slug is not None else ""
    base_params: list[Any] = [subs, tool]
    if slug is not None:
        base_params.append(slug)
    with _connect() as conn:
        callers = conn.execute(
            f"""
            SELECT u.email, COUNT(*) AS n
            FROM tool_calls l LEFT JOIN users u ON u.sub = l.sub
            WHERE l.sub = ANY(%s) AND l.tool = %s{slug_clause} AND l.ok
            GROUP BY u.email ORDER BY n DESC
            """,
            tuple(base_params),
        ).fetchall()
        daily = conn.execute(
            f"""
            SELECT (l.created_at AT TIME ZONE 'UTC')::date AS d, COUNT(*) AS n
            FROM tool_calls l
            WHERE l.sub = ANY(%s) AND l.tool = %s{slug_clause} AND l.ok
              AND l.created_at >= NOW() - make_interval(days => %s)
            GROUP BY d
            """,
            tuple(base_params + [days]),
        ).fetchall()
    return {
        "count": sum(int(r["n"]) for r in callers),
        "callers": [r["email"] for r in callers if r["email"]],
        "daily": {str(r["d"]): int(r["n"]) for r in daily},
    }


def tool_call_stats(since_days: int = 7, *, org_id: Optional[int] = None,
                    sub: Optional[str] = None) -> dict:
    """Agrégats pour le dashboard de monitoring sur les `since_days` derniers jours :
    total, échecs, ventilation par tool / par user / par jour.

    Défaut = PLATEFORME-wide (console `/platform/monitoring`). `org_id`/`sub`
    RESTREIGNENT la fenêtre : `org_id` = l'activité d'UN workspace,
    `sub` = celle d'UN membre — la vue « activité de CE workspace / de moi » de
    l'overview ne fuite plus le trafic des autres orgs/users (oto/#5.2)."""
    since_days = max(1, min(int(since_days), 365))

    def _where(prefix: str = "") -> tuple[str, list]:
        clauses = [f"{prefix}kind = 'mcp'",
                   f"{prefix}created_at >= NOW() - make_interval(days => %s)"]
        params: list = [since_days]
        if org_id is not None:
            clauses.append(f"{prefix}org_id = %s"); params.append(org_id)
        if sub is not None:
            clauses.append(f"{prefix}sub = %s"); params.append(sub)
        return " AND ".join(clauses), params

    w, wp = _where()
    wl, wlp = _where("l.")
    with _connect() as conn:
        totals = conn.execute(
            f"""
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE NOT ok) AS errors,
                   COUNT(DISTINCT sub) AS users
            FROM tool_calls WHERE {w}
            """,
            tuple(wp),
        ).fetchone() or {}
        by_tool = conn.execute(
            f"""
            SELECT tool AS tool_name,
                   COUNT(*) AS calls,
                   COUNT(*) FILTER (WHERE NOT ok) AS errors,
                   ROUND(AVG(duration_ms))::int AS avg_ms,
                   ROUND(percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms))::int AS p95_ms
            FROM tool_calls WHERE {w}
            GROUP BY tool ORDER BY calls DESC LIMIT 100
            """,
            tuple(wp),
        ).fetchall()
        by_user = conn.execute(
            f"""
            SELECT l.sub, u.email, u.name,
                   COUNT(*) AS calls,
                   COUNT(*) FILTER (WHERE NOT l.ok) AS errors
            FROM tool_calls l
            LEFT JOIN users u ON u.sub = l.sub
            WHERE {wl}
            GROUP BY l.sub, u.email, u.name ORDER BY calls DESC LIMIT 100
            """,
            tuple(wlp),
        ).fetchall()
        by_day = conn.execute(
            f"""
            SELECT to_char(created_at::date, 'YYYY-MM-DD') AS day,
                   COUNT(*) AS calls,
                   COUNT(*) FILTER (WHERE NOT ok) AS errors
            FROM tool_calls WHERE {w}
            GROUP BY created_at::date ORDER BY created_at::date
            """,
            tuple(wp),
        ).fetchall()
    return {
        "since_days": since_days,
        "total_calls": int((totals or {}).get("total") or 0),
        "error_count": int((totals or {}).get("errors") or 0),
        "active_users": int((totals or {}).get("users") or 0),
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
    `sub` vient du jeton présenté (fiable) ; `org_id` vient de l'org de CONSULTATION
    revendiquée en en-tête par le client (`RestCallLogger`, best-effort) — une requête
    sans cet en-tête ne porte aucune org et sort donc du filtre. Un total à 0 sous
    `org_id` ne prouve pas l'inactivité de l'org."""
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
    with _connect() as conn:
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
    out = {
        "since_days": since_days,
        "total_calls": int((totals or {}).get("total") or 0),
        "error_count": int((totals or {}).get("errors") or 0),
        "active_users": int((totals or {}).get("users") or 0),
        "last_call_at": (totals or {}).get("last_call_at"),
        "by_route": list(by_route),
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


def connector_failure_stats(since_days: int = 7, *, org_id: Optional[int] = None) -> dict:
    """Lentille santé connecteurs (ADR 0017, kind='connector') : échecs de résolution
    de credential par provider — combien, combien d'users distincts touchés, dernier
    échec. C'est le signal « ce connecteur ne résout pas » (compte actif sans clé valide).

    `org_id` = les échecs subis SOUS cette org (lentille org_admin : « quel connecteur
    bloque MES membres »). Sans lui : plateforme-wide."""
    since_days = max(1, min(int(since_days), 365))
    org_clause = " AND l.org_id = %s" if org_id is not None else ""
    params: list[Any] = [since_days] + ([int(org_id)] if org_id is not None else [])
    with _connect() as conn:
        by_provider = conn.execute(
            f"""
            SELECT l.tool AS provider,
                   COUNT(*) AS failures,
                   COUNT(DISTINCT l.sub) AS users_affected,
                   MAX(l.created_at) AS last_at
            FROM tool_calls l
            WHERE l.kind = 'connector'
              AND l.created_at >= NOW() - make_interval(days => %s){org_clause}
            GROUP BY l.tool
            ORDER BY failures DESC
            LIMIT 100
            """,
            tuple(params),
        ).fetchall()
    return {
        "since_days": since_days,
        "total_failures": sum(int(r["failures"]) for r in by_provider),
        "by_provider": list(by_provider),
    }


def activation_funnel(active_window_days: int = 30) -> dict:
    """Funnel d'activation (ADR 0017) : distingue COMPTE de USAGE. Un compte avec 0
    appel d'outil n'a jamais rien déclenché (idle, ou handshake OAuth jamais réussi) —
    invisible au monitoring d'outils, détecté ici. `active_window_days` borne « actif »."""
    active_window_days = max(1, min(int(active_window_days), 365))
    with _connect() as conn:
        total = int((conn.execute("SELECT COUNT(*) AS n FROM users").fetchone() or {}).get("n") or 0)
        # Comptes ayant déclenché ≥1 outil MCP dans la fenêtre = vraiment actifs.
        active = int((conn.execute(
            "SELECT COUNT(DISTINCT sub) AS n FROM tool_calls "
            "WHERE kind = 'mcp' AND sub IS NOT NULL "
            "AND created_at >= NOW() - make_interval(days => %s)",
            (active_window_days,),
        ).fetchone() or {}).get("n") or 0)
        # Comptes ayant touché la plateforme (REST) mais SANS aucun appel d'outil :
        # connectés-mais-idle (ont ouvert le dashboard, jamais invoqué Claude).
        rest_only = int((conn.execute(
            """
            SELECT COUNT(*) AS n FROM (
                SELECT sub FROM tool_calls WHERE kind = 'rest' AND sub IS NOT NULL
                GROUP BY sub
                EXCEPT
                SELECT sub FROM tool_calls WHERE kind = 'mcp' AND sub IS NOT NULL
            ) q
            """
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
    with _connect() as conn:
        rows = [dict(r) for r in conn.execute(
            """
            SELECT m.sub, u.email, u.name, m.org_role,
                   COALESCE(a.n_calls, 0)    AS calls,
                   COALESCE(a.n_errors, 0)   AS errors,
                   a.last_call_at,
                   COALESCE(f.n_failures, 0) AS connector_failures
            FROM org_members m
            LEFT JOIN users u ON u.sub = m.sub
            LEFT JOIN LATERAL (
                SELECT COUNT(*) FILTER (
                           WHERE c.created_at >= NOW() - make_interval(days => %s)) AS n_calls,
                       COUNT(*) FILTER (
                           WHERE c.created_at >= NOW() - make_interval(days => %s)
                             AND NOT c.ok) AS n_errors,
                       MAX(c.created_at) AS last_call_at
                FROM tool_calls c
                WHERE c.kind = 'mcp' AND c.sub = m.sub AND c.org_id = m.org_id
            ) a ON TRUE
            LEFT JOIN LATERAL (
                SELECT COUNT(*) AS n_failures
                FROM tool_calls c
                WHERE c.kind = 'connector' AND c.sub = m.sub AND c.org_id = m.org_id
                  AND c.created_at >= NOW() - make_interval(days => %s)
            ) f ON TRUE
            WHERE m.org_id = %s
            ORDER BY calls DESC, u.email
            """,
            (days, days, days, int(org_id)),
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

    Un run EST ses faits ; sa ligne `runs` n'est qu'un index. Une fois le journal du
    mois archivé au froid et supprimé, l'étiquette resterait à annoncer « prospection
    Q3 → done » au-dessus d'une page vide. Même borne que le journal, donc, mais
    exécutée APRÈS lui — d'où sa sortie de `prune_tool_calls` : les deux moitiés
    n'ont plus le même exécutant (l'archive pour le journal, le timer de maintenance
    pour les étiquettes)."""
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
                   l.run_id, r.run_label, r.doctrine, r.outcome
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
    })


def datastore_row_activity(row_id: str, key_value: Optional[str] = None,
                           *, owner_type: Optional[str] = None,
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
    c'est un uuid4 non devinable et l'appelant a déjà prouvé son accès à CETTE row."""
    limit = max(1, min(int(limit), 200))
    clauses = [_DS_ACTIVITY_KINDS, "l.tool LIKE 'data\\_%%'"]
    params: list[Any] = []
    match = ["l.args->>'id' = %s"]
    params.append(str(row_id))
    key_bound = _owner_clause(owner_type, owner_id)
    if key_value is not None and str(key_value).strip() and key_bound:
        sql, bound = key_bound
        match.append(f"(l.args::text ILIKE %s AND {sql})")
        params += [f"%{str(key_value).strip()}%", bound]
    clauses.append("(" + " OR ".join(match) + ")")
    params.append(limit)
    with _connect() as conn:
        rows = conn.execute(
            f"""{_DS_ACTIVITY_SELECT}
            WHERE {' AND '.join(clauses)}
            ORDER BY l.created_at DESC LIMIT %s
            """,
            tuple(params),
        ).fetchall()
        return [_ds_activity_entry(dict(r)) for r in rows]


def datastore_namespace_activity(ns_id: int, namespace: Optional[str] = None,
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
        match.append(f"(l.args->>'namespace' = ANY(%s) AND {sql})")
        params += [names, bound]
    params.append(limit)
    with _connect() as conn:
        rows = conn.execute(
            f"""{_DS_ACTIVITY_SELECT}
            WHERE {_DS_ACTIVITY_KINDS} AND l.tool LIKE 'data\\_%%'
                  AND ({' OR '.join(match)})
            ORDER BY l.created_at DESC LIMIT %s
            """,
            tuple(params),
        ).fetchall()
        return [_ds_activity_entry(dict(r)) for r in rows]
