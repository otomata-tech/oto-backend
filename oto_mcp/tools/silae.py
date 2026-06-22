"""Silae — French payroll (read-only).

Credential = OAuth2 client-credentials (Azure AD B2C), three secrets
(client_id + client_secret + subscription_key). Resolved per call via
`access.resolve_credential_fields("silae")` — generic multi-field model
(ADR 0011). byo_user: each payroll cabinet / employer enters its own Silae API
credentials; its payroll is visible only to it.

Read-only surface (dossiers, employees, payslips, variables awaiting entry).
The write operations (adding a bonus/hours, confirming staged entries) stay out
of the agent for now — entering payroll is a sensitive act. Bank details
(IBAN/BIC/RIB) are masked before the response reaches the agent : the redaction
is applied at the tool boundary by `FieldRedactionMiddleware` (server default in
`field_filter_defaults.SERVER_DEFAULTS`, overridable by the org_admin), not here.
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP

from .. import access


def register(mcp: FastMCP) -> None:
    from oto.tools.silae import SilaeClient

    def _client() -> SilaeClient:
        creds = access.resolve_credential_fields("silae")
        # Rédaction (masque IBAN/BIC/RIB par défaut) appliquée à la frontière des
        # tools par `FieldRedactionMiddleware`, plus au niveau client.
        return SilaeClient(
            client_id=creds.get("client_id"),
            client_secret=creds.get("client_secret"),
            subscription_key=creds.get("subscription_key"),
        )

    # --- Dossiers (payroll files) ---

    @mcp.tool()
    async def silae_dossiers() -> object:
        """List the payroll dossiers (folders) reachable with the API key."""
        return _client().list_dossiers()

    @mcp.tool()
    async def silae_dossier_numbers() -> object:
        """List just the dossier numbers reachable with the API key."""
        return _client().list_numeros_dossiers()

    @mcp.tool()
    async def silae_dossier_info(numero_dossier: str) -> object:
        """Detailed payroll information for a dossier.

        Args:
            numero_dossier: Dossier (folder) number.
        """
        return _client().dossier_infos(numero_dossier)

    @mcp.tool()
    async def silae_dossier_current_period(numero_dossier: str) -> object:
        """Current open payroll period for a dossier.

        Args:
            numero_dossier: Dossier (folder) number.
        """
        return _client().dossier_periode_en_cours(numero_dossier)

    # --- Salariés (employees) ---

    @mcp.tool()
    async def silae_employees(numero_dossier: str) -> object:
        """List the employees of a dossier.

        Args:
            numero_dossier: Dossier (folder) number.
        """
        return _client().list_salaries(numero_dossier)

    @mcp.tool()
    async def silae_employee(numero_dossier: str, matricule_salarie: str) -> object:
        """Fetch one employee by registration number (matricule).

        Args:
            numero_dossier: Dossier (folder) number.
            matricule_salarie: Employee registration number.
        """
        return _client().salarie_matricule(numero_dossier, matricule_salarie)

    @mcp.tool()
    async def silae_employee_jobs(
        numero_dossier: str,
        matricule_salarie: str = "",
        type_emplois: int = 0,
    ) -> object:
        """List an employee's jobs/positions (emplois).

        Args:
            numero_dossier: Dossier (folder) number.
            matricule_salarie: Employee matricule (empty = all employees).
            type_emplois: 0 = current jobs only, 1 = current + archived.
        """
        return _client().list_salarie_emplois(
            numero_dossier, matricule_salarie, type_emplois
        )

    # --- Bulletins (payslips) ---

    @mcp.tool()
    async def silae_payslips(
        numero_dossier: str, periode: str, matricule_salarie: str = ""
    ) -> object:
        """Retrieve payslips for a period (one employee or the whole dossier).

        Args:
            numero_dossier: Dossier (folder) number.
            periode: Payroll period (e.g. "2026-05").
            matricule_salarie: Employee matricule, or empty for all employees.
        """
        return _client().bulletins(numero_dossier, periode, matricule_salarie)

    @mcp.tool()
    async def silae_payslip_header(
        numero_dossier: str, matricule_salarie: str, periode: str
    ) -> object:
        """Payslip header (entête) for one employee/period.

        Args:
            numero_dossier: Dossier (folder) number.
            matricule_salarie: Employee matricule.
            periode: Payroll period (e.g. "2026-05").
        """
        return _client().bulletin_entete(numero_dossier, matricule_salarie, periode)

    @mcp.tool()
    async def silae_payslip_lines(
        numero_dossier: str, matricule_salarie: str, periode: str
    ) -> object:
        """Payslip lines (lignes) for one employee/period.

        Args:
            numero_dossier: Dossier (folder) number.
            matricule_salarie: Employee matricule.
            periode: Payroll period (e.g. "2026-05").
        """
        return _client().bulletin_lignes(numero_dossier, matricule_salarie, periode)

    @mcp.tool()
    async def silae_payslip_totals(
        numero_dossier: str, matricule_salarie: str, periode: str
    ) -> object:
        """Payslip cumulative totals (cumuls) for one employee/period.

        Args:
            numero_dossier: Dossier (folder) number.
            matricule_salarie: Employee matricule.
            periode: Payroll period (e.g. "2026-05").
        """
        return _client().bulletin_cumuls(numero_dossier, matricule_salarie, periode)

    # --- Variables de paie (EVP) ---

    @mcp.tool()
    async def silae_variables_to_enter(numero_dossier: str) -> object:
        """List the payroll variables (EVP) still awaiting entry for a dossier.

        Args:
            numero_dossier: Dossier (folder) number.
        """
        return _client().list_variables_a_saisir(numero_dossier)
