"""Power BI — the workspaces, reports and semantic models a person reaches, READ ONLY,
via the Power BI REST API (`PowerBIClient`).

Credential = the PERSON's Microsoft 365 account (carrier `microsoft`, OAuth, delegated
permissions) that has authorized THIS service (scopes `POWERBI`: workspaces, reports,
models, all read). Power BI is its own Entra resource: `auth/microsoft` asks its token
apart from Graph's (`SERVICE_RESOURCE`), from the same refresh token. Several linked
accounts are chosen by the generic `_account=` axis.

**Surface** — one tool, `powerbi`, `lecture.LECTURE`: nothing here writes, and nothing
is stored — a query's rows go back to the caller and nowhere else.

Protocol facts the tool carries (`oto.tools.microsoft.powerbi`): a DAX query needs Read
AND Build on the model and the tenant setting « Semantic model Execute Queries REST API »;
a measure's DAX expression is only returned to a person with WRITE permission on the
model — otherwise the definition comes without its formula, and the answer says so.
"""
from __future__ import annotations

from typing import Literal, Optional

from fastmcp import FastMCP

from .. import output_projection
from ._microsoft_graph import bad, need, raw, refuse_ignored, run, token
from .lecture import LECTURE

#: The Microsoft service these tools call: its scopes, its card (`auth/microsoft`).
_SERVICE = "powerbi"
_LABEL = "Power BI"

_OPS = ("workspaces", "reports", "report_pages", "datasets", "query", "measures")
#: Rows of a DAX query served inline, at most (Power BI itself stops at 100,000).
_ROWS_MAX = 2000
_SANS_FORMULE = (
    "Power BI returned the measures WITHOUT their DAX expression: it only gives it to a "
    "person with write permission on the semantic model (Contributor, Member or Admin "
    "of its workspace). Names, tables, folders and descriptions are complete.")


def _workspace(w: dict) -> dict:
    return {"id": w.get("id"), "name": w.get("name"), "type": w.get("type"),
            "isReadOnly": w.get("isReadOnly")}


def _report(r: dict) -> dict:
    return {"id": r.get("id"), "name": r.get("name"), "datasetId": r.get("datasetId"),
            "reportType": r.get("reportType"), "webUrl": r.get("webUrl")}


def _dataset(d: dict) -> dict:
    return {"id": d.get("id"), "name": d.get("name"), "configuredBy": d.get("configuredBy"),
            "isRefreshable": d.get("isRefreshable"),
            "targetStorageMode": d.get("targetStorageMode"), "webUrl": d.get("webUrl")}


def register(mcp: FastMCP) -> None:
    from oto.tools.microsoft import PowerBIClient

    def _client() -> PowerBIClient:
        """The Power BI client of THIS caller, with their current Power BI token."""
        return PowerBIClient(token(_SERVICE))

    def _run(fn):
        return run(fn, _LABEL)

    @mcp.tool(name="powerbi", annotations=LECTURE)
    def powerbi(
        op: Literal["workspaces", "reports", "report_pages", "datasets", "query",
                    "measures"] = "workspaces",
        workspace_id: Optional[str] = None,
        report_id: Optional[str] = None,
        dataset_id: Optional[str] = None,
        dax: Optional[str] = None,
        max_rows: int = 500,
        limit: int = 100,
        full: bool = False,
        fields: Optional[list[str]] = None,
    ) -> dict:
        """READ Power BI with the person's rights: workspaces, reports, semantic models,
        a DAX query, the definition of a model's measures. Nothing writes, nothing is
        stored. `workspace_id` omitted = the person's "My workspace".

        `op`:
        - **"workspaces"** (default): the shared workspaces — {workspaces: [{id, name,
          type, isReadOnly}], count}.
        - **"reports"**: the reports of the workspace — {reports: [{id, name, datasetId,
          reportType, webUrl}], count}; `datasetId` is the model behind a report.
        - **"report_pages"**: the pages of `report_id` — {pages: [{name, displayName,
          order}], count}.
        - **"datasets"**: the semantic models of the workspace — {datasets: [{id, name,
          configuredBy, isRefreshable, targetStorageMode, webUrl}], count}.
        - **"query"**: ONE DAX query (`dax`, starting with EVALUATE) on `dataset_id` —
          {columns, rows, row_count, truncated, error}. Needs Read AND Build on the model,
          and the tenant setting « Semantic model Execute Queries REST API ». At most
          `max_rows` rows come back (default 500, ≤ 2000): aggregate in DAX
          (SUMMARIZECOLUMNS, TOPN) rather than reading raw tables.
        - **"measures"**: the measures of `dataset_id` and their definition — {measures:
          [{Name, Table, Description, Expression, DisplayFolder, FormatString, DataType,
          IsHidden, State}], count, expression_visible}. ⚠️ Power BI gives the DAX
          `Expression` only to a person with WRITE permission on the model:
          `expression_visible=false` says the formulas are missing (`note`).

        Args:
            op: workspaces (default) | reports | report_pages | datasets | query | measures.
            workspace_id: the workspace (`id` from op="workspaces"); omitted = My workspace.
            report_id: op="report_pages".
            dataset_id: op="query"/"measures" — the model (`datasetId` of a report).
            dax: op="query" — one DAX query, e.g. `EVALUATE SUMMARIZECOLUMNS(...)`.
            max_rows: op="query" — rows returned, 1..2000 (default 500).
            limit: op="workspaces" — max workspaces (default 100).
            full: return Power BI's raw objects instead of the trimmed view (listings).
            fields: listing ops — keep only these keys of each item.
        """
        if op not in _OPS:
            raise bad("op must be " + ", ".join(f"'{o}'" for o in _OPS[:-1])
                      + f" or '{_OPS[-1]}'")
        if op not in ("query",) and max_rows != 500:
            raise bad(f"`max_rows` only applies to op='query' (got op='{op}').")
        if op in ("query", "measures") and (full or fields is not None):
            raise bad(f"`full` and `fields` only apply to the listing ops (got op='{op}').")

        def _liste(cle: str, items: list, vue) -> dict:
            out = {cle: [raw(i) if full else vue(i) for i in items], "count": len(items)}
            return output_projection.project(out, items_path=cle, fields=fields)

        if op == "workspaces":
            refuse_ignored(op, workspace_id=workspace_id, report_id=report_id,
                           dataset_id=dataset_id, dax=dax)
            c = _client()
            return _liste("workspaces", _run(lambda: c.list_workspaces(limit=limit)),
                          _workspace)
        if op == "reports":
            refuse_ignored(op, report_id=report_id, dataset_id=dataset_id, dax=dax)
            c = _client()
            return _liste("reports", _run(lambda: c.list_reports(workspace_id)), _report)
        if op == "report_pages":
            refuse_ignored(op, dataset_id=dataset_id, dax=dax)
            rid = need(report_id, "report_id", op)
            c = _client()
            return _liste("pages", _run(lambda: c.list_report_pages(rid, workspace_id)),
                          lambda p: {"name": p.get("name"),
                                     "displayName": p.get("displayName"),
                                     "order": p.get("order")})
        if op == "datasets":
            refuse_ignored(op, report_id=report_id, dataset_id=dataset_id, dax=dax)
            c = _client()
            return _liste("datasets", _run(lambda: c.list_datasets(workspace_id)), _dataset)
        refuse_ignored(op, report_id=report_id)
        did = need(dataset_id, "dataset_id", op)
        if op == "measures":
            refuse_ignored(op, dax=dax)
            c = _client()
            res = _run(lambda: c.list_measures(did, workspace_id=workspace_id))
            out = {"measures": res["measures"], "count": len(res["measures"]),
                   "expression_visible": res["expression_visible"]}
            if res["measures"] and not res["expression_visible"]:
                out["note"] = _SANS_FORMULE
            return out
        requete = need(dax, "dax", op)
        if not 1 <= max_rows <= _ROWS_MAX:
            raise bad(f"`max_rows` must be between 1 and {_ROWS_MAX}.")
        c = _client()
        res = _run(lambda: c.execute_query(did, requete, workspace_id=workspace_id))
        rows = res["rows"]
        return {"columns": res["columns"], "rows": rows[:max_rows], "row_count": len(rows),
                "truncated": res["truncated"] or len(rows) > max_rows,
                "error": res["error"]}
