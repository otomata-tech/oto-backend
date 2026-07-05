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
    """Sink otomata-calllog : insère un row canonique (server, sub, email, tool,
    args, ok, error, duration_ms) + corrélation OTO-LOCALE (session_id, run_id ;
    ADR 0017, absents du contrat canonique → enrichis par le sink). `kind` discrimine
    l'événement ('mcp' défaut / 'rest' / 'connector', ADR 0017 « un seul flux »).
    Best-effort côté middleware — jamais bloquant."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO tool_calls
                (server, kind, sub, email, tool, args, ok, error, duration_ms, session_id, run_id, org_id)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s)
            """,
            (
                row.get("server") or "oto", row.get("kind") or "mcp",
                row.get("sub"), row.get("email"),
                row["tool"], json.dumps(row.get("args")) if row.get("args") is not None else None,
                bool(row.get("ok")), row.get("error"), row.get("duration_ms"),
                row.get("session_id"), row.get("run_id"), row.get("org_id"),
            ),
        )


def insert_run(
    run_id: str, *, sub: Optional[str], org_id: Optional[int], label: str,
    doctrine: Optional[str] = None, project_id: Optional[int] = None,
) -> None:
    """Persiste l'ouverture d'un run (best-effort, idempotent sur `run_id`). La pile
    session-scopée de `doctrine_run.py` reste la source du run ACTIF ; cette ligne
    est la trace durable (label/doctrine). `project_id` = projet actif gelé au start
    (ADR 0032 §5/§6, B3) ; NULL hors projet."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO runs (run_id, sub, org_id, project_id, label, doctrine) "
            "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (run_id) DO NOTHING",
            (run_id, sub, org_id, project_id, label, doctrine),
        )


def finish_run(run_id: str, outcome: str, note: Optional[str] = None,
               sub: Optional[str] = None) -> None:
    """Clôt un run persisté (outcome + note + finished_at). No-op si run_id inconnu
    (run ouvert dans une session sans persistance, ou déjà prune). `sub` (≠ None)
    SCOPE la clôture au propriétaire du run — un run_id d'autrui (session réutilisée,
    #108) n'est pas clôturable ; `sub=None` (stdio local) matche les runs sans sub."""
    with _connect() as conn:
        conn.execute(
            "UPDATE runs SET outcome = %s, note = %s, finished_at = NOW() "
            "WHERE run_id = %s AND sub IS NOT DISTINCT FROM %s",
            (outcome, note, run_id, sub),
        )


def recent_runs(sub: str, org_id: Optional[int], limit: int = 5) -> list[dict]:
    """Les `limit` derniers runs d'un (sub, org), plus récent d'abord. Sert
    l'anticipation du contexte injecté (#50 bloc C) + la boucle d'usage."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT run_id, label, doctrine, outcome, project_id, started_at, finished_at "
            "FROM runs WHERE sub = %s AND org_id IS NOT DISTINCT FROM %s "
            "ORDER BY started_at DESC LIMIT %s",
            (sub, org_id, limit),
        ).fetchall()
    return list(rows)


def project_run_tools(project_id: int, limit: int = 200) -> list[str]:
    """Outils réellement APPELÉS par les runs d'un projet — la part « usage observé »
    de l'inventaire dérivé (ADR 0035 B4 : surface d'un projet = refs des procédures
    liées ∪ slots×bindings ∪ runs). Distincts, plus fréquents d'abord ; brut (spine/
    méta inclus — le consommateur cure)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT tc.tool, count(*) AS n FROM tool_calls tc "
            "JOIN runs r ON r.run_id = tc.run_id "
            "WHERE r.project_id = %s AND tc.kind = 'mcp' "
            "GROUP BY tc.tool ORDER BY n DESC, tc.tool LIMIT %s",
            (project_id, limit),
        ).fetchall()
    return [r["tool"] for r in rows]


def project_runs(project_id: int, doctrine: Optional[str] = None,
                 limit: int = 20) -> list[dict]:
    """Derniers runs d'un projet (plus récent d'abord), optionnellement filtrés sur une
    `doctrine` (slug) — alimente la pastille ok/échec du viewer de procédure (refonte UX,
    ADR 0032/0017). `outcome` NULL = run en cours / non clôturé."""
    sql = ("SELECT run_id, label, doctrine, outcome, started_at, finished_at "
           "FROM runs WHERE project_id = %s ")
    params: list = [project_id]
    if doctrine is not None:
        sql += "AND doctrine = %s "
        params.append(doctrine)
    sql += "ORDER BY started_at DESC LIMIT %s"
    params.append(limit)
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]


def project_run_stats(project_id: int) -> dict:
    """Nombre de runs d'un projet + slugs de doctrines déroulées (distincts) — sert
    l'inertie de l'audit de liens (ADR 0035 B5 : procédure liée jamais déroulée)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT count(*) AS n, "
            "array_agg(DISTINCT doctrine) FILTER (WHERE doctrine IS NOT NULL) AS doctrines "
            "FROM runs WHERE project_id = %s",
            (project_id,),
        ).fetchone()
    return {"runs": int(row["n"] or 0), "doctrines": list(row["doctrines"] or [])}


def insert_usage_signal(
    *, sub: Optional[str], org_id: Optional[int], signal: str, kind: str,
    target: Optional[str], body: Optional[str], session_id: Optional[str],
    source: str = "agent",
) -> int:
    with _connect() as conn:
        row = conn.execute(
            """
            INSERT INTO usage_signals
                (sub, org_id, signal, kind, target, body, session_id, source)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (sub, org_id, signal, kind, target, body, session_id, source),
        ).fetchone()
        return int(row["id"])


def list_usage_signals(
    signal: Optional[str] = None, target: Optional[str] = None, limit: int = 200,
    status: Optional[str] = None,
) -> list[dict]:
    """Signaux récents (récent d'abord), filtrables par type / cible / statut —
    base des projections (qualité d'outil, manques) du barreau 4.

    status: 'open' (resolved_at IS NULL) | 'resolved' (NOT NULL) | None (tous)."""
    limit = max(1, min(int(limit), 1000))
    sql = ("SELECT id, created_at, sub, org_id, signal, kind, target, body, "
           "session_id, source, resolved_at, resolved_by, resolution "
           "FROM usage_signals")
    clauses, params = [], []
    if signal:
        clauses.append("signal = %s"); params.append(signal)
    if target:
        clauses.append("target = %s"); params.append(target)
    if status == "open":
        clauses.append("resolved_at IS NULL")
    elif status == "resolved":
        clauses.append("resolved_at IS NOT NULL")
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at DESC LIMIT %s"
    params.append(limit)
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]


def resolve_usage_signal(
    signal_id: int, *, resolved_by: Optional[str], note: Optional[str] = None,
    resolved: bool = True,
) -> Optional[dict]:
    """Marque un signal traité (ou le ré-ouvre si resolved=False). Renvoie la row
    mise à jour, ou None si l'id n'existe pas."""
    with _connect() as conn:
        if resolved:
            row = conn.execute(
                """
                UPDATE usage_signals
                   SET resolved_at = NOW(), resolved_by = %s, resolution = %s
                 WHERE id = %s
                RETURNING id, signal, kind, target, resolved_at, resolved_by, resolution
                """,
                (resolved_by, note, signal_id),
            ).fetchone()
        else:
            row = conn.execute(
                """
                UPDATE usage_signals
                   SET resolved_at = NULL, resolved_by = NULL, resolution = NULL
                 WHERE id = %s
                RETURNING id, signal, kind, target, resolved_at, resolved_by, resolution
                """,
                (signal_id,),
            ).fetchone()
        return dict(row) if row else None


def list_runs(limit: int = 100) -> list[dict]:
    """Runs récents (un par run_id ouvert via run_start) avec label/doctrine,
    acteur, bornes, outcome (si fermé) et nb d'appels du déroulé. `slug` (alias =
    doctrine sinon label) conservé pour compat dashboard."""
    limit = max(1, min(int(limit), 500))
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            """
            SELECT s.run_id,
                   COALESCE(s.args->>'doctrine', s.args->>'label') AS slug,
                   s.args->>'label'    AS label,
                   s.args->>'doctrine' AS doctrine,
                   s.sub,
                   s.created_at      AS started_at,
                   f.created_at      AS finished_at,
                   f.args->>'outcome' AS outcome,
                   COALESCE(c.n_calls, 0) AS n_calls
            FROM tool_calls s
            LEFT JOIN LATERAL (
                SELECT created_at, args FROM tool_calls
                WHERE tool = 'run_finish' AND args->>'run_id' = s.run_id
                ORDER BY created_at DESC LIMIT 1
            ) f ON TRUE
            LEFT JOIN (
                SELECT run_id, count(*) AS n_calls FROM tool_calls
                WHERE run_id IS NOT NULL GROUP BY run_id
            ) c ON c.run_id = s.run_id
            WHERE s.tool = 'run_start' AND s.run_id IS NOT NULL
            ORDER BY s.created_at DESC LIMIT %s
            """,
            (limit,),
        ).fetchall()]


def get_run(run_id: str) -> list[dict]:
    """Timeline d'un déroulé : tous les appels du run, dans l'ordre."""
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            """
            SELECT created_at, tool, args, ok, error, duration_ms
            FROM tool_calls WHERE run_id = %s ORDER BY created_at
            """,
            (run_id,),
        ).fetchall()]


def aggregate_gaps(days: int = 30) -> list[dict]:
    """Manques agrégés (cas d'usage non couverts) — backlog produit dérivé."""
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            """
            SELECT kind, target AS intent, count(*) AS n, max(created_at) AS last_at
            FROM usage_signals
            WHERE signal = 'gap' AND created_at > NOW() - make_interval(days => %s)
            GROUP BY kind, target ORDER BY n DESC, last_at DESC
            """,
            (int(days),),
        ).fetchall()]


def aggregate_tool_feedback(days: int = 30) -> list[dict]:
    """Qualité d'outil agrégée : feedback par (outil, kind)."""
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            """
            SELECT target AS tool, kind, count(*) AS n, max(created_at) AS last_at
            FROM usage_signals
            WHERE signal = 'tool_feedback' AND created_at > NOW() - make_interval(days => %s)
            GROUP BY target, kind ORDER BY n DESC, last_at DESC
            """,
            (int(days),),
        ).fetchall()]


def list_tool_calls(
    limit: int = 200,
    sub: Optional[str] = None,
    tool_name: Optional[str] = None,
    errors_only: bool = False,
    since_days: Optional[int] = None,
    org_id: Optional[int] = None,
) -> list[dict]:
    """Derniers appels MCP (récent d'abord), joints à l'email user pour l'UI.

    `org_id` (si fourni) scope les appels émis SOUS cette org (colonne `tool_calls.org_id`
    stampée par le seam `current_org` au moment de l'appel, ADR 0023) — l'activité « la
    mienne » du dashboard doit refléter l'org chargée, pas l'union de toutes mes orgs."""
    limit = max(1, min(int(limit), 1000))
    clauses: list[str] = ["l.kind = 'mcp'"]
    params: list[Any] = []
    if sub:
        clauses.append("l.sub = %s")
        params.append(sub)
    if org_id is not None:
        clauses.append("l.org_id = %s")
        params.append(int(org_id))
    if tool_name:
        clauses.append("l.tool = %s")
        params.append(tool_name)
    if errors_only:
        clauses.append("l.ok = FALSE")
    if since_days is not None:
        clauses.append("l.created_at >= NOW() - make_interval(days => %s)")
        params.append(int(since_days))
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    with _connect() as conn:
        # Alias tool_name/called_at : compat avec l'UI admin existante.
        rows = conn.execute(
            f"""
            SELECT l.id, l.sub, u.email, u.name, l.tool AS tool_name, l.created_at AS called_at,
                   l.duration_ms, l.ok, l.error
            FROM tool_calls l
            LEFT JOIN users u ON u.sub = l.sub
            {where}
            ORDER BY l.created_at DESC, l.id DESC
            LIMIT %s
            """,
            tuple(params),
        ).fetchall()
        return list(rows)


def list_tool_calls_for_org(
    org_id: int, since: Optional[str] = None, until: Optional[str] = None,
    limit: int = 1000,
) -> list[dict]:
    """Journal d'audit org-scopé (export #67) : appels émis **sous** `org_id`
    (colonne `tool_calls.org_id`, stampée par le seam `current_org` au moment de
    l'appel — scope EXACT, pas l'appartenance), récent d'abord, fenêtre
    `[since, until]` (ISO timestamptz, bornes incluses). JAMAIS d'args ni de secret
    (garantie calllog). ⚠ Les appels antérieurs à la colonne (`org_id` NULL)
    n'apparaissent dans aucun export org — non reconstructibles a posteriori."""
    limit = max(1, min(int(limit), 5000))
    clauses = ["l.kind = 'mcp'", "l.org_id = %s"]
    params: list[Any] = [int(org_id)]
    if since:
        clauses.append("l.created_at >= %s::timestamptz"); params.append(since)
    if until:
        clauses.append("l.created_at <= %s::timestamptz"); params.append(until)
    params.append(limit)
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT l.id, l.created_at, l.sub, u.email, l.tool, l.ok, l.error, l.duration_ms
            FROM tool_calls l
            LEFT JOIN users u ON u.sub = l.sub
            WHERE {" AND ".join(clauses)}
            ORDER BY l.created_at DESC, l.id DESC
            LIMIT %s
            """,
            tuple(params),
        ).fetchall()
        return list(rows)


def instruction_usage(
    subs: list[str], tool: str, slug: Optional[str], days: int = 30
) -> dict:
    """Usage d'une doctrine dérivé de `tool_calls` (ADR 0014, « doctrine = process
    = log d'usage ») : combien de fois elle a été chargée par l'agent, par qui,
    et la distribution journalière sur `days` jours.

    `tool` = `oto_get_doctrine` (slug=None pour la base, sinon filtré par
    `args->>'slug'` pour une skill). Scopé aux `subs` (membres de
    l'org). Lecture pure ; renvoie {count, callers, daily{date:str -> n}}.
    """
    if not subs:
        return {"count": 0, "callers": [], "daily": {}}
    days = max(1, min(int(days), 365))
    slug_clause = " AND l.args->>'slug' = %s" if slug is not None else ""
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


def tool_call_stats(since_days: int = 7) -> dict:
    """Agrégats pour le dashboard de monitoring sur les `since_days` derniers jours :
    total, échecs, ventilation par tool / par user / par jour."""
    since_days = max(1, min(int(since_days), 365))
    with _connect() as conn:
        totals = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE NOT ok) AS errors,
                   COUNT(DISTINCT sub) AS users
            FROM tool_calls
            WHERE kind = 'mcp' AND created_at >= NOW() - make_interval(days => %s)
            """,
            (since_days,),
        ).fetchone() or {}
        by_tool = conn.execute(
            """
            SELECT tool AS tool_name,
                   COUNT(*) AS calls,
                   COUNT(*) FILTER (WHERE NOT ok) AS errors,
                   ROUND(AVG(duration_ms))::int AS avg_ms,
                   ROUND(percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms))::int AS p95_ms
            FROM tool_calls
            WHERE kind = 'mcp' AND created_at >= NOW() - make_interval(days => %s)
            GROUP BY tool
            ORDER BY calls DESC
            LIMIT 100
            """,
            (since_days,),
        ).fetchall()
        by_user = conn.execute(
            """
            SELECT l.sub, u.email, u.name,
                   COUNT(*) AS calls,
                   COUNT(*) FILTER (WHERE NOT l.ok) AS errors
            FROM tool_calls l
            LEFT JOIN users u ON u.sub = l.sub
            WHERE l.kind = 'mcp' AND l.created_at >= NOW() - make_interval(days => %s)
            GROUP BY l.sub, u.email, u.name
            ORDER BY calls DESC
            LIMIT 100
            """,
            (since_days,),
        ).fetchall()
        by_day = conn.execute(
            """
            SELECT to_char(created_at::date, 'YYYY-MM-DD') AS day,
                   COUNT(*) AS calls,
                   COUNT(*) FILTER (WHERE NOT ok) AS errors
            FROM tool_calls
            WHERE kind = 'mcp' AND created_at >= NOW() - make_interval(days => %s)
            GROUP BY created_at::date
            ORDER BY created_at::date
            """,
            (since_days,),
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


def rest_call_stats(since_days: int = 7) -> dict:
    """Lentille REST (ADR 0017, kind='rest') : volume + erreurs + latence des appels
    `/api/*`, par route normalisée. `ok` = 2xx/3xx ; les ≥400 sont comptés erreurs."""
    since_days = max(1, min(int(since_days), 365))
    with _connect() as conn:
        totals = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE NOT ok) AS errors,
                   COUNT(DISTINCT sub) AS users
            FROM tool_calls
            WHERE kind = 'rest' AND created_at >= NOW() - make_interval(days => %s)
            """,
            (since_days,),
        ).fetchone() or {}
        by_route = conn.execute(
            """
            SELECT tool AS route,
                   COUNT(*) AS calls,
                   COUNT(*) FILTER (WHERE NOT ok) AS errors,
                   ROUND(AVG(duration_ms))::int AS avg_ms,
                   ROUND(percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms))::int AS p95_ms
            FROM tool_calls
            WHERE kind = 'rest' AND created_at >= NOW() - make_interval(days => %s)
            GROUP BY tool
            ORDER BY calls DESC
            LIMIT 100
            """,
            (since_days,),
        ).fetchall()
    return {
        "since_days": since_days,
        "total_calls": int((totals or {}).get("total") or 0),
        "error_count": int((totals or {}).get("errors") or 0),
        "active_users": int((totals or {}).get("users") or 0),
        "by_route": list(by_route),
    }


def connector_failure_stats(since_days: int = 7) -> dict:
    """Lentille santé connecteurs (ADR 0017, kind='connector') : échecs de résolution
    de credential par provider — combien, combien d'users distincts touchés, dernier
    échec. C'est le signal « ce connecteur ne résout pas » (compte actif sans clé valide)."""
    since_days = max(1, min(int(since_days), 365))
    with _connect() as conn:
        by_provider = conn.execute(
            """
            SELECT l.tool AS provider,
                   COUNT(*) AS failures,
                   COUNT(DISTINCT l.sub) AS users_affected,
                   MAX(l.created_at) AS last_at
            FROM tool_calls l
            WHERE l.kind = 'connector'
              AND l.created_at >= NOW() - make_interval(days => %s)
            GROUP BY l.tool
            ORDER BY failures DESC
            LIMIT 100
            """,
            (since_days,),
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


def prune_tool_calls(keep_days: int = 30) -> int:
    """Retire les lignes de journal plus vieilles que `keep_days`. Borne la
    volumétrie (appelé au boot dans init_db). Retourne le nombre de lignes
    supprimées."""
    keep_days = max(1, int(keep_days))
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM tool_calls WHERE created_at < NOW() - make_interval(days => %s)",
            (keep_days,),
        )
        return cur.rowcount or 0


def get_usage_today(sub: str, tool: str) -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT count FROM usage WHERE sub = %s AND tool = %s AND day = CURRENT_DATE",
            (sub, tool),
        ).fetchone()
        return int(row["count"]) if row else 0
