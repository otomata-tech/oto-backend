"""Pennylane general ledger — read entries, post some, letter lines.

Second module of the `pennylane` connector (see `Connector.modules` in the registry):
same key, same client, distinct domain. Journals, for their part, are reference data
and are read through `pennylane_ref(kind="journals")`, with the others.

⚠️ **This is not the GED (document management).** The `pennylaneged` connector targets the same company
through another door (the interface's private API, browser session), and its
`company_id`s are NOT the ones here. An id taken from one and played in the other
returns a refusal that mimics an expired session.

⚠️ **Three scopes, not one.** Pennylane split the old `ledger` scope: reading
entries requires `ledger_entries:*`, journals `journals:*`, the chart
of accounts `ledger_accounts:*`. A key that reads one does not necessarily read the
others, and the scope is specific to whoever created the key. The real permissions are
read with `pennylane_ref(kind="company")`, `scopes` field.
"""
from __future__ import annotations

from typing import Literal, Optional

from fastmcp import FastMCP

from .pennylane_socle import _analytique, _bad, _client, _ecrit, _need


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    def pennylane_ledger_entry(
        op: Literal["list", "get", "lines", "lettered", "create",
                    "update", "categories", "categorize", "search_lines", "line",
                    "documents"] = "list",
        entry_id: Optional[int] = None,
        line_id: Optional[int] = None,
        clauses: Optional[list] = None,
        max_pages: Optional[int] = None,
        date: Optional[str] = None,
        label: Optional[str] = None,
        journal_id: Optional[int] = None,
        lines: Optional[list] = None,
        due_date: Optional[str] = None,
        currency: Optional[str] = None,
        piece_number: Optional[str] = None,
        fields: Optional[dict] = None,
        categories: Optional[list] = None,
    ) -> dict | list:
        """General-ledger entries: read, post an entry, correct it.

        ⚠️ **`op="create"` has NO draft, and the action is irreversible.**
        Everywhere else in this connector, a committing entry is posted as a
        draft then finalized in a second action, after human
        validation. Pennylane does not offer this step for an accounting entry:
        it is posted immediately, and the API cannot delete it — the
        only recourse is `op="update"`, which can itself destroy lines.
        **Announce the exact detail to the user — journal, date, label, and
        each line with its account and amount — and wait for their agreement
        BEFORE calling.**

        ⚠️ **`op="list"` without `clauses` brings back the WHOLE history** — on a
        real accounting system, thousands of entries, well beyond the token
        limit. Filtering at the source is the only way to find an
        entry; `max_pages` limits the damage but targets nothing.

        Args:
            op: "list" — the entries, to be filtered with `clauses`;
                "get" — ONE entry by its `entry_id`;
                "lines" — the lines of an entry, with their `id` (this is the
                    id that lettering consumes, not the entry's);
                "lettered" — the lines lettered WITH the line `line_id`, to
                    see what a lettering actually associated;
                "categories" — the ANALYTICAL categories of the LINE `line_id`,
                    each with its `weight`;
                "categorize" — REPLACES all the analytical categories of the LINE
                    `line_id` with `categories` (see below);
                "search_lines" — entry LINES across ALL entries, filtered with
                    `clauses` on `id`, `journal_id`, `ledger_account_id`,
                    `ledger_entry_id`, `date` (all operators): the lines of one
                    account over a period. ⚠️ Without `clauses`, the whole ledger;
                "line" — ONE line by its `line_id`;
                "documents" — the documents attached to the entry `entry_id`
                    (`{id, name, url}`), the receipts it was booked from.
            entry_id: entry id — required by "get", "lines" and "documents".
            line_id: id of an entry LINE (from op="lines") — required by
                "lettered", "line", "categories" and "categorize".
            clauses: server-side filter, list of `{"field", "operator", "value"}`.
                Filterable fields: `id`, `date`, `journal_id`. Operators:
                `lt`, `lteq`, `gt`, `gteq`, `eq`, `not_eq`, plus `in` and
                `not_in` on `id` and `journal_id`. Example:
                `[{"field": "date", "operator": "gteq", "value": "2026-01-01"}]`.
            max_pages: bounds the number of pages fetched.
            date: op="create" — entry date (YYYY-MM-DD).
            label: op="create" — entry label.
            journal_id: op="create" — the journal in which to post the entry. Resolve it
                with `pennylane_ref(kind="journals")`: these ids are specific to
                the company, never to be hard-coded.
            lines: op="create" — the lines, 1 to 1000. Each `{"debit": "…",
                "credit": "…", "ledger_account_id": …}` and an optional `label`.
                Amounts are decimal STRINGS ("120.50"), and
                debits must equal credits — otherwise the call is refused
                before reaching Pennylane, with the figure of the gap. Accounts
                are resolved with `pennylane_ref(kind="ledger_accounts")`.
            due_date / currency / piece_number: op="create", optional
                (EUR currency by default, auto-generated document number).
            fields: op="update" — the fields to modify on the entry.
                ⚠️ `ledger_entry_lines` takes `create`/`update`/`delete` there: this
                action can DELETE lines: it commits as much as a
                creation, and is announced in the same way.
            categories: op="categorize" — `[{"id": …, "weight": "0.5"}]`, weight as a
                STRING between 0 and 1, the weights of ONE axis sum to 1, axes are
                independent; `[]` removes them all. Ids from `pennylane_ref(kind=
                "categories")`. Reversible: re-send the list read with
                op="categories".
        """
        c = _client()
        if op == "list":
            return c.get_ledger_entries(max_pages=max_pages, clauses=clauses)
        if op == "get":
            return c.get_ledger_entry(_need(entry_id, "entry_id", op))
        if op == "lines":
            return c.get_ledger_entry_lines(_need(entry_id, "entry_id", op),
                                            max_pages=max_pages)
        if op == "lettered":
            return c.get_lettered_lines(_need(line_id, "line_id", op),
                                        max_pages=max_pages)
        if op == "create":
            return _ecrit(lambda: c.create_ledger_entry(
                date=_need(date, "date", op), label=_need(label, "label", op),
                journal_id=_need(journal_id, "journal_id", op),
                ledger_entry_lines=_need(lines, "lines", op),
                due_date=due_date, currency=currency, piece_number=piece_number),
                "accounting entry creation")
        if op == "update":
            return _ecrit(lambda: c.update_ledger_entry(_need(entry_id, "entry_id", op),
                                                **(fields or {})),
                          "accounting entry correction")
        if op in ("categories", "categorize"):
            return _analytique(c, op, "ledger_entry_line",
                               _need(line_id, "line_id", op), categories)
        if op == "search_lines":
            try:
                return c.search_ledger_entry_lines(filter=clauses, max_pages=max_pages)
            except ValueError as e:      # malformed clauses, refused before the call
                raise _bad(f"op='search_lines': {e}") from e
        if op == "line":
            return c.get_ledger_entry_line(_need(line_id, "line_id", op))
        if op == "documents":
            return c.list_ledger_entry_documents(_need(entry_id, "entry_id", op),
                                                 max_pages=max_pages)
        raise _bad("op must be 'list', 'get', 'lines', 'lettered', 'create', "
                   "'update', 'categories', 'categorize', 'search_lines', 'line' or "
                   "'documents'")

    @mcp.tool()
    def pennylane_ledger_lettering(
        op: Literal["set", "unset"],
        line_ids: list[int],
        unbalanced_lettering_strategy: Literal["none", "partial"] = "none",
    ) -> dict:
        """Letter general-ledger LINES with each other, or undo that lettering.

        ⚠️ **This is not `pennylane_match`.** The word "lettering" covers two
        actions on two objects: reconciling a bank transaction with an
        invoice is `pennylane_match`; associating entry lines
        with each other in the general ledger is here. Picking the wrong tool does not produce an
        error, only an action performed in the wrong place.

        ⚠️ **Lettering is ABSORBING**: if a line passed is already lettered,
        the lettering extends to those already associated with it. Asking for
        [A, C] when A and B are lettered produces [A, B, C]. To see
        what was actually associated, re-read with
        `pennylane_ledger_entry(op="lettered", line_id=…)`.

        The action is reversible (`op="unset"`), which distinguishes it from an
        accounting entry.

        Args:
            op: "set" to letter, "unset" to undo.
            line_ids: at least two entry LINE ids — not entry
                ids. They are read with
                `pennylane_ledger_entry(op="lines", entry_id=…)`.
            unbalanced_lettering_strategy: "none" refuses an unbalanced
                lettering (default), "partial" accepts it.
        """
        c = _client()
        if op == "set":
            return _ecrit(lambda:
                c.letter_ledger_entry_lines(line_ids, unbalanced_lettering_strategy),
                "general-ledger line lettering")
        if op == "unset":
            return _ecrit(lambda:
                c.unletter_ledger_entry_lines(line_ids, unbalanced_lettering_strategy),
                "general-ledger line unlettering")
        raise _bad("op must be 'set' or 'unset'")
