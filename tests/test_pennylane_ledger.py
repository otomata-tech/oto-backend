"""Grand livre côté outils — oto-backend#872, pièces A à D.

Les épreuves visent ce qui casse en silence côté agent : un `op` routé vers la
mauvaise méthode, un argument obligatoire avalé, et surtout le référentiel des
journaux — sans lui, aucune écriture comptable n'est possible plus tard, puisque
`journal_id` est requis et propre à la société.
"""
import asyncio
from unittest.mock import MagicMock

import pytest
from oto_mcp.mcp_errors import McpError


@pytest.fixture
def client(monkeypatch):
    """`_client()` importe `PennylaneClient` depuis le PACKAGE à chaque appel :
    c'est l'attribut du package qu'on remplace."""
    import oto.tools.pennylane as pkg

    inst = MagicMock()
    monkeypatch.setattr(pkg, "PennylaneClient", lambda **kw: inst)
    monkeypatch.setattr("oto_mcp.access.resolve_api_key", lambda *a, **k: ("k", False))
    return inst


def _tool(nom: str, module: str = "pennylane_ledger"):
    from fastmcp import FastMCP
    import importlib

    m = FastMCP("t")
    importlib.import_module(f"oto_mcp.tools.{module}").register(m)
    return asyncio.run(m.get_tool(nom)).fn


# --- routage ---------------------------------------------------------------

def test_list_passe_les_clauses_au_client(client):
    clauses = [{"field": "date", "operator": "gteq", "value": "2026-01-01"}]
    _tool("pennylane_ledger_entry")(op="list", clauses=clauses, max_pages=2)
    client.get_ledger_entries.assert_called_once_with(max_pages=2, clauses=clauses)


def test_get_lit_une_ecriture_par_son_id(client):
    _tool("pennylane_ledger_entry")(op="get", entry_id=42)
    client.get_ledger_entry.assert_called_once_with(42)


def test_lines_lit_les_lignes_de_l_ecriture(client):
    _tool("pennylane_ledger_entry")(op="lines", entry_id=42)
    client.get_ledger_entry_lines.assert_called_once_with(42, max_pages=None)


def test_lettered_part_d_une_LIGNE_et_non_d_une_ecriture(client):
    """Le piège du domaine : `lettered` prend l'id d'une LIGNE. Passer un id
    d'écriture rendrait le lettrage d'une autre ligne, sans erreur."""
    _tool("pennylane_ledger_entry")(op="lettered", line_id=7)
    client.get_lettered_lines.assert_called_once_with(7, max_pages=None)
    client.get_ledger_entry_lines.assert_not_called()


# --- arguments obligatoires ------------------------------------------------

@pytest.mark.parametrize("op,manquant", [("get", "entry_id"), ("lines", "entry_id"),
                                         ("lettered", "line_id")])
def test_un_op_sans_son_id_est_refuse_avant_tout_appel(client, op, manquant):
    with pytest.raises(McpError, match=f"op='{op}' requiert {manquant}"):
        _tool("pennylane_ledger_entry")(op=op)
    assert not client.method_calls, "rien ne doit partir sans son identifiant"


def test_un_op_inconnu_nomme_les_op_valides(client):
    with pytest.raises(McpError, match="'list'.*'get'.*'lines'.*'lettered'"):
        _tool("pennylane_ledger_entry")(op="nope")


def test_le_defaut_est_une_lecture_de_liste(client):
    _tool("pennylane_ledger_entry")()
    client.get_ledger_entries.assert_called_once()


# --- le référentiel des journaux -------------------------------------------

def test_pennylane_ref_sert_les_journaux(client):
    """Prérequis de toute écriture comptable : `journal_id` est requis et propre
    à la société. Sans ce référentiel, la pièce C est inutilisable."""
    _tool("pennylane_ref", module="pennylane")(kind="journals")
    client.get_journals.assert_called_once_with(max_pages=None)


def test_pennylane_ref_nomme_les_journaux_parmi_les_kinds_valides(client):
    with pytest.raises(McpError, match="journals"):
        _tool("pennylane_ref", module="pennylane")(kind="nope")


# --- écriture comptable : le geste sans brouillon --------------------------

def test_create_passe_les_champs_requis_et_les_lignes(client):
    lignes = [{"debit": "120.00", "credit": "0", "ledger_account_id": 11},
              {"debit": "0", "credit": "120.00", "ledger_account_id": 22}]
    _tool("pennylane_ledger_entry")(op="create", date="2026-09-04", label="OD",
                                    journal_id=5, lines=lignes)
    client.create_ledger_entry.assert_called_once_with(
        date="2026-09-04", label="OD", journal_id=5, ledger_entry_lines=lignes,
        due_date=None, currency=None, piece_number=None)


@pytest.mark.parametrize("manquant,kwargs", [
    ("date", {"label": "OD", "journal_id": 5, "lines": [{}]}),
    ("label", {"date": "2026-09-04", "journal_id": 5, "lines": [{}]}),
    ("journal_id", {"date": "2026-09-04", "label": "OD", "lines": [{}]}),
    ("lines", {"date": "2026-09-04", "label": "OD", "journal_id": 5}),
])
def test_create_refuse_un_champ_requis_manquant_avant_tout_appel(client, manquant, kwargs):
    with pytest.raises(McpError, match=f"op='create' requiert {manquant}"):
        _tool("pennylane_ledger_entry")(op="create", **kwargs)
    client.create_ledger_entry.assert_not_called()


def test_un_refus_de_creation_leve_au_lieu_de_remonter_en_valeur(client):
    """Le geste le plus engageant du connecteur : un refus lu comme un succès
    laisserait croire qu'une écriture est passée."""
    from oto.tools.common.errors import UpstreamHTTPError

    client.create_ledger_entry.side_effect = UpstreamHTTPError(
        422, "Entry lines are not balanced", service="pennylane")
    with pytest.raises(McpError, match="Entry lines are not balanced"):
        _tool("pennylane_ledger_entry")(op="create", date="2026-09-04", label="OD",
                                        journal_id=5, lines=[{}])


def test_la_description_dit_qu_il_n_y_a_pas_de_brouillon(client):
    """Le reste du connecteur est brouillon-d'abord ; ici non. L'agent lit la
    description à chaque appel — c'est le seul endroit où cette asymétrie peut
    l'arrêter avant qu'il ne pose l'écriture."""
    from fastmcp import FastMCP
    import importlib

    m = FastMCP("t")
    importlib.import_module("oto_mcp.tools.pennylane_ledger").register(m)
    doc = asyncio.run(m.get_tool("pennylane_ledger_entry")).description or ""
    assert "brouillon" in doc.lower(), doc
    assert "update" in doc, "le seul recours doit être nommé"


# --- lettrage de lignes ----------------------------------------------------

def test_set_lettre_les_lignes(client):
    _tool("pennylane_ledger_lettering")(op="set", line_ids=[1, 2])
    client.letter_ledger_entry_lines.assert_called_once_with([1, 2], "none")


def test_unset_defait_le_lettrage(client):
    _tool("pennylane_ledger_lettering")(op="unset", line_ids=[1, 2])
    client.unletter_ledger_entry_lines.assert_called_once_with([1, 2], "none")


def test_le_defaut_refuse_un_lettrage_desequilibre(client):
    """Un défaut permissif passerait inaperçu."""
    _tool("pennylane_ledger_lettering")(op="set", line_ids=[1, 2])
    assert client.letter_ledger_entry_lines.call_args[0][1] == "none"


def test_un_op_de_lettrage_inconnu_est_refuse(client):
    with pytest.raises(McpError, match="'set'.*'unset'"):
        _tool("pennylane_ledger_lettering")(op="nope", line_ids=[1, 2])
    client.letter_ledger_entry_lines.assert_not_called()


def test_les_deux_lettrages_se_designent_l_un_l_autre():
    """Le mot « lettrage » recouvre deux gestes sur deux objets. La confusion a
    déjà coûté une conclusion fausse : chaque description doit nommer l'autre
    outil, sinon l'agent choisit au hasard sans jamais voir d'erreur."""
    from fastmcp import FastMCP
    import importlib

    m = FastMCP("t")
    for mod in ("pennylane", "pennylane_ledger"):
        importlib.import_module(f"oto_mcp.tools.{mod}").register(m)
    match = asyncio.run(m.get_tool("pennylane_match")).description or ""
    lettrage = asyncio.run(m.get_tool("pennylane_ledger_lettering")).description or ""
    assert "pennylane_ledger_lettering" in match, match
    assert "pennylane_match" in lettrage, lettrage
