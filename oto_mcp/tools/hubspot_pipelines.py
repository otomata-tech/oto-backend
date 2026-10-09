"""HubSpot — pipelines and their stages, read-only; and what they let the rest read.

Third module of the connector (`hubspot` holds objects, lists and properties,
`hubspot_lignes` the push by reference). It carries `hubspot_pipeline`, and the two
helpers the other tools borrow from it.

**Why a pipeline tool at all.** A deal's stage does not belong to the `dealstage`
property: it belongs to a PIPELINE. So `hubspot_property` served `dealstage` with an
EMPTY `options` list, and `hubspot_object` served deals carrying `closedlost` or a
numeric custom id with no label — an agent could neither write a stage nor say
which one a deal sat in. Same for tickets (`hs_pipeline_stage`). Pipelines are read
with ONE call per object type (`GET /crm/v3/pipelines/{deals|tickets}`), never one
per record.

**Labels are SIBLINGS, never replacements.** `resolve_labels` on `hubspot_object`
adds `dealstage_label` next to `dealstage`; it never rewrites the raw id, because a
write (`op=update`) still needs the id, and an agent that read a label back where an
id was would write the label — which HubSpot refuses, or worse.

**A missing pipelines scope never fails the call it decorates.** Labelling is a
bonus on a read that already worked: the objects come back unlabelled, with a
`warnings` entry that names the scope. Only `hubspot_pipeline` itself refuses —
there, the pipelines ARE the answer.

The HubSpot client (oto-core) has no pipelines method; we call its `_request`, as
the connection probe already does, rather than tag oto-core for two GETs.
"""
from __future__ import annotations

from typing import Literal, Optional

from fastmcp import FastMCP

from .. import access
from .lecture import LECTURE

#: Object types that have pipelines, and the properties that point INTO them —
#: (stage property, pipeline property).
PIPELINE_PROPS = {
    "deals": ("dealstage", "pipeline"),
    "tickets": ("hs_pipeline_stage", "hs_pipeline"),
}

#: Per object type, the stage `metadata` keys worth serving in the compact form.
_STAGE_META = {"deals": ("probability", "isClosed"), "tickets": ("ticketState",)}


def _pipeline_type(object_type) -> Optional[str]:
    key = str(object_type or "").strip().lower()
    return key if key in PIPELINE_PROPS else None


def compact_pipeline(p: dict, object_type: str) -> dict:
    """One pipeline, reduced to what an agent reads: ids, labels, order, state.

    Stages keep their HubSpot order (`displayOrder`), archived ones included and
    FLAGGED — a deal can still sit in an archived stage, and dropping it would
    turn its id into an unknown one."""
    keys = _STAGE_META.get(object_type, ())
    stages = []
    for s in sorted(p.get("stages") or [], key=lambda s: s.get("displayOrder", 0)):
        meta = s.get("metadata") or {}
        stages.append({"id": s.get("id"), "label": s.get("label"),
                       "displayOrder": s.get("displayOrder"),
                       "archived": bool(s.get("archived", False)),
                       "metadata": {k: meta[k] for k in keys if k in meta}})
    return {"id": p.get("id"), "label": p.get("label"),
            "displayOrder": p.get("displayOrder"),
            "archived": bool(p.get("archived", False)), "stages": stages}


def fetch_pipelines(client, object_type: str) -> list[dict]:
    """The pipelines of one object type, raw — ONE call."""
    rep = client._request("GET", f"/crm/v3/pipelines/{object_type}") or {}
    return list(rep.get("results") or [])


class Labels:
    """Id → label tables for one object type, fetched AT MOST once per call.

    `error` holds the refusal that stopped the fetch (a missing scope, typically):
    the caller turns it into a warning and serves its objects unlabelled."""

    def __init__(self, client, object_type: str):
        from .hubspot_scopes import is_missing_scopes, scope_hint
        self.pipelines: dict[str, str] = {}
        self.stages: dict[str, str] = {}
        self.warning: Optional[str] = None
        try:
            raw = fetch_pipelines(client, object_type)
        except Exception as e:  # noqa: BLE001 — degraded to a NAMED warning below
            if not is_missing_scopes(e):
                raise
            self.warning = (
                f"stage labels not resolved: the token lacks the scope to read "
                f"{object_type} pipelines ({scope_hint(e, 'pipelines_' + object_type)}). "
                "objects are served with raw ids only.")
            return
        for p in raw:
            self.pipelines[str(p.get("id"))] = p.get("label")
            for s in p.get("stages") or []:
                self.stages[str(s.get("id"))] = s.get("label")


def add_labels(result, object_type, client) -> None:
    """Add `<prop>_label` siblings to every record of `result`, IN PLACE.

    `result` is what the client returned: a page (`{"results": [...]}`) or one
    record (`{"id", "properties"}`). An object type without pipelines is left
    untouched — the flag then has nothing to resolve, and says nothing."""
    otype = _pipeline_type(object_type)
    if otype is None or not isinstance(result, dict):
        return
    records = result.get("results") if "results" in result else [result]
    if not isinstance(records, list):
        return
    labels = Labels(client, otype)
    if labels.warning:
        result.setdefault("warnings", []).append(labels.warning)
        return
    stage_prop, pipeline_prop = PIPELINE_PROPS[otype]
    for rec in records:
        props = rec.get("properties") if isinstance(rec, dict) else None
        if not isinstance(props, dict):
            continue
        if props.get(stage_prop) is not None:
            props[f"{stage_prop}_label"] = labels.stages.get(str(props[stage_prop]))
        if props.get(pipeline_prop) is not None:
            props[f"{pipeline_prop}_label"] = labels.pipelines.get(
                str(props[pipeline_prop]))


# --- properties ------------------------------------------------------------------

def compact_property(p: dict) -> dict:
    """The default row of `hubspot_property op=list`: enough to write a value.

    The full HubSpot card (descriptions, modification metadata, display hints…)
    stays one flag away (`verbose=true`). Measured on a customer portal: about 275k
    characters for `deals`, mostly hidden internal properties — a page the MCP
    client truncated before the agent could read it."""
    row = {k: p.get(k) for k in ("name", "label", "type", "fieldType", "groupName")}
    row["options"] = [{"value": o.get("value"), "label": o.get("label")}
                      for o in p.get("options") or []]
    for k in ("pipeline_options", "warnings"):
        if k in p:
            row[k] = p[k]
    return row


def filter_properties(results: list, *, names=None, group=None,
                      include_hidden=False, custom_only=False) -> tuple[list, dict]:
    """Apply the `op=list` filters, and COUNT what each one dropped — a list that
    shrinks says by how much and why (`docs/conventions.md`, a projection names what
    it set aside)."""
    dropped = {"hidden": 0, "hubspot_defined": 0, "names": 0, "group": 0}
    wanted = set(names) if names else None
    out = []
    for p in results:
        if wanted is not None and p.get("name") not in wanted:
            dropped["names"] += 1
        elif group is not None and p.get("groupName") != group:
            dropped["group"] += 1
        elif not include_hidden and p.get("hidden"):
            dropped["hidden"] += 1
        elif custom_only and p.get("hubspotDefined"):
            dropped["hubspot_defined"] += 1
        else:
            out.append(p)
    return out, {k: v for k, v in dropped.items() if v}


def fill_pipeline_options(props: list, object_type, client) -> None:
    """Fill the stage and pipeline properties' EMPTY `options` from the pipelines,
    IN PLACE — one pipelines read, only if one of those properties is present.

    Options keep HubSpot's `{value, label}` shape (value = the id to WRITE), each
    tagged with its pipeline; `pipeline_options` groups them by pipeline, since the
    same label ("Closed won") repeats across pipelines with different ids."""
    otype = _pipeline_type(object_type)
    if otype is None:
        return
    stage_prop, pipeline_prop = PIPELINE_PROPS[otype]
    targets = [p for p in props if p.get("name") in (stage_prop, pipeline_prop)]
    if not targets:
        return
    labels_err = None
    try:
        raw = fetch_pipelines(client, otype)
    except Exception as e:  # noqa: BLE001 — degraded to a NAMED warning below
        from .hubspot_scopes import is_missing_scopes, scope_hint
        if not is_missing_scopes(e):
            raise
        labels_err = (f"options not filled: the token lacks the scope to read {otype} "
                      f"pipelines ({scope_hint(e, 'pipelines_' + otype)})")
        raw = []
    for p in targets:
        if labels_err:
            p["warnings"] = [labels_err]
            continue
        if p["name"] == pipeline_prop:
            if not p.get("options"):
                p["options"] = [{"value": x.get("id"), "label": x.get("label"),
                                 "displayOrder": x.get("displayOrder")} for x in raw]
            continue
        grouped, flat = [], []
        for pl in raw:
            stages = compact_pipeline(pl, otype)["stages"]
            grouped.append({"pipeline_id": pl.get("id"), "pipeline_label": pl.get("label"),
                            "options": [{"value": s["id"], "label": s["label"],
                                         "archived": s["archived"]} for s in stages]})
            flat += [{"value": s["id"], "label": s["label"], "pipeline_id": pl.get("id"),
                      "displayOrder": s["displayOrder"]} for s in stages]
        if not p.get("options"):
            p["options"] = flat
        p["pipeline_options"] = grouped


# --- the tool ---------------------------------------------------------------------

def register(mcp: FastMCP) -> None:
    from oto.tools.common.errors import UpstreamHTTPError
    from oto.tools.hubspot.client import HubSpotClient

    from ..mcp_errors import McpError
    from mcp.types import ErrorData, INVALID_PARAMS
    from .hubspot_scopes import translate

    def _bad(msg: str) -> McpError:
        return McpError(ErrorData(code=INVALID_PARAMS, message=msg))

    @mcp.tool(annotations=LECTURE)
    def hubspot_pipeline(
        op: Literal["list", "get"] = "list",
        object_type: Literal["deals", "tickets"] = "deals",
        pipeline_id: Optional[str] = None,
        verbose: bool = False,
    ) -> dict:
        """HubSpot pipelines and their stages (read-only) — what `dealstage` and
        `hs_pipeline_stage` ids MEAN, and which ids to write.

        A deal's stage belongs to its pipeline, not to the `dealstage` property:
        read the stage ids and labels here. Archived stages are kept and flagged.

        Ops:
        - **"list"** (default) : every pipeline of `object_type`, with its stages.
        - **"get"** : one pipeline. Requires `pipeline_id`.

        Args:
            op: list | get.
            object_type: deals | tickets.
            pipeline_id: op="get" — the pipeline id (e.g. "default").
            verbose: return HubSpot's raw payload instead of the compact form
                (id, label, displayOrder, archived, stages with probability /
                isClosed for deals, ticketState for tickets).
        """
        key, _ = access.resolve_api_key("hubspot")
        c = HubSpotClient(api_key=key)
        try:
            if op == "list":
                raw = fetch_pipelines(c, object_type)
                if verbose:
                    return {"results": raw}
                return {"object_type": object_type,
                        "results": [compact_pipeline(p, object_type) for p in raw]}
            if op == "get":
                if not pipeline_id:
                    raise _bad("op='get' requires pipeline_id (from op='list')")
                raw = c._request(
                    "GET", f"/crm/v3/pipelines/{object_type}/{pipeline_id}") or {}
                return raw if verbose else compact_pipeline(raw, object_type)
            raise _bad("op must be 'list' or 'get'")
        except UpstreamHTTPError as e:
            refus = translate(e, object_type=object_type, object_id=pipeline_id,
                              family=f"pipelines_{object_type}", what="pipeline")
            if refus is None:
                raise          # any other upstream refusal keeps its shape and its trace
            raise refus from None
