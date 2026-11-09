"""Pennylane EXPORTS — the FEC, the general ledger and the analytical general
ledger of a period, built in the background by Pennylane.

Module of the `pennylane` connector (see `Connector.modules` in the registry): same
key, same client. `op="start"` asks for the export and returns `{id, status:
pending}`; `op="get"` reads it again until `status` is `ready` (or `error`), with
`file_url`, an address that expires 30 minutes after the read.

Each op calls the client method of the description's function (`oto-core`,
`connectors/pennylane/connector.yaml`): `create_<kind>_export`, `get_<kind>_export`.
"""
from __future__ import annotations

from typing import Literal, Optional

from fastmcp import FastMCP

from .pennylane_socle import _bad, _client, _ecrit, _need


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    def pennylane_export(
        op: Literal["start", "get"],
        kind: Literal["fec", "general_ledger", "analytical_general_ledger"],
        period_start: Optional[str] = None,
        period_end: Optional[str] = None,
        mode: Optional[Literal["in_line", "in_column"]] = None,
        export_id: Optional[int] = None,
    ) -> dict:
        """Pennylane accounting EXPORTS of a period: the FEC (the French legal
        ledger file), the general ledger (xlsx), the analytical general ledger (xlsx,
        the lines with their analytical categories).

        `op`:
        - "start" (`kind`, `period_start`, `period_end`; `mode` for the analytical
          ledger: in_line — one row per line and category, the default — or
          in_column — one column per axis): returns `{id, status: "pending"}`.
        - "get" (`kind`, `export_id`): `status` pending | ready | error, and
          `file_url` once ready. The `file_url` EXPIRES 30 minutes after this read:
          hand it on at once (e.g. a `{"kind": "url"}` file source), or read the
          export again for a fresh one. Pending: wait a little and read again.

        Scopes: `exports:fec`, `exports:gl`, `exports:agl` — one per kind.

        Args:
            op: "start" | "get".
            kind: "fec" | "general_ledger" | "analytical_general_ledger".
            period_start / period_end: op="start" — YYYY-MM-DD.
            mode: op="start", kind="analytical_general_ledger" only.
            export_id: op="get" — the `id` returned by op="start".
        """
        if mode is not None and kind != "analytical_general_ledger":
            raise _bad("mode only applies to kind='analytical_general_ledger'")
        c = _client()
        if op == "start":
            debut = _need(period_start, "period_start", op)
            fin = _need(period_end, "period_end", op)
            if kind == "fec":
                return _ecrit(lambda: c.create_fec_export(debut, fin), "FEC export")
            if kind == "general_ledger":
                return _ecrit(lambda: c.create_general_ledger_export(debut, fin),
                              "general ledger export")
            return _ecrit(lambda: c.create_analytical_general_ledger_export(
                debut, fin, mode=mode), "analytical general ledger export")
        if op == "get":
            ident = _need(export_id, "export_id", op)
            lecture = {"fec": c.get_fec_export, "general_ledger": c.get_general_ledger_export,
                       "analytical_general_ledger": c.get_analytical_general_ledger_export}
            return lecture[kind](ident)
        raise _bad("op must be 'start' or 'get'")
