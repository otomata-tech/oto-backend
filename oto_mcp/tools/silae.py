"""Silae — French payroll, the READERS (dossiers, employees, payslips; reports and
declarations live in `silae_documents.py`, the entries in `silae_act.py`).

Credential = OAuth2 client-credentials (Azure AD B2C), three secrets (client_id +
client_secret + subscription_key), resolved per call via
`access.resolve_credential_fields("silae")` (ADR 0011). byo_user: each payroll firm or
employer enters its own Silae API credentials.

Surface (ADR 0047 §Amendment): one tool per business OBJECT, the verb in `op` —
`silae_dossier`, `silae_employee`, `silae_payslip`, `silae_variables_to_enter` here;
`silae_report` and `silae_declaration` in `silae_documents.py`. Routes and bodies follow
the Silae function pages and OpenAPI file (`oto.tools.silae`).

⚠️ **This module and `silae_documents.py` never write.** `SilaeClient`'s four writes
(`ajouter_element_variable`, `ajouter_prime`, `ajouter_heures`, `confirmer_saisies`) are
served by `silae_act` ONLY (`tools/silae_act.py`, `ACTE`, the `full` access level — `read`
is the default); `tests/test_silae_op_dispatch.py` verifies the readers never reach them
(every op played, the writes `assert_not_called`, plus a static check of both modules).
The functions that change payroll state beyond entries (payslip control, DSN
configuration, pay cycle, show-business payslip computation) are not even in the client.

⚠️ **Redaction is NOT active by default**: `field_filter_defaults.SERVER_DEFAULTS` has
nothing for `silae`, so JSON responses (NIR, names, amounts) reach the agent in clear
until the org sets a policy (`FieldRedactionMiddleware`, by namespace). A DOCUMENT
cannot be filtered: `silae_documents.py` refuses documents while the org's policy masks
anything.

⚠️ **The subscription key BOUNDS the perimeter** (dossiers AND functions): a dossier
missing from `silae_dossier()` may simply be outside it. Silae errors RAISE in the
client (`UpstreamHTTPError`, Silae's `errors[]` body) and become a named tool error.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Literal, Optional

from fastmcp import FastMCP
from mcp.types import ErrorData, INVALID_PARAMS
from pydantic import BaseModel, ConfigDict, Field

from .. import access
from ..connectors import verify as connector_verify
from ..mcp_errors import McpError

if TYPE_CHECKING:
    from oto.tools.silae import SilaeClient

_NAME = "silae"
#: Payslip zones, named for the agent → Silae's code.
_ZONES = {"pay": 2, "contributions": 3, "net": 4}
_TYPES_DETAILS = {"header": 1, "lines": 2, "both": 3}


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _client() -> SilaeClient:
    """The client for THIS caller's credential. The real import is in the body: the
    tests replace the package's class. No `field_filter`: redaction is the
    middleware's job (module docstring)."""
    from oto.tools.silae import SilaeClient

    creds = access.resolve_credential_fields(_NAME)
    return SilaeClient(client_id=creds.get("client_id"),
                       client_secret=creds.get("client_secret"),
                       subscription_key=creds.get("subscription_key"))


def _upstream_message(e: Any) -> str:
    """Silae's own error (`errors[].code/message`) with what the status means."""
    body = e.body
    if isinstance(body, dict) and isinstance(body.get("errors"), list):
        detail = "; ".join(f"{x.get('code')}: {x.get('message')}" for x in body["errors"]
                           if isinstance(x, dict))
    else:
        detail = str(body)[:500]
    if e.status_code == 401:
        return (f"Silae HTTP 401 — the credential or the subscription key is refused "
                f"({detail}). Check the three fields of the silae connector.")
    if e.status_code == 403:
        return (f"Silae HTTP 403 — this access configuration does not open this function "
                f"or this dossier ({detail}).")
    return f"Silae HTTP {e.status_code}: {detail}"


def run(fn: Callable[[], Any]) -> Any:
    """Run a client call; a validation or upstream error becomes a named tool error."""
    from oto.tools.common.errors import UpstreamHTTPError

    try:
        return fn()
    except ValueError as e:
        raise _bad(f"Silae: {e}") from None
    except UpstreamHTTPError as e:
        raise _bad(_upstream_message(e)) from None


def need(op: str, **required: Any) -> None:
    """Arguments THIS op requires. `""` counts as absent: Silae reads an empty
    matricule as "every employee" — a plausible and wrong answer."""
    missing = [n for n, v in required.items() if v is None or v == ""]
    if missing:
        raise _bad(f"op='{op}' requires {', '.join(missing)}")


def only(op: str, uses: tuple[str, ...], **provided: Any) -> None:
    """An argument THIS op does not use is an intent error, never silently dropped
    (`is not None`: `False` and `0` are provided values)."""
    for name, value in provided.items():
        if name not in uses and value is not None:
            raise _bad(f"op='{op}' does not use {name}")


def check_op(op: str, allowed: tuple[str, ...]) -> None:
    """An unknown op is refused naming the valid ones — never a fallback on the default."""
    if op not in allowed:
        raise _bad(f"op must be {', '.join(repr(a) for a in allowed[:-1])} or {allowed[-1]!r}")


def months(op: str, periode: Optional[str], periode_debut: Optional[str],
           periode_fin: Optional[str]) -> tuple[str, str]:
    """A month range: `periode` alone (one month) OR `periode_debut` + `periode_fin`."""
    if periode is not None:
        if periode_debut is not None or periode_fin is not None:
            raise _bad(f"op='{op}': give `periode` (one month) OR periode_debut + "
                       "periode_fin, not both")
        return periode, periode
    need(op, periode_debut=periode_debut, periode_fin=periode_fin)
    return periode_debut, periode_fin


def _verify(fields: dict, config: dict | None = None) -> None:
    """"Test the connection" probe — covers `auth` ONLY (otomata-tech/oto#69).

    `ListeDossiers`, the smallest call: Silae exposes neither `/me` nor a balance. A
    refusal (token endpoint, subscription key: 401/403) is `NonAutorise`; anything
    else (5xx, network, a 400 we caused) RAISES as is and reads `unknown` — a failure,
    not a refused key. An EMPTY list is a normal state, never a refusal."""
    from oto.tools.common.errors import UpstreamHTTPError
    from oto.tools.silae import SilaeClient

    try:
        SilaeClient(client_id=fields["client_id"], client_secret=fields["client_secret"],
                    subscription_key=fields["subscription_key"]).list_dossiers()
    except UpstreamHTTPError as e:
        if e.status_code in (401, 403):
            raise connector_verify.NonAutorise(_upstream_message(e)) from e
        raise


class LineFilters(BaseModel):
    """Payslip line filters; text accepts `%` as a wildcard ("SS0%")."""
    model_config = ConfigDict(extra="forbid")

    zone: Optional[Literal["pay", "contributions", "net"]] = None
    code_ducs: Optional[str] = None
    code_libelle: Optional[str] = None
    libelle: Optional[str] = None
    compte6: Optional[str] = Field(None, description="charge account")
    exclure_lignes_neutres: Optional[bool] = Field(None, description="drop greyed lines")

    def silae(self) -> dict:
        out = self.model_dump(exclude_none=True)
        if "zone" in out:
            out["zone"] = _ZONES[out["zone"]]
        return out


def register(mcp: FastMCP) -> None:
    connector_verify.register(_NAME, _verify)

    @mcp.tool()
    def silae_dossier(
        op: Literal["list", "numbers", "collective_agreements", "current_period",
                    "establishments", "organisms"] = "list",
        numero_dossier: Optional[str] = None,
        code_organisme: Optional[str] = None,
        code_nature: Optional[str] = None,
        etablissement: Optional[str] = None,
    ) -> object:
        """A payroll dossier — what the key reaches, or one dossier's settings.

        `op`:
        - "list" (default): reachable dossiers (number, company name, SIRET, state).
        - "numbers": only their numbers.
        - "collective_agreements": the collective agreements of EVERY reachable dossier.
        - "current_period": the open pay period of `numero_dossier`.
        - "establishments": its establishments (internal name, SIRET, main).
        - "organisms": its social bodies (URSSAF, pension, provident…) with affiliation
          and payment settings — Silae contracts "Usage interne" (1A/1B) only.

        Args:
            numero_dossier: required by current_period, establishments, organisms.
            code_organisme: organisms — one body by Silae code.
            code_nature: organisms — SSOC, CH, ARRCO, AGIRC, GRS, RPO, RPS, CCP, MT, FP,
                RSPECIAL, TS, ATCS or DIV.
            etablissement: organisms — one establishment (internal name).
        """
        check_op(op, ("list", "numbers", "collective_agreements", "current_period",
                      "establishments", "organisms"))
        args = dict(numero_dossier=numero_dossier, code_organisme=code_organisme,
                    code_nature=code_nature, etablissement=etablissement)
        if op in ("list", "numbers", "collective_agreements"):
            only(op, (), **args)
            c = _client()
            if op == "list":
                return run(lambda: c.list_dossiers())
            if op == "numbers":
                return run(lambda: c.list_numeros_dossiers())
            return run(lambda: c.list_conventions_collectives())
        need(op, numero_dossier=numero_dossier)
        if op == "current_period":
            only(op, ("numero_dossier",), **args)
            return run(lambda: _client().dossier_periode_en_cours(numero_dossier))
        if op == "establishments":
            only(op, ("numero_dossier",), **args)
            return run(lambda: _client().list_etablissements(numero_dossier))
        return run(lambda: _client().list_organismes(
            numero_dossier, code_organisme=code_organisme, code_nature=code_nature,
            etablissement=etablissement))

    @mcp.tool()
    def silae_employee(
        numero_dossier: str,
        op: Literal["list", "get", "jobs", "from_internal_id"] = "list",
        matricule_salarie: Optional[str] = None,
        matricule_interne: Optional[str] = None,
        type_emplois: Optional[int] = None,
        periode: Optional[str] = None,
        actif_a_la_date: Optional[str] = None,
    ) -> object:
        """An employee of a dossier.

        `op`:
        - "list" (default): matricule, displayed name, NIR, emails.
        - "get": the employee record (identity, NIR, birth, address, contacts, latest job).
        - "jobs": the employee's jobs, each with the `identifiant_emploi` that
          `silae_payslip` needs (an intermittent holds several in a month).
        - "from_internal_id": the Silae matricule of an internal matricule — nothing else.

        Args:
            numero_dossier: dossier number — every op.
            matricule_salarie: required by get and jobs.
            matricule_interne: from_internal_id — the internal matricule.
            type_emplois: jobs — 0 current jobs (default), 1 current + archived.
            periode: list — keep those active in this pay month, `AAAA-MM`.
            actif_a_la_date: list — keep those active on this day, `AAAA-MM-JJ`.
        """
        check_op(op, ("list", "get", "jobs", "from_internal_id"))
        args = dict(matricule_salarie=matricule_salarie, matricule_interne=matricule_interne,
                    type_emplois=type_emplois, periode=periode,
                    actif_a_la_date=actif_a_la_date)
        if op == "list":
            only(op, ("periode", "actif_a_la_date"), **args)
            return run(lambda: _client().list_salaries(
                numero_dossier, actif_sur_periode=periode, actif_a_la_date=actif_a_la_date))
        if op == "get":
            only(op, ("matricule_salarie",), **args)
            need(op, matricule_salarie=matricule_salarie)
            return run(lambda: _client().lecture_informations_salarie(
                numero_dossier, matricule_salarie))
        if op == "jobs":
            only(op, ("matricule_salarie", "type_emplois"), **args)
            need(op, matricule_salarie=matricule_salarie)
            return run(lambda: _client().list_salarie_emplois(
                numero_dossier, matricule_salarie, type_emplois or 0))
        only(op, ("matricule_interne",), **args)
        need(op, matricule_interne=matricule_interne)
        return run(lambda: _client().matricule_depuis_interne(numero_dossier, matricule_interne))

    @mcp.tool()
    def silae_payslip(
        numero_dossier: str,
        op: Literal["pdf_ids", "indices", "header", "lines", "details", "totals"] = "pdf_ids",
        matricule_salarie: Optional[str] = None,
        identifiant_emploi: Optional[int] = None,
        periode: Optional[str] = None,
        periode_debut: Optional[str] = None,
        periode_fin: Optional[str] = None,
        indice_periode: Optional[int] = None,
        type_details: Optional[Literal["header", "lines", "both"]] = None,
        line_filters: Optional[LineFilters] = None,
        originaux_seulement: Optional[bool] = None,
        etablissement: Optional[str] = None,
    ) -> object:
        """Payslips of a dossier. One payslip = matricule + identifiant_emploi (from
        `silae_employee(op="jobs")`) + month + indice_periode (intermittents: several a
        month; op="indices" lists them).

        `op`:
        - "pdf_ids" (default): ids of the payslip PDF IMAGES per employee and month.
        - "indices": the payslips of one job per month.
        - "header": gross, net, taxable net, deduction totals, hours.
        - "lines": code, label, employee/employer base, rate, amount, DUCS code.
        - "details": header and/or lines, filterable.
        - "totals": cumulative totals, 12 months at most.

        Args:
            numero_dossier: dossier number — every op.
            matricule_salarie: required except by pdf_ids (omitted = everyone).
            identifiant_emploi: required except by pdf_ids (omitted = every job) and
                totals.
            periode: pay month `AAAA-MM`; on range ops, a one-month range.
            periode_debut: range start `AAAA-MM` — pdf_ids, indices, totals.
            periode_fin: range end `AAAA-MM`, included.
            indice_periode: header, lines, details — omitted = the first payslip (0).
            type_details: details — header, lines or both (default).
            line_filters: lines, details — filters on the lines.
            originaux_seulement: pdf_ids, indices — only original payslips.
            etablissement: pdf_ids — one establishment (internal name).
        """
        check_op(op, ("pdf_ids", "indices", "header", "lines", "details", "totals"))
        args = dict(matricule_salarie=matricule_salarie, identifiant_emploi=identifiant_emploi,
                    periode=periode, periode_debut=periode_debut, periode_fin=periode_fin,
                    indice_periode=indice_periode, type_details=type_details,
                    line_filters=line_filters, originaux_seulement=originaux_seulement,
                    etablissement=etablissement)
        plage = ("periode", "periode_debut", "periode_fin")
        if op == "pdf_ids":
            only(op, ("matricule_salarie", "identifiant_emploi", "originaux_seulement",
                      "etablissement") + plage, **args)
            debut, fin = months(op, periode, periode_debut, periode_fin)
            return run(lambda: _client().bulletins_ids_pdf(
                numero_dossier, periode_debut=debut, periode_fin=fin,
                matricule_salarie=matricule_salarie or "",
                identifiant_emploi=identifiant_emploi or 0,
                originaux_seulement=originaux_seulement, etablissement=etablissement))
        if op == "totals":
            only(op, ("matricule_salarie",) + plage, **args)
            need(op, matricule_salarie=matricule_salarie)
            debut, fin = months(op, periode, periode_debut, periode_fin)
            return run(lambda: _client().bulletin_cumuls(
                numero_dossier, matricule_salarie, periode_debut=debut, periode_fin=fin))
        need(op, matricule_salarie=matricule_salarie, identifiant_emploi=identifiant_emploi)
        if op == "indices":
            only(op, ("matricule_salarie", "identifiant_emploi", "originaux_seulement")
                 + plage, **args)
            debut, fin = months(op, periode, periode_debut, periode_fin)
            return run(lambda: _client().bulletins_indices(
                numero_dossier, matricule_salarie, identifiant_emploi=identifiant_emploi,
                periode_debut=debut, periode_fin=fin, originaux_seulement=originaux_seulement))
        need(op, periode=periode)
        un = dict(identifiant_emploi=identifiant_emploi, periode=periode,
                  indice_periode=indice_periode)
        base = ("matricule_salarie", "identifiant_emploi", "periode", "indice_periode")
        if op == "header":
            only(op, base, **args)
            return run(lambda: _client().bulletin_entete(numero_dossier, matricule_salarie, **un))
        if op == "lines":
            only(op, base + ("line_filters",), **args)
            filtres = line_filters.silae() if line_filters else {}
            if filtres:
                return run(lambda: _client().bulletin_lignes_filtrees(
                    numero_dossier, matricule_salarie, filtres=filtres, **un))
            return run(lambda: _client().bulletin_lignes(numero_dossier, matricule_salarie, **un))
        only(op, base + ("line_filters", "type_details"), **args)
        filtres = line_filters.silae() if line_filters else None
        return run(lambda: _client().bulletin_details(
            numero_dossier, matricule_salarie, filtres=filtres,
            type_details=_TYPES_DETAILS[type_details or "both"], **un))

    @mcp.tool()
    def silae_variables_to_enter(numero_dossier: str) -> object:
        """The variable payroll elements (EVP) defined for entry in a dossier: name,
        family, labels, type, format — definitions, not entered values.

        Args:
            numero_dossier: Dossier (folder) number.
        """
        return run(lambda: _client().list_variables_a_saisir(numero_dossier))
