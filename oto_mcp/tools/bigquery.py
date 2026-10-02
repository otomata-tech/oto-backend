"""Google BigQuery — surface oto-core (BigQueryClient) exposée par-utilisateur, multi-compte.

Septième service du compte Google (2026-10-02) : scope `bigquery`, accordé depuis sa
carte. Les requêtes tournent sous l'identité de la personne — ses droits IAM, pas
ceux d'une clé partagée — et sont facturées au projet qu'elle désigne (`project`).

**Quatre tools, lecture seule** :
- `bigquery_catalog` — projets → datasets → tables, un niveau par appel ;
- `bigquery_table` — schéma d'une table (+ aperçu GRATUIT par `tabledata.list`) ;
- `bigquery_query` — SQL standard, SELECT seulement ;
- `bigquery_results` — reprendre une requête pas finie à temps, ou la page suivante.

**Le scope permet d'écrire** (`bigquery.readonly` ne permet pas de requêter, cf.
`auth/google.SERVICE_SCOPES`) : la lecture seule est donc tenue ICI, pas par Google.
Chaque requête passe d'abord par un dry run (`jobs.insert`), dont le `statementType`
doit être `SELECT` — DML, DDL, scripts et procédures sont refusés avant tout
lancement. Jamais déduit du texte SQL : un `WITH … DELETE` ou un commentaire en tête
tromperait une lecture de chaîne.

**Le coût est borné à chaque requête** : le dry run estime les octets lus ; au-delà
du plafond (`max_gb_billed`, 10 Go par défaut, 1 To au plus), refus AVANT de lancer,
estimation à l'appui. La requête part ensuite avec `maximumBytesBilled` = ce plafond
— la garde est aussi chez Google, pas seulement chez nous.

**Le temps est borné** : l'invocation REST d'un tool coupe à 45 s. La requête attend
au plus 20 s ; pas finie → `status="running"` + `job_id`, à reprendre par
`bigquery_results`. Les lignes rendues sont plafonnées (`max_rows` ≤ 1 000) ; la
suite se lit par `page_token`, et le mieux reste d'agréger en SQL.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

from fastmcp import FastMCP
from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access
from ..auth import google as google_oauth

_GB = 1024 ** 3
_DEFAULT_MAX_GB = 10.0
# Plafond DUR d'une requête (≈ 6 $ au tarif à la demande) : l'argument ne le dépasse pas.
_HARD_MAX_GB = 1024.0
_DEFAULT_ROWS = 100
_MAX_ROWS = 1000
_MAX_PREVIEW_ROWS = 100
# Attente côté BigQuery, sous les 45 s de l'invocation REST (dry run + jeton compris).
_QUERY_WAIT_MS = 20_000
_RESULTS_WAIT_MS = 25_000
_LABELS = {"source": "oto"}
_SELECT = "SELECT"


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _http_error(e, project: Optional[str] = None) -> McpError:
    """`HttpError` → conduite à tenir, lue sur la RAISON BigQuery (jamais le texte)."""
    from oto.tools.google.bigquery.lib.bigquery_client import parse_http_error
    err = parse_http_error(e)
    status, reason, detail = err["status"], err["reason"], err["message"].strip()
    where = f" dans le projet {project}" if project else ""
    if reason == "invalidQuery":
        msg = (f"BigQuery a refusé la requête : {detail} — vérifie les noms de colonnes "
               "avec `bigquery_table` (SQL standard, tables en `projet.dataset.table`).")
    elif reason == "bytesBilledLimitExceeded":
        msg = (f"BigQuery a arrêté la requête au plafond d'octets facturés : {detail} — "
               "filtre davantage (colonne de partition, moins de colonnes) ou relève "
               "`max_gb_billed`.")
    elif reason == "accessNotConfigured" or "has not been used in project" in detail:
        msg = ("L'API BigQuery n'est pas activée dans le projet Google Cloud du client "
               "OAuth qui a émis la connexion — configuration de ce client, à faire par "
               "son administrateur (console Google Cloud → API et services → BigQuery "
               f"API) ; reconnecter le compte n'y change rien. Détail Google : {detail}")
    elif reason == "accessDenied" or status == 403:
        if "jobs.create" in detail:
            msg = (f"Ton compte Google ne peut pas lancer de requête{where} (permission "
                   "`bigquery.jobs.create`, rôle « BigQuery Job User ») — choisis un autre "
                   "projet de facturation (`bigquery_catalog` liste les tiens) ou demande "
                   f"ce rôle à un administrateur. Détail Google : {detail}")
        else:
            msg = (f"Ton compte Google n'a pas accès à cette ressource BigQuery : {detail} "
                   "— il faut le rôle « BigQuery Data Viewer » sur le dataset.")
    elif reason == "notFound" or status == 404:
        msg = (f"BigQuery ne trouve pas la ressource : {detail} — vérifie le nom, et "
               "`location` pour un dataset hors des multi-régions US/EU.")
    elif reason in ("quotaExceeded", "rateLimitExceeded") or status == 429:
        msg = f"BigQuery : quota ou débit atteint — réessaie plus tard. Détail : {detail}"
    elif status and status >= 500:
        msg = f"BigQuery est momentanément indisponible (HTTP {status}) — réessaie plus tard."
    else:
        msg = f"BigQuery a refusé la requête (HTTP {status}, {reason or '?'}) : {detail}"
    return _bad(msg)


async def _call(fn, *args, _project: Optional[str] = None, **kwargs):
    """Appel client hors boucle ; tout `HttpError` devient une erreur nommée.

    `_project` ne sert QU'AU message du refus (il n'est pas transmis à `fn`)."""
    from googleapiclient.errors import HttpError
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except HttpError as e:
        raise _http_error(e, _project)


def _client_for_user(account: Optional[str] = None):
    sub = access.current_user_sub_or_raise()
    try:
        creds = google_oauth.credentials_for(sub, account=account, service="bigquery")
    except RuntimeError as e:
        raise _bad(str(e))
    from oto.tools.google.bigquery.lib.bigquery_client import BigQueryClient
    return BigQueryClient(credentials=creds)


_GOOGLE_CLIENT_TIMEOUT_S = 20
# oto-backend#867 lot 2 — voir gmail.py::_client_for_user_async pour la
# justification (même mécanisme de rafraîchissement de jeton, même méthode).
async def _client_for_user_async(account: Optional[str] = None):
    try:
        return await asyncio.wait_for(asyncio.to_thread(_client_for_user, account),
                                      timeout=_GOOGLE_CLIENT_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise _bad(f"Google n'a pas répondu dans les {_GOOGLE_CLIENT_TIMEOUT_S}s "
                   "(rafraîchissement de jeton) — réessaie.")


async def _billing_project(client, project: Optional[str]) -> str:
    """Le projet qui exécute (et paie) la requête : explicite, sinon le SEUL projet
    visible. Plusieurs → refus qui les nomme, jamais un choix au hasard (c'est une
    facture)."""
    if project:
        return project
    listed = await _call(client.list_projects, 50)
    ids = [p["id"] for p in listed["projects"] if p.get("id")]
    if len(ids) == 1:
        return ids[0]
    if not ids:
        raise _bad("Ton compte Google ne voit aucun projet BigQuery : il en faut un pour "
                   "exécuter (et facturer) les requêtes.")
    shown = ", ".join(ids[:15]) + (" …" if len(ids) > 15 else "")
    raise _bad(f"Précise `project`, le projet qui exécute et paie la requête : {shown}.")


def _cap_gb(max_gb_billed: Optional[float]) -> float:
    if max_gb_billed is None:
        return _DEFAULT_MAX_GB
    if max_gb_billed <= 0:
        raise _bad("max_gb_billed doit être > 0.")
    if max_gb_billed > _HARD_MAX_GB:
        raise _bad(f"max_gb_billed est plafonné à {_HARD_MAX_GB:g} Go par requête.")
    return float(max_gb_billed)


def _gb(n: Any) -> Optional[float]:
    if n in (None, ""):
        return None
    return round(int(n) / _GB, 3)


def _rows_cap(max_rows: int, hard: int = _MAX_ROWS) -> int:
    if max_rows < 1:
        raise _bad("max_rows doit être ≥ 1.")
    return min(max_rows, hard)


def _table_view(schema: Optional[dict], raw_rows: Optional[list]) -> dict:
    """Vue compacte : `columns` (nom, type) + `rows` en listes, dans l'ordre du schéma."""
    from oto.tools.google.bigquery.lib.bigquery_client import rows_to_records
    fields = (schema or {}).get("fields") or []
    names = [f["name"] for f in fields]
    records = rows_to_records(schema, raw_rows)
    return {
        "columns": [{"name": f["name"], "type": f.get("type"),
                     **({"mode": f["mode"]} if f.get("mode") == "REPEATED" else {})}
                    for f in fields],
        "rows": [[r.get(n) for n in names] for r in records],
    }


def _result(resp: dict, project: str) -> dict:
    """Réponse `jobs.query` / `getQueryResults` → résultat rendu à l'agent."""
    ref = resp.get("jobReference") or {}
    job = {"job_id": ref.get("jobId"), "location": ref.get("location") or resp.get("location"),
           "project": ref.get("projectId") or project}
    if not resp.get("jobComplete"):
        return {"status": "running", **job,
                "hint": "requête encore en cours — reprends avec `bigquery_results(job_id, "
                        "project, location)`."}
    out = {"status": "done", **_table_view(resp.get("schema"), resp.get("rows"))}
    out["row_count"] = len(out["rows"])
    out["total_rows"] = int(resp["totalRows"]) if resp.get("totalRows") is not None else None
    for key, src in (("gb_processed", "totalBytesProcessed"), ("gb_billed", "totalBytesBilled")):
        if resp.get(src) is not None:
            out[key] = _gb(resp[src])
    if "cacheHit" in resp:
        out["cache_hit"] = bool(resp["cacheHit"])
    out.update(job)
    if resp.get("pageToken"):
        out["page_token"] = resp["pageToken"]
        out["hint"] = ("lignes tronquées — page suivante par `bigquery_results(job_id, "
                       "project, location, page_token)`, ou mieux : agrège en SQL.")
    return out


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def bigquery_catalog(
        project: Optional[str] = None,
        dataset: Optional[str] = None,
        page_token: Optional[str] = None,
        account: Optional[str] = None,
    ) -> dict:
        """Browse BigQuery one level at a time: projects → datasets → tables.

        - no `project`: the projects your Google account can see (pick one as the
          billing `project` of `bigquery_query`);
        - `project`: its datasets (with their location);
        - `project` + `dataset`: its tables and views (type, partition column).

        Data often lives in another project than the one you bill to (e.g.
        `bigquery-public-data`): browse it by name, query it from your own project.

        Args:
            project: project id (e.g. "my-company-dwh").
            dataset: dataset id inside `project`.
            page_token: `next_page_token` of a previous call.
            account: email of the Google account to use (default if omitted).
        """
        if dataset and not project:
            raise _bad("`dataset` demande `project`.")
        client = await _client_for_user_async(account)
        if not project:
            return {"level": "projects", **await _call(client.list_projects, 100, page_token)}
        if not dataset:
            return {"level": "datasets", "project": project,
                    **await _call(client.list_datasets, project, 200, page_token,
                                  _project=project)}
        return {"level": "tables", "project": project, "dataset": dataset,
                **await _call(client.list_tables, project, dataset, 200, page_token,
                              _project=project)}

    @mcp.tool()
    async def bigquery_table(
        table: str,
        preview_rows: int = 0,
        project: Optional[str] = None,
        account: Optional[str] = None,
    ) -> dict:
        """A BigQuery table's schema, size and partitioning — plus an optional preview.

        The preview reads stored rows directly (`tabledata.list`): free, no query job,
        nothing billed. It doesn't work on views — query those instead.

        Returns {table, type, columns: [{name, type, mode, description?}] (nested
        fields as `a.b`), num_rows, gb, partitioning, clustering, view_sql?, preview?}.

        Args:
            table: `project.dataset.table` (or `dataset.table` with `project`).
            preview_rows: rows to preview (0 = none, max 100).
            project: default project when `table` is `dataset.table`.
            account: email of the Google account to use (default if omitted).
        """
        from oto.tools.google.bigquery.lib.bigquery_client import (
            flatten_schema, split_table_ref)
        try:
            p, d, t = split_table_ref(table, project)
        except ValueError as e:
            raise _bad(str(e))
        if not 0 <= preview_rows <= _MAX_PREVIEW_ROWS:
            raise _bad(f"preview_rows doit être entre 0 et {_MAX_PREVIEW_ROWS}.")
        client = await _client_for_user_async(account)
        meta = await _call(client.get_table, p, d, t, _project=p)
        part = meta.get("timePartitioning") or meta.get("rangePartitioning")
        out: dict = {
            "table": f"{p}.{d}.{t}", "type": meta.get("type"),
            "location": meta.get("location"),
            "description": meta.get("description"),
            "columns": flatten_schema(meta.get("schema")),
            "num_rows": int(meta["numRows"]) if meta.get("numRows") is not None else None,
            "gb": _gb(meta.get("numBytes")),
            "partitioning": ({"type": part.get("type"), "field": part.get("field")
                              or ("_PARTITIONTIME" if part.get("type") else None),
                              "require_filter": meta.get("requirePartitionFilter")}
                             if part else None),
            "clustering": (meta.get("clustering") or {}).get("fields"),
            "view_sql": (meta.get("view") or {}).get("query"),
        }
        if preview_rows and meta.get("type") == "TABLE":
            page = await _call(client.list_rows, p, d, t, preview_rows, _project=p)
            out["preview"] = _table_view(meta.get("schema"), page.get("rows"))["rows"]
        elif preview_rows:
            out["preview_note"] = (f"aperçu impossible sur une {(meta.get('type') or 'ressource').lower()} "
                                   "(seule une table stockée se lit sans requête) — passe par `bigquery_query`.")
        return {k: v for k, v in out.items() if v is not None}

    @mcp.tool()
    async def bigquery_query(
        sql: str,
        project: Optional[str] = None,
        params: Optional[dict] = None,
        max_rows: int = _DEFAULT_ROWS,
        max_gb_billed: Optional[float] = None,
        dry_run: bool = False,
        location: Optional[str] = None,
        account: Optional[str] = None,
    ) -> dict:
        """Run a read-only SQL query (GoogleSQL / standard SQL) on BigQuery.

        SELECT only: every query is dry-run first and anything else (INSERT, UPDATE,
        DELETE, MERGE, CREATE, scripts, CALL) is refused before it runs. Name tables
        fully: `project.dataset.table`.

        Cost guard: the dry run estimates the bytes read; above `max_gb_billed`
        (default 10 GB, max 1024) the query is refused before running, and BigQuery
        also enforces the cap (`maximumBytesBilled`). On-demand pricing bills bytes
        SCANNED, not rows returned — `LIMIT` doesn't reduce it; selecting fewer
        columns and filtering on the partition column does.

        `dry_run=True` only validates and estimates: {statement_type, gb_processed,
        within_cap, columns, referenced_tables}.

        Returns {status: "done", columns, rows (lists, in column order), row_count,
        total_rows, gb_processed, gb_billed, cache_hit, job_id, location, project,
        page_token?} — or {status: "running", job_id, …} if it takes longer than
        ~20 s: resume with `bigquery_results`. Aggregate in SQL rather than paging
        through raw rows.

        Args:
            sql: the query. Use `@name` placeholders with `params` instead of
                pasting values into the SQL.
            project: billing project that runs the query (your own, even to read
                public or other-project data). Omitted: your only visible project.
            params: named parameters, e.g. {"since": "2026-01-01", "ids": [1, 2]}
                (bool / int / float / string, or a list of one type; CAST in SQL
                for DATE/TIMESTAMP).
            max_rows: rows returned in this call (default 100, max 1000).
            max_gb_billed: cost cap in GB for this query (default 10).
            dry_run: validate and estimate only, run nothing.
            location: job location for datasets outside the US/EU multi-regions
                (e.g. "europe-west1").
            account: email of the Google account to use (default if omitted).
        """
        if not sql or not sql.strip():
            raise _bad("sql est vide.")
        cap = _cap_gb(max_gb_billed)
        rows = _rows_cap(max_rows)
        from oto.tools.google.bigquery.lib.bigquery_client import query_parameters
        try:
            query_parameters(params)
        except ValueError as e:
            raise _bad(str(e))
        client = await _client_for_user_async(account)
        billing = await _billing_project(client, project)

        plan = await _call(client.dry_run, sql, billing, location=location, params=params,
                           _project=billing)
        stype = plan.get("statement_type")
        if stype != _SELECT:
            raise _bad(f"Lecture seule : seules les requêtes SELECT sont acceptées (celle-ci "
                       f"est {stype or 'de type inconnu'}). Rien n'a été exécuté.")
        estimate_gb = round(plan["bytes_processed"] / _GB, 3)
        within = plan["bytes_processed"] <= cap * _GB
        if dry_run:
            return {"dry_run": True, "statement_type": stype, "gb_processed": estimate_gb,
                    "max_gb_billed": cap, "within_cap": within,
                    "columns": [{"name": f["name"], "type": f.get("type")}
                                for f in (plan.get("schema") or {}).get("fields") or []],
                    "referenced_tables": plan["referenced_tables"], "project": billing}
        if not within:
            raise _bad(f"Requête refusée avant exécution : elle lirait ~{estimate_gb:g} Go, "
                       f"au-delà du plafond de {cap:g} Go. Réduis les colonnes, filtre sur la "
                       "colonne de partition, ou relève `max_gb_billed` si c'est voulu.")

        resp = await _call(client.query, sql, billing, location=location, params=params,
                           max_results=rows, timeout_ms=_QUERY_WAIT_MS,
                           maximum_bytes_billed=int(cap * _GB), labels=_LABELS,
                           _project=billing)
        return _result(resp, billing)

    @mcp.tool()
    async def bigquery_results(
        job_id: str,
        project: str,
        location: Optional[str] = None,
        page_token: Optional[str] = None,
        max_rows: int = _DEFAULT_ROWS,
        account: Optional[str] = None,
    ) -> dict:
        """Results of a BigQuery query job: resume one still running, or read the
        next page (`page_token`). Same shape as `bigquery_query`.

        Args:
            job_id: `job_id` returned by `bigquery_query`.
            project: `project` returned by `bigquery_query`.
            location: `location` returned by `bigquery_query` (required outside US/EU).
            page_token: `page_token` of the previous page.
            max_rows: rows in this page (default 100, max 1000).
            account: email of the Google account to use (default if omitted).
        """
        rows = _rows_cap(max_rows)
        client = await _client_for_user_async(account)
        resp = await _call(client.get_query_results, project, job_id, location=location,
                           page_token=page_token, max_results=rows,
                           timeout_ms=_RESULTS_WAIT_MS, _project=project)
        return _result(resp, project)
