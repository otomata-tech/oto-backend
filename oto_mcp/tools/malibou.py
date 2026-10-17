"""Malibou tool — French payroll and HR, read-only.

Wraps `oto.tools.malibou.MalibouClient` (Public API v1, `Authorization: Bearer`),
credential resolved per call via `access.resolve_credential_fields("malibou")`:
the API key and the organization id, which every route carries in its path.

The connector is read-only, so it serves ONE tool, `malibou` (LECTURE), the read
verb in `op` — no `malibou_act` (`tests/test_convention_act.py`).

Personal data: with the key's "details" scope, employees carry the NIR (`ssn` in
the list, `socialSecurityNumber` in the record) and `bankAccount {iban, bic}`.
They are masked by the redaction floor (`field_filter_defaults.SERVER_DEFAULTS
["malibou"]`) until an org_admin lifts it. An absence's leave code comes out as
`absence_type` — the floor masks it too (sick leave is health data), and a rule on
`type` would hit its homonyms.

Client calls are written out (`_client().list_collaborators(…)`) so the
version-skew probe (`test_tools_client_methods_exist`) can check them.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from fastmcp import FastMCP
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, output_projection
from ..connectors import verify as connector_verify
from ..mcp_errors import McpError
from .lecture import LECTURE

_NAME = "malibou"

_OPS = ("collaborators", "collaborator", "absences", "work_locations")


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _need(value: Any, name: str, op: str) -> Any:
    if value is None or value == "":
        raise _bad(f"`{name}` is required for op='{op}'. Nothing was done.")
    return value


def _refuse_ignored(op: str, **given: Any) -> None:
    """No argument is silently dropped: one that `op` does not read is refused."""
    stray = sorted(k for k, v in given.items() if v is not None)
    if stray:
        raise _bad(f"op='{op}' does not take {', '.join(f'`{k}`' for k in stray)}. "
                   "Nothing was done.")


def _upstream_message(e) -> str:
    """Turn a Malibou refusal into an actionable message, by status."""
    status = e.status_code
    detail = e.body if isinstance(e.body, (str, dict)) else ""
    if status == 400:
        return f"Malibou rejected the request as invalid (400): {detail}"
    if status == 401:
        return ("Malibou rejected the API key (401): it is invalid or revoked — an "
                "organization admin creates one under Admin → Company → API keys.")
    if status == 403:
        return ("Malibou refused the call (403): the API key lacks the scope this read "
                "needs (employees, absences or work locations), or the organization ID "
                "set on the connector is not the key's.")
    if status == 404:
        return f"Malibou: not found (404) — check the collaborator id. {detail}"
    if status == 429:
        return "Malibou: too many requests (429) — retry later."
    if status in (500, 502, 503, 504):
        return f"Malibou is temporarily unavailable (HTTP {status}) — retry later."
    return f"Malibou refused the request (HTTP {status}): {detail}"


#: What the default (non-`full`) list view drops from each row: personal details and
#: bookkeeping nobody reads at triage. `full=True` returns the raw rows.
_COLLABORATOR_ROW_DROP = ("address", "bankAccount", "ssn", "birthName", "birthDate",
                          "birthCity", "nationality", "secondaryEmail", "phoneNumber",
                          "isProfileComplete")
_RECORD_ROW_DROP = ("createdAt",)


def _view(result: Any, full: bool, drop: tuple) -> Any:
    """The default list view: each row of `data` without `drop`; envelope (`total`,
    `page`, `limit`) kept. `full=True` returns the response untouched."""
    if full:
        return result
    return output_projection.project(result, items_path="data", item_drop=drop)


def _absence_type(result: Any) -> Any:
    """Serve an absence's leave code as `absence_type`: a unique leaf name, so the
    redaction floor can mask it without touching other `type` fields."""
    if not isinstance(result, dict) or not isinstance(result.get("data"), list):
        return result
    rows = [{("absence_type" if k == "type" else k): v for k, v in row.items()}
            if isinstance(row, dict) else row for row in result["data"]]
    return {**result, "data": rows}


def _verify(fields: dict, config: dict | None = None) -> None:  # noqa: ARG001
    """"Test connection" probe: reads one employee (`list_collaborators(limit=1)`) —
    no side effect; 401 on a wrong key, 403 on a missing employee scope or a
    mismatched organization ID."""
    from oto.tools.malibou import MalibouClient
    from oto.tools.common.errors import UpstreamHTTPError

    try:
        MalibouClient(api_key=fields.get("api_key"),
                      organization_id=fields.get("organization_id")).probe()
    except UpstreamHTTPError as e:
        if e.status_code in (401, 403):
            raise connector_verify.NonAutorise(f"Malibou HTTP {e.status_code}: {e.body}")
        raise RuntimeError(f"Malibou HTTP {e.status_code}: {e.body}")
    except ValueError as e:
        raise connector_verify.NonAutorise(str(e))


def register(mcp: FastMCP) -> None:
    from oto.tools.malibou import MalibouClient
    from oto.tools.common.errors import UpstreamHTTPError

    connector_verify.register(_NAME, _verify)

    def _client() -> MalibouClient:
        creds = access.resolve_credential_fields(_NAME)
        return MalibouClient(api_key=creds.get("api_key"),
                             organization_id=creds.get("organization_id"))

    def _run(fn):
        try:
            return fn()
        except ValueError as e:
            raise _bad(str(e))
        except UpstreamHTTPError as e:
            raise _bad(_upstream_message(e))

    @mcp.tool(name="malibou", annotations=LECTURE)
    def malibou(
        op: Literal[_OPS] = "collaborators",
        collaborator_id: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        status: Optional[Literal["approved", "pending", "rejected"]] = None,
        contract_status: Optional[Literal["past", "active", "future"]] = None,
        establishment_id: Optional[str] = None,
        page: Optional[int] = None,
        limit: Optional[int] = None,
        full: bool = False,
    ) -> Any:
        """Malibou payroll and HR, read-only: the organization's employees and their
        contracts, absences, and declared work locations (remote or on site).

        `op`:
        - `collaborators` — the employees, ordered by last then first name, each
          with their current contract (`natureContrat`, `estCadre`), team and
          manager. `contract_status` narrows on today's employment: `active` (at
          least one active contract), `past` (all ended), `future` (only an
          upcoming one).
        - `collaborator` — one employee (`collaborator_id`) with every contract
          (`contractId`, `startDate`, `endDate`); not wrapped in `data`.
        - `absences` — absences OVERLAPPING `start_date`..`end_date` (both
          required), with their status and half-days; the leave code is
          `absence_type`.
        - `work_locations` — declared remote or on-site work overlapping the
          window (`isRemoteWork`, `location`).

        Lists answer `{data, total, page, limit}`; `page` from 1, the last page
        holds fewer than `limit` items. Which fields come out depends on the scopes
        of the key: without the details scope, no NIR, address, phone or bank
        account. NIR, IBAN/BIC and `absence_type` are masked by default until an
        org admin lifts the rule. Lists drop by default each employee's personal
        details (address, bank account, NIR, birth, nationality, phone, secondary
        email) and each record's `createdAt`; `full=true` returns raw rows. No payslips, payroll variables or company data:
        the self-serve API does not serve them.

        Args:
            op: collaborators | collaborator | absences | work_locations.
            collaborator_id: op='collaborator' — the employee id, from op='collaborators'.
            start_date: absences / work_locations — window start, YYYY-MM-DD.
            end_date: absences / work_locations — window end, YYYY-MM-DD.
            status: absences / work_locations — approved | pending | rejected.
            contract_status: op='collaborators' — past | active | future.
            establishment_id: only this establishment.
            page: page number, from 1 (lists).
            limit: items per page (lists).
            full: lists — raw rows instead of the trimmed view.
        """
        if op == "collaborator":
            _need(collaborator_id, "collaborator_id", op)
            _refuse_ignored(op, start_date=start_date, end_date=end_date, status=status,
                            contract_status=contract_status, page=page, limit=limit,
                            full=full or None)
            return _run(lambda: _client().get_collaborator(
                collaborator_id, establishment_id=establishment_id))
        if op == "collaborators":
            _refuse_ignored(op, collaborator_id=collaborator_id, start_date=start_date,
                            end_date=end_date, status=status)
            return _view(_run(lambda: _client().list_collaborators(
                establishment_id=establishment_id, contract_status=contract_status,
                page=page, limit=limit)), full, _COLLABORATOR_ROW_DROP)
        _need(start_date, "start_date", op)
        _need(end_date, "end_date", op)
        _refuse_ignored(op, collaborator_id=collaborator_id,
                        contract_status=contract_status)
        if op == "absences":
            return _view(_absence_type(_run(lambda: _client().list_absences(
                start_date, end_date, status=status, establishment_id=establishment_id,
                page=page, limit=limit))), full, _RECORD_ROW_DROP)
        return _view(_run(lambda: _client().list_work_locations(
            start_date, end_date, status=status, establishment_id=establishment_id,
            page=page, limit=limit)), full, _RECORD_ROW_DROP)
