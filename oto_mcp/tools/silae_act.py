"""Silae — EVERY write, in one tool: `silae_act` (24/11/2026).

`tools/silae.py` and `tools/silae_documents.py` only read, and stay that way (their
static check in `tests/test_silae_op_dispatch.py` still holds): the writes live in their
OWN tool, because an MCP client authorizes TOOLS, not ops — "always allow" on
`silae_payslip` must never enter a bonus (`lecture.ACTE`, convention `<ns>_act` of
17/10/2026, same shape as `payfit_act`).

What it serves is exactly the four entry functions `SilaeClient` already carries
(`oto.tools.silae._api.saisies`), nothing more — no payslip computation, no payslip
deletion, no DSN, no pay-cycle change (those are not even in the client):

- `add_variable_element` → `ElementsVariables/SalarieAjouterElementVariable`: a variable
  pay element for one employee and one month (its code must exist in a bonus profile the
  computation uses, or the next computation drops it);
- `add_bonus` → `ElementsVariables/SalarieAjouterPrime`: a bonus (its code must exist);
- `add_hours` → `ActivitesEtHeures/SalarieAjouterHeures`: hours of one type for the month,
  ADDED to the existing value (`ajouter=True`) or REPLACING it (`ajouter=False`);
- `set_entries_confirmed` → `ElementsVariables/SalariesConfirmerSaisies`: confirms the
  month's entered hours and/or bonuses for the whole dossier.

⚠️ **Three gates, none of them ours to bypass**:
- Silae's own: these functions are reserved to the "Usage interne RH" contract (model 1B)
  and to what the subscription key opens (403 otherwise, named by `silae._upstream_message`);
  an entry on a payslip already computed is refused until that payslip is deleted.
- The org's or team's access level (`providers/silae.ACCESS_LEVELS`): `read` — the DEFAULT,
  so every key installed while Silae was read-only keeps reading — serves none of these
  ops; an org admin sets `full` to open them.
- The MCP client's confirmation (`ACTE`).

`dry_run=True` runs every check and returns the call it WOULD make, without calling Silae:
what an agent shows the collaborator before the real call.

Never exercised against a real Silae dossier: check a first entry on a test dossier.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from fastmcp import FastMCP

from .lecture import ACTE
from .silae import _bad, _client, check_op, need, only, run

_OPS = ("add_variable_element", "add_bonus", "add_hours", "set_entries_confirmed")

#: op → (client method, the optional arguments this op uses besides its required ones)
_EMPLOYEE = ("numero_dossier", "matricule_salarie", "periode", "code")
_USES: dict[str, tuple[str, ...]] = {
    "add_variable_element": _EMPLOYEE + ("montant", "valeur_chaine", "controle_existence"),
    "add_bonus": _EMPLOYEE + ("montant", "valeur_chaine", "prime_nette", "intitule_bulletin",
                              "intitule_court"),
    "add_hours": _EMPLOYEE + ("nombre", "ajouter"),
    "set_entries_confirmed": ("numero_dossier", "periode", "confirmer_heures",
                              "confirmer_primes"),
}


def _one_value(op: str, montant: Optional[float], valeur_chaine: Optional[str]) -> None:
    if (montant is None) == (valeur_chaine is None or valeur_chaine == ""):
        raise _bad(f"op='{op}' takes exactly one of montant (number) or valeur_chaine (text)")


def register(mcp: FastMCP) -> None:

    @mcp.tool(annotations=ACTE)
    def silae_act(
        op: Literal["add_variable_element", "add_bonus", "add_hours", "set_entries_confirmed"],
        numero_dossier: str,
        periode: str,
        matricule_salarie: Optional[str] = None,
        code: Optional[str] = None,
        montant: Optional[float] = None,
        valeur_chaine: Optional[str] = None,
        controle_existence: Optional[bool] = None,
        prime_nette: Optional[bool] = None,
        intitule_bulletin: Optional[str] = None,
        intitule_court: Optional[str] = None,
        nombre: Optional[float] = None,
        ajouter: Optional[bool] = None,
        confirmer_heures: Optional[bool] = None,
        confirmer_primes: Optional[bool] = None,
        dry_run: bool = False,
    ) -> Any:
        """⚠️ WRITES into Silae payroll entries — what the next payslip computation uses.
        Only after a person has validated the entry. Reading is `silae_dossier`,
        `silae_employee`, `silae_payslip`, `silae_variables_to_enter` (the codes a
        dossier accepts).

        `op`:
        - "add_variable_element" (`matricule_salarie`, `code`, `montant` OR
          `valeur_chaine`; optional `controle_existence`): a variable pay element for one
          employee and one month. The code must exist in a bonus profile the computation
          uses (see `silae_variables_to_enter`).
        - "add_bonus" (`matricule_salarie`, `code`, `montant` OR `valeur_chaine`; optional
          `prime_nette`, `intitule_bulletin` ≤ 45 chars, `intitule_court` ≤ 10): a bonus.
        - "add_hours" (`matricule_salarie`, `code`, `nombre`, `ajouter`): hours of type
          `code` for the month. `ajouter=True` ADDS to the existing value, `False`
          REPLACES it — always say which.
        - "set_entries_confirmed" (`confirmer_heures`, `confirmer_primes`): confirms the
          month's entered hours and/or bonuses for the whole dossier.

        Silae refuses an entry on a payslip already computed (delete it first, in Silae),
        and these functions need the "Usage interne RH" contract (1B). The org's access
        level must be `full`: at `read` (the default) this tool is not served.

        Args:
            op: the write to make.
            numero_dossier: Dossier (folder) number.
            periode: The month, "YYYY-MM".
            matricule_salarie: The employee's matricule (every op but
                "set_entries_confirmed").
            code: The variable element, bonus or hours code.
            montant: A numeric value (add_variable_element / add_bonus).
            valeur_chaine: A text value instead of `montant`.
            controle_existence: add_variable_element — let Silae check the code exists.
            prime_nette: add_bonus — the bonus is net.
            intitule_bulletin: add_bonus — the label printed on the payslip (≤ 45).
            intitule_court: add_bonus — short label (≤ 10).
            nombre: add_hours — the number of hours.
            ajouter: add_hours — True adds to the month's value, False replaces it.
            confirmer_heures: set_entries_confirmed — confirm the hours.
            confirmer_primes: set_entries_confirmed — confirm the bonuses.
            dry_run: Check everything and return the call it would make, without
                calling Silae.
        """
        check_op(op, _OPS)
        args = {"matricule_salarie": matricule_salarie, "code": code, "montant": montant,
                "valeur_chaine": valeur_chaine, "controle_existence": controle_existence,
                "prime_nette": prime_nette, "intitule_bulletin": intitule_bulletin,
                "intitule_court": intitule_court, "nombre": nombre, "ajouter": ajouter,
                "confirmer_heures": confirmer_heures, "confirmer_primes": confirmer_primes}
        only(op, _USES[op], **args)
        need(op, numero_dossier=numero_dossier, periode=periode)

        if op == "set_entries_confirmed":
            need(op, confirmer_heures=confirmer_heures, confirmer_primes=confirmer_primes)
            if not (confirmer_heures or confirmer_primes):
                raise _bad("op='set_entries_confirmed': confirm the hours, the bonuses or both")
            method = "confirmer_saisies"
            call_args: tuple = (numero_dossier,)
            kwargs: dict = {"periode": periode, "confirmer_heures": confirmer_heures,
                            "confirmer_primes": confirmer_primes}
        else:
            need(op, matricule_salarie=matricule_salarie, code=code)
            call_args = (numero_dossier, matricule_salarie)
            kwargs = {"periode": periode, "code": code}
            if op == "add_hours":
                need(op, nombre=nombre, ajouter=ajouter)
                method = "ajouter_heures"
                kwargs.update(nombre=nombre, ajouter=ajouter)
            else:
                _one_value(op, montant, valeur_chaine)
                kwargs.update(montant=montant, valeur_chaine=valeur_chaine or None)
                if op == "add_variable_element":
                    method = "ajouter_element_variable"
                    if controle_existence is not None:
                        kwargs["controle_existence"] = controle_existence
                else:
                    if intitule_bulletin is not None and len(intitule_bulletin) > 45:
                        raise _bad("op='add_bonus': intitule_bulletin is 45 characters at most")
                    if intitule_court is not None and len(intitule_court) > 10:
                        raise _bad("op='add_bonus': intitule_court is 10 characters at most")
                    method = "ajouter_prime"
                    for k, v in (("prime_nette", prime_nette),
                                 ("intitule_bulletin", intitule_bulletin),
                                 ("intitule_court", intitule_court)):
                        if v is not None:
                            kwargs[k] = v

        if dry_run:
            return {"dry_run": True, "op": op, "would_call": method,
                    "numero_dossier": numero_dossier,
                    **({"matricule_salarie": matricule_salarie} if matricule_salarie else {}),
                    "arguments": kwargs}
        c = _client()
        # one explicit call per write, so the version-skew probe sees each of them
        writes = {
            "ajouter_element_variable": lambda: c.ajouter_element_variable(*call_args, **kwargs),
            "ajouter_prime": lambda: c.ajouter_prime(*call_args, **kwargs),
            "ajouter_heures": lambda: c.ajouter_heures(*call_args, **kwargs),
            "confirmer_saisies": lambda: c.confirmer_saisies(*call_args, **kwargs),
        }
        return run(writes[method])
