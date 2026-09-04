"""Grand livre Pennylane — lire les écritures, en poser, lettrer des lignes.

Second module du connecteur `pennylane` (cf. `Connector.modules` au registre) :
même clé, même client, domaine distinct. Les journaux, eux, sont un référentiel
et se lisent par `pennylane_ref(kind="journals")`, avec les autres.

⚠️ **Ce n'est pas la GED.** Le connecteur `pennylaneged` vise la même entreprise
par une autre porte (API privée de l'interface, session navigateur), et ses
`company_id` ne sont PAS ceux d'ici. Un id pris dans l'un et joué dans l'autre
rend un refus qui imite une session expirée.

⚠️ **Trois scopes, pas un.** Pennylane a éclaté l'ancien scope `ledger` : lire
les écritures demande `ledger_entries:*`, les journaux `journals:*`, le plan
comptable `ledger_accounts:*`. Une clé qui lit l'un ne lit pas forcément les
autres, et le périmètre est propre à qui a posé la clé. Les droits réels se
lisent avec `pennylane_ref(kind="company")`, champ `scopes`.
"""
from __future__ import annotations

from typing import Literal, Optional

from fastmcp import FastMCP

from .pennylane_socle import _bad, _client, _ecrit, _need


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    def pennylane_ledger_entry(
        op: Literal["list", "get", "lines", "lettered", "create",
                    "update"] = "list",
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
    ) -> dict | list:
        """Écritures du grand livre : lire, poser une écriture, la corriger.

        ⚠️ **`op="create"` n'a PAS de brouillon, et le geste est irréversible.**
        Partout ailleurs dans ce connecteur, une écriture engageante se pose en
        brouillon puis se finalise dans un second geste, après validation
        humaine. Pennylane n'offre pas ce palier pour une écriture comptable :
        elle est posée immédiatement, et l'API ne sait pas la supprimer — le
        seul recours est `op="update"`, qui peut lui-même détruire des lignes.
        **Annoncer à l'utilisateur le détail exact — journal, date, libellé, et
        chaque ligne avec son compte et son montant — et attendre son accord
        AVANT d'appeler.**

        ⚠️ **`op="list"` sans `clauses` remonte TOUT l'historique** — sur une
        comptabilité réelle, des milliers d'écritures, bien au-delà de la limite
        de tokens. Filtrer à la source est le seul moyen de retrouver une
        écriture ; `max_pages` borne les dégâts mais ne cible rien.

        Args:
            op: "list" — les écritures, à filtrer avec `clauses` ;
                "get" — UNE écriture par son `entry_id` ;
                "lines" — les lignes d'une écriture, avec leur `id` (c'est cet
                    id que consomme le lettrage, pas celui de l'écriture) ;
                "lettered" — les lignes lettrées AVEC la ligne `line_id`, pour
                    constater ce qu'un lettrage a réellement associé.
            entry_id: id de l'écriture — requis par "get" et "lines".
            line_id: id d'une LIGNE d'écriture — requis par "lettered".
            clauses: filtre serveur, liste de `{"field", "operator", "value"}`.
                Champs filtrables : `id`, `date`, `journal_id`. Opérateurs :
                `lt`, `lteq`, `gt`, `gteq`, `eq`, `not_eq`, plus `in` et
                `not_in` sur `id` et `journal_id`. Exemple :
                `[{"field": "date", "operator": "gteq", "value": "2026-01-01"}]`.
            max_pages: borne le nombre de pages ramenées.
            date: op="create" — date de l'écriture (YYYY-MM-DD).
            label: op="create" — libellé de l'écriture.
            journal_id: op="create" — le journal où poser l'écriture. Se résout
                avec `pennylane_ref(kind="journals")` : ces ids sont propres à
                la société, jamais à coder en dur.
            lines: op="create" — les lignes, 1 à 1000. Chacune `{"debit": "…",
                "credit": "…", "ledger_account_id": …}` et un `label` optionnel.
                Les montants sont des CHAÎNES décimales ("120.50"), et les
                débits doivent égaler les crédits — sinon l'appel est refusé
                avant d'atteindre Pennylane, avec l'écart chiffré. Les comptes
                se résolvent avec `pennylane_ref(kind="ledger_accounts")`.
            due_date / currency / piece_number: op="create", optionnels
                (devise EUR par défaut, numéro de pièce auto-généré).
            fields: op="update" — les champs à modifier sur l'écriture.
                ⚠️ `ledger_entry_lines` y prend `create`/`update`/`delete` : ce
                geste peut SUPPRIMER des lignes : il engage autant qu'une
                création, et s'annonce de la même façon.
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
            return _ecrit(c.create_ledger_entry(
                date=_need(date, "date", op), label=_need(label, "label", op),
                journal_id=_need(journal_id, "journal_id", op),
                ledger_entry_lines=_need(lines, "lines", op),
                due_date=due_date, currency=currency, piece_number=piece_number),
                "la création d'écriture comptable")
        if op == "update":
            return _ecrit(c.update_ledger_entry(_need(entry_id, "entry_id", op),
                                                **(fields or {})),
                          "la correction d'écriture comptable")
        raise _bad("op doit être 'list', 'get', 'lines', 'lettered', 'create' "
                   "ou 'update'")

    @mcp.tool()
    def pennylane_ledger_lettering(
        op: Literal["set", "unset"],
        line_ids: list[int],
        unbalanced_lettering_strategy: Literal["none", "partial"] = "none",
    ) -> dict:
        """Lettre des LIGNES du grand livre entre elles, ou défait ce lettrage.

        ⚠️ **Ce n'est pas `pennylane_match`.** Le mot « lettrage » recouvre deux
        gestes sur deux objets : rapprocher une transaction bancaire d'une
        facture, c'est `pennylane_match` ; associer entre elles des lignes
        d'écriture au grand livre, c'est ici. Se tromper d'outil ne produit pas
        d'erreur, seulement un geste posé au mauvais endroit.

        ⚠️ **Le lettrage est ABSORBANT** : si une ligne passée est déjà lettrée,
        le lettrage s'étend à celles qui lui sont déjà associées. Demander
        [A, C] quand A et B sont lettrées produit [A, B, C]. Pour constater ce
        qui a réellement été associé, relire avec
        `pennylane_ledger_entry(op="lettered", line_id=…)`.

        Le geste est réversible (`op="unset"`), ce qui le distingue d'une
        écriture comptable.

        Args:
            op: "set" pour lettrer, "unset" pour défaire.
            line_ids: au moins deux ids de LIGNES d'écriture — pas des ids
                d'écritures. Ils se lisent avec
                `pennylane_ledger_entry(op="lines", entry_id=…)`.
            unbalanced_lettering_strategy: "none" refuse un lettrage
                déséquilibré (défaut), "partial" l'accepte.
        """
        c = _client()
        if op == "set":
            return _ecrit(
                c.letter_ledger_entry_lines(line_ids, unbalanced_lettering_strategy),
                "le lettrage de lignes du grand livre")
        if op == "unset":
            return _ecrit(
                c.unletter_ledger_entry_lines(line_ids, unbalanced_lettering_strategy),
                "le délettrage de lignes du grand livre")
        raise _bad("op doit être 'set' ou 'unset'")
