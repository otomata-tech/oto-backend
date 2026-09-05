"""Silae — French payroll (read-only).

Credential = OAuth2 client-credentials (Azure AD B2C), three secrets
(client_id + client_secret + subscription_key). Resolved per call via
`access.resolve_credential_fields("silae")` — generic multi-field model
(ADR 0011). byo_user: each payroll cabinet / employer enters its own Silae API
credentials; its payroll is visible only to it.

Read-only surface (dossiers, employees, payslips, variables awaiting entry).
The write operations (adding a bonus/hours, confirming staged entries) stay out
of the agent for now — entering payroll is a sensitive act. ⚠️ `SilaeClient` DOES
carry them (`ajouter_element_variable`, `ajouter_prime`, `ajouter_heures`,
`confirmer_saisies`) : aucun tool d'ici ne les atteint, et
`tests/test_silae_op_dispatch.py` le VÉRIFIE (toutes les ops jouées, les quatre
méthodes d'écriture `assert_not_called`, plus un contrôle statique du module).
Exposer une écriture = un acte explicite, pas un effet de bord de refactor.

**Surface consolidée (ADR 0047 §Amendement, appliqué au connecteur silae le
2026-08-11)** : un tool par OBJET métier, le verbe en paramètre `op` —
`silae_dossier` (list/numbers/info/current_period), `silae_employee`
(list/get/jobs, tous scopés par `numero_dossier`) et `silae_payslip`
(list/header/lines/totals, tous scopés par `numero_dossier` + `periode`).
`silae_variables_to_enter` reste SEUL et **inchangé** : une seule capacité (un
`op` à valeur unique n'est pas un verbe), et c'est la ressource Silae
(`v1/Variables/*`) où vivent TOUTES les écritures non exposées — la garder à
part maintient la frontière lecture/écriture visible.

⚠️ **La rédaction des coordonnées bancaires n'est PAS active par défaut.** Le
masquage IBAN/BIC/RIB est disponible à la frontière des tools
(`FieldRedactionMiddleware`, politique résolue par NAMESPACE `silae` — donc
insensible au nom des tools : ce renommage ne la casse pas), mais
`field_filter_defaults.SERVER_DEFAULTS` est **vide depuis le 2026-06-22** : rien
n'est redacté tant que l'org n'a pas posé de politique (template `bank_details`,
applicable en 1 clic ; `connector_field_schema` déclare le plancher PII silae —
iban/bic/rib/salaire/numeroSecu/dateNaissance/nom/prenom). Les bulletins
arrivent donc **en clair** à l'agent par défaut. *(Ce docstring affirmait
l'inverse jusqu'au 2026-08-11 — « the redaction is applied … server default in
`field_filter_defaults.SERVER_DEFAULTS` » — c'était faux : SERVER_DEFAULTS = {}.)*

⚠️ **L'API Silae ne lève pas : elle renvoie l'erreur dans le corps.**
`SilaeClient.call` rend `{"error": "<status>", "details": …, "status_code": …}`
sur échec HTTP, `{"error": "<exception>"}` sur erreur réseau, et
`{"error": "Max retries exceeded"}` à bout de tentatives — un tool d'ici peut
donc renvoyer un dict d'erreur en HTTP 200. Vérifier la clé `error` avant de
conclure « dossier vide ». (401 = invalidation du token puis retry ; 429 =
backoff exponentiel — gérés dans le client, pas ici.)

⚠️ **La subscription key BORNE le périmètre** : envoyée en
`Ocp-Apim-Subscription-Key`, elle scope les dossiers ET les fonctions
atteignables. Un dossier absent de `silae_dossier(op="list")` n'est pas
forcément inexistant — il peut être hors périmètre de la clé.
"""
from __future__ import annotations

from typing import Literal, Optional

from fastmcp import FastMCP
from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access
from ..connectors import verify as connector_verify


def _verify(fields: dict, config: dict | None = None) -> None:
    """Sonde « tester la connexion » — otomata-tech/oto#69. Couvre `auth` SEUL.

    `POST v1/Dossiers/ListeDossiers` (déjà dans le client — `list_dossiers`),
    le plus petit appel disponible : Silae n'expose ni `/me` ni solde. Le mint
    de token (client_id/client_secret) lève NATURELLEMENT
    (`resp.raise_for_status()`) sur ces deux champs — mais `call()` lui-même NE
    LÈVE JAMAIS sur un refus HTTP (dict `{"error", "status_code"}`, déjà noté
    dans le docstring de ce module) : une `subscription_key` fausse ou trop
    étroite échouerait `list_dossiers()` SANS que le mint de token ne le
    signale, d'où la lecture explicite ci-dessous.

    **Authentifié ≠ utilisable** (classe oto#69) : la `subscription_key` scope
    quels dossiers/fonctions sont joignables — une liste VIDE (`[]`) est un
    état normal (compte tout juste créé), jamais un refus.
    """
    from oto.tools.silae import SilaeClient

    infos = SilaeClient(
        client_id=fields["client_id"], client_secret=fields["client_secret"],
        subscription_key=fields["subscription_key"],
    ).list_dossiers()
    if not (isinstance(infos, dict) and "error" in infos):
        return
    code = infos.get("status_code")
    detail = str(infos.get("details") or infos["error"])[:300]
    if code in (401, 403):
        raise connector_verify.NonAutorise(f"Silae HTTP {code}: {detail}")
    raise RuntimeError(f"Silae: {detail}")


def register(mcp: FastMCP) -> None:
    from oto.tools.silae import SilaeClient

    connector_verify.register("silae", _verify)

    def _client() -> SilaeClient:
        creds = access.resolve_credential_fields("silae")
        # Rédaction (masque IBAN/BIC/RIB) appliquée à la frontière des tools par
        # `FieldRedactionMiddleware` — et SEULEMENT si l'org a posé une politique
        # (cf. l'avertissement du docstring de module) ; plus au niveau client.
        return SilaeClient(
            client_id=creds.get("client_id"),
            client_secret=creds.get("client_secret"),
            subscription_key=creds.get("subscription_key"),
        )

    def _bad(msg: str) -> McpError:
        return McpError(ErrorData(code=INVALID_PARAMS, message=msg))

    def _need(value, name: str, op: str):
        """Argument obligatoire pour CET op — erreur actionnable, jamais de fallback.
        Une chaîne vide compte comme absente : Silae traite `""` comme « tous les
        salariés », donc la laisser passer sur un op mono-salarié rendrait un
        résultat plausible et faux."""
        if value is None or value == "":
            raise _bad(f"op='{op}' requiert {name}")
        return value

    def _refuse_ignored(op: str, hint: str, **provided) -> None:
        """Un argument fourni que CET op n'utilise pas est une erreur d'intention,
        pas un détail. Le silence est le vrai risque de la consolidation par `op` :
        `silae_dossier(numero_dossier="001")` sans op rendrait la liste COMPLÈTE des
        dossiers — un résultat crédible, à côté de la demande. On nomme donc l'op qui
        honore l'argument."""
        for name, value in provided.items():
            if value is not None and value != "":
                raise _bad(f"op='{op}' n'utilise pas {name} — {hint}")

    # --- Dossiers (payroll files) ---

    @mcp.tool()
    def silae_dossier(
        op: Literal["list", "numbers", "info", "current_period"] = "list",
        numero_dossier: Optional[str] = None,
    ) -> object:
        """A payroll dossier (folder) — what the key reaches, or one dossier's payroll.

        `op`:
        - **"list"** (default): the payroll dossiers reachable with the API key.
        - **"numbers"**: just the dossier numbers reachable with the API key —
          lighter than "list" when all you need is a `numero_dossier` to feed the
          other tools.
        - **"info"**: detailed payroll information for a dossier (`numero_dossier`).
        - **"current_period"**: the current OPEN payroll period of a dossier
          (`numero_dossier`) — that's the value to pass as `periode` to
          `silae_payslip`.

        Args:
            op: list (default) | numbers | info | current_period.
            numero_dossier: op="info"/"current_period" — dossier (folder) number.
                Refused on "list"/"numbers", which enumerate everything the
                subscription key reaches and would silently ignore the filter.
        """
        client = _client()

        if op == "list":
            _refuse_ignored(op, "utilise op='info' pour un dossier précis",
                            numero_dossier=numero_dossier)
            return client.list_dossiers()
        if op == "numbers":
            _refuse_ignored(op, "utilise op='info' pour un dossier précis",
                            numero_dossier=numero_dossier)
            return client.list_numeros_dossiers()
        if op == "info":
            return client.dossier_infos(_need(numero_dossier, "numero_dossier", op))
        if op == "current_period":
            return client.dossier_periode_en_cours(
                _need(numero_dossier, "numero_dossier", op))
        raise _bad("op doit être 'list', 'numbers', 'info' ou 'current_period'")

    # --- Salariés (employees) ---

    @mcp.tool()
    def silae_employee(
        numero_dossier: str,
        op: Literal["list", "get", "jobs"] = "list",
        matricule_salarie: Optional[str] = None,
        type_emplois: Optional[int] = None,
    ) -> object:
        """An employee of a dossier — the roster, one employee, or their jobs.

        `op`:
        - **"list"** (default): list the employees of a dossier.
        - **"get"**: fetch one employee by registration number (matricule).
        - **"jobs"**: list an employee's jobs/positions (emplois). Omit
          `matricule_salarie` to get them for every employee of the dossier.

        Args:
            numero_dossier: Dossier (folder) number — required by every op.
            op: list (default) | get | jobs.
            matricule_salarie: Employee registration number. Required by "get";
                optional on "jobs" (omitted = all employees); refused on "list",
                which returns the whole roster and would ignore it.
            type_emplois: op="jobs" — 0 = current jobs only (default),
                1 = current + archived.
        """
        client = _client()

        if op == "list":
            _refuse_ignored(op, "utilise op='get' pour un salarié précis",
                            matricule_salarie=matricule_salarie,
                            type_emplois=type_emplois)
            return client.list_salaries(numero_dossier)
        if op == "get":
            _refuse_ignored(op, "type_emplois ne vaut que pour op='jobs'",
                            type_emplois=type_emplois)
            return client.salarie_matricule(
                numero_dossier, _need(matricule_salarie, "matricule_salarie", op))
        if op == "jobs":
            return client.list_salarie_emplois(
                numero_dossier, matricule_salarie or "", type_emplois or 0)
        raise _bad("op doit être 'list', 'get' ou 'jobs'")

    # --- Bulletins (payslips) ---

    @mcp.tool()
    def silae_payslip(
        numero_dossier: str,
        periode: str,
        op: Literal["list", "header", "lines", "totals"] = "list",
        matricule_salarie: Optional[str] = None,
    ) -> object:
        """A payslip (bulletin) of a period — the payslips, or one payslip's detail.

        `op`:
        - **"list"** (default): retrieve payslips for a period — the whole dossier,
          or one employee when `matricule_salarie` is given.
        - **"header"**: payslip header (entête) for one employee/period.
        - **"lines"**: payslip lines (lignes) for one employee/period.
        - **"totals"**: payslip cumulative totals (cumuls) for one employee/period.

        Args:
            numero_dossier: Dossier (folder) number — required by every op.
            periode: Payroll period (e.g. "2026-05") — required by every op.
                `silae_dossier(op="current_period")` gives the open one.
            op: list (default) | header | lines | totals.
            matricule_salarie: Employee matricule. Optional on "list" (omitted =
                all employees of the dossier); REQUIRED by "header", "lines" and
                "totals", which each describe ONE payslip.
        """
        client = _client()

        if op == "list":
            return client.bulletins(numero_dossier, periode, matricule_salarie or "")
        if op == "header":
            return client.bulletin_entete(
                numero_dossier, _need(matricule_salarie, "matricule_salarie", op),
                periode)
        if op == "lines":
            return client.bulletin_lignes(
                numero_dossier, _need(matricule_salarie, "matricule_salarie", op),
                periode)
        if op == "totals":
            return client.bulletin_cumuls(
                numero_dossier, _need(matricule_salarie, "matricule_salarie", op),
                periode)
        raise _bad("op doit être 'list', 'header', 'lines' ou 'totals'")

    # --- Variables de paie (EVP) ---

    @mcp.tool()
    def silae_variables_to_enter(numero_dossier: str) -> object:
        """List the payroll variables (EVP) still awaiting entry for a dossier.

        Args:
            numero_dossier: Dossier (folder) number.
        """
        return _client().list_variables_a_saisir(numero_dossier)
