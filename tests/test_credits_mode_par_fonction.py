"""Le mode PAR FONCTION du barème (`OTO_CREDITS_MODE_FUNCTIONS`, #1376 E) et le plancher
d'un travail (`OTO_CREDITS_JOB_FLOOR`, #1376 B).

Ce que ces bancs tiennent :

1. **une fonction nommée prend son mode, les autres gardent celui de l'instance** —
   `anthropic.messages:*=enforce` refuse les appels au modèle pendant que les outils
   restent en `report` ;
2. **un réglage illisible refuse le démarrage**, en nommant l'élément — une fonction que
   le barème ne sait pas tarifer comprise ; un nom exact l'emporte toujours sur un motif ;
3. **le plancher ne mord qu'en `enforce`**, sur le disponible (solde − réservations
   ouvertes) du payeur du travail ; `report` journalise, `off` et 0 ne lisent rien ; un
   solde illisible est `spend_ledger_unavailable`, rejouable.
"""
from __future__ import annotations

import pytest

from oto_mcp.access import credits, credits_modes as modes, credits_tours as tours
from oto_mcp.db import credits as db_credits


@pytest.fixture(autouse=True)
def _propre(monkeypatch):
    for v in ("OTO_CREDITS_MODE", "OTO_CREDITS_MODE_FUNCTIONS", "OTO_CREDITS_JOB_FLOOR"):
        monkeypatch.delenv(v, raising=False)


# ── 1–2. le mode par fonction ───────────────────────────────────────────────

def test_sans_reglage_chaque_fonction_a_le_mode_de_l_instance(monkeypatch):
    monkeypatch.setenv("OTO_CREDITS_MODE", "report")
    assert modes.mode_de("anthropic", "messages:claude-sonnet-5") == "report"
    assert modes.mode_de("fullenrich", "submit") == "report"


def test_les_appels_au_modele_en_enforce_les_outils_en_report(monkeypatch):
    monkeypatch.setenv("OTO_CREDITS_MODE", "report")
    monkeypatch.setenv("OTO_CREDITS_MODE_FUNCTIONS", "anthropic.messages:*=enforce")
    assert modes.mode_de("anthropic", "messages:claude-opus-5-5") == "enforce"
    assert modes.mode_de("anthropic", "messages:") == "enforce"
    assert modes.mode_de("fullenrich", "submit") == "report"
    assert modes.mode_de("anthropic", "batches:x") == "report", "un autre préfixe"
    assert modes.mode_de("anthropic", None) == "report", "sans fonction : l'instance"


def test_le_nom_le_plus_long_gagne(monkeypatch):
    monkeypatch.setenv("OTO_CREDITS_MODE_FUNCTIONS",
                       "anthropic.messages:*=enforce, anthropic.messages:claude-haiku-4-5=off")
    assert modes.mode_de("anthropic", "messages:claude-haiku-4-5") == "off"
    assert modes.mode_de("anthropic", "messages:claude-sonnet-5") == "enforce"


def test_un_nom_sans_etoile_est_exact(monkeypatch):
    monkeypatch.setenv("OTO_CREDITS_MODE_FUNCTIONS", "fullenrich.submit=enforce")
    assert modes.mode_de("fullenrich", "submit") == "enforce"
    assert modes.mode_de("fullenrich", "submit2") == "report"


def test_le_tour_lit_le_mode_de_son_modele(monkeypatch):
    monkeypatch.setenv("OTO_CREDITS_MODE", "off")
    monkeypatch.setenv("OTO_CREDITS_MODE_FUNCTIONS", "anthropic.messages:*=enforce")
    assert tours.mode_du_modele("claude-sonnet-5-20261001") == "enforce"
    assert tours.mode_du_modele(None) == "enforce", "un travail sans modèle : `messages:`"


@pytest.mark.parametrize("valeur", ["anthropic.messages:*", "anthropic=enforce",
                                    ".messages:*=enforce", "anthropic.messages:*=strict"])
def test_un_reglage_illisible_refuse_le_demarrage(monkeypatch, valeur):
    monkeypatch.setenv("OTO_CREDITS_MODE_FUNCTIONS", valeur)
    with pytest.raises(ValueError, match="OTO_CREDITS_MODE_FUNCTIONS"):
        credits.verifier_reglages()


def test_une_entree_exacte_l_emporte_toujours_sur_un_motif(monkeypatch):
    """L'étoile ne compte pas dans la longueur : `messages:claude-haiku-4-5*` est plus
    long que le nom exact, et ne l'emporte pas pour autant."""
    monkeypatch.setenv("OTO_CREDITS_MODE_FUNCTIONS",
                       "anthropic.messages:claude-haiku-4-5*=enforce, "
                       "anthropic.messages:claude-haiku-4-5=off")
    assert modes.mode_de("anthropic", "messages:claude-haiku-4-5") == "off"
    assert modes.mode_de("anthropic", "messages:claude-haiku-4-5-bis") == "enforce"


def test_entre_deux_motifs_le_prefixe_le_plus_long_gagne(monkeypatch):
    monkeypatch.setenv("OTO_CREDITS_MODE_FUNCTIONS",
                       "anthropic.messages:*=enforce, anthropic.messages:claude-opus*=off")
    assert modes.mode_de("anthropic", "messages:claude-opus-5") == "off"
    assert modes.mode_de("anthropic", "messages:claude-sonnet-5") == "enforce"


@pytest.mark.parametrize("valeur", [
    "anthropic.messages:*=enforce",
    "anthropic.messages:claude-haiku-4-5=off",
    "anthropic.messages:claude-opus*=enforce",
    "fullenrich.submit=enforce",
    "fullenrich.sub*=off",
])
def test_une_fonction_du_bareme_se_pose(monkeypatch, valeur):
    monkeypatch.setenv("OTO_CREDITS_MODE_FUNCTIONS", valeur)
    credits.verifier_reglages()


@pytest.mark.parametrize("valeur,nomme", [
    ("antropic.messages:*=enforce", "unknown connector `antropic`"),
    ("anthropic.message:*=enforce", "names no priced function"),
    ("anthropic.messages:claude-sonet-5=enforce", "names no priced function"),
    ("anthropic.messages:claude-sonnet-5-20261001=enforce", "names no priced function"),
    ("fullenrich.submitt=enforce", "names no priced function"),
    ("fullenrich.x*=enforce", "names no priced function"),
])
def test_une_fonction_absente_du_bareme_refuse_le_demarrage(monkeypatch, valeur, nomme):
    """Jamais un retour SILENCIEUX au mode de l'instance : une faute de frappe laisserait
    la fonction dans le mode global sans que personne le sache."""
    monkeypatch.setenv("OTO_CREDITS_MODE_FUNCTIONS", valeur)
    with pytest.raises(ValueError, match="OTO_CREDITS_MODE_FUNCTIONS") as e:
        credits.verifier_reglages()
    assert nomme in str(e.value)


def test_une_fonction_nommee_deux_fois_refuse_le_demarrage(monkeypatch):
    monkeypatch.setenv("OTO_CREDITS_MODE_FUNCTIONS",
                       "anthropic.messages:*=enforce, anthropic.messages:*=off")
    with pytest.raises(ValueError, match="named twice"):
        credits.verifier_reglages()


@pytest.mark.parametrize("instance,fonctions,averti", [
    ("off", "", False),
    ("off", "anthropic.messages:*=enforce", True),
    ("off", "anthropic.messages:*=report", True),
    ("off", "anthropic.messages:*=off", False),
    ("report", "", True),
])
def test_la_borne_d_outil_desarmee_se_dit_des_qu_une_fonction_reserve(
        monkeypatch, caplog, instance, fonctions, averti):
    """Une instance `off` dont une fonction est en `enforce` réserve : la garde de
    démarrage doit le voir, pas seulement le mode de l'instance."""
    import types
    from oto_mcp import bornes_processus
    monkeypatch.setenv("OTO_CREDITS_MODE", instance)
    monkeypatch.setenv("OTO_CREDITS_MODE_FUNCTIONS", fonctions)
    monkeypatch.setattr(bornes_processus, "reglages",
                        lambda: types.SimpleNamespace(outil_s=0))
    with caplog.at_level("WARNING", logger="oto_mcp.access.credits"):
        credits.verifier_reglages()
    assert ("OTO_BORNE_OUTIL_S=0" in caplog.text) is averti


@pytest.mark.parametrize("valeur", ["-1", "beaucoup", "1.5"])
def test_un_plancher_illisible_refuse_le_demarrage(monkeypatch, valeur):
    monkeypatch.setenv("OTO_CREDITS_JOB_FLOOR", valeur)
    with pytest.raises(ValueError, match="OTO_CREDITS_JOB_FLOOR"):
        credits.verifier_reglages()


# ── 3. le plancher ──────────────────────────────────────────────────────────

@pytest.fixture
def solde(monkeypatch):
    vu = {"solde": {"balance": 1000, "holds_open": 0}, "lu": []}
    monkeypatch.setattr(db_credits, "version_du_bareme", lambda: 1)
    monkeypatch.setattr(db_credits, "lignes_du_bareme", lambda v: [])
    monkeypatch.setattr(db_credits, "orgs_en_mode_personne", lambda: set())
    monkeypatch.setattr(db_credits, "solde",
                        lambda payer: vu["lu"].append(payer) or vu["solde"])
    credits.oublier()
    yield vu
    credits.oublier()


def test_sans_plancher_rien_n_est_lu(monkeypatch, solde):
    monkeypatch.setenv("OTO_CREDITS_MODE", "enforce")
    assert tours.sous_le_plancher("alexis", 42, "claude-sonnet-5") is None
    assert solde["lu"] == []


def test_au_dessus_du_plancher_le_travail_part(monkeypatch, solde):
    monkeypatch.setenv("OTO_CREDITS_MODE", "enforce")
    monkeypatch.setenv("OTO_CREDITS_JOB_FLOOR", "500")
    assert tours.sous_le_plancher("alexis", 42, "claude-sonnet-5") is None
    assert solde["lu"] == ["org:42"], "le payeur du travail : son org"


def test_les_reservations_ouvertes_ne_comptent_pas_au_disponible(monkeypatch, solde):
    monkeypatch.setenv("OTO_CREDITS_MODE", "enforce")
    monkeypatch.setenv("OTO_CREDITS_JOB_FLOOR", "500")
    solde["solde"] = {"balance": 1000, "holds_open": 600}
    assert tours.sous_le_plancher("alexis", 42, "claude-sonnet-5") == \
        {"code": "insufficient_credits", "needed": 500, "available": 400}


def test_en_report_le_plancher_journalise_sans_refuser(monkeypatch, solde):
    monkeypatch.setenv("OTO_CREDITS_MODE", "report")
    monkeypatch.setenv("OTO_CREDITS_JOB_FLOOR", "500")
    solde["solde"] = {"balance": 0, "holds_open": 0}
    assert tours.sous_le_plancher("alexis", 42, "claude-sonnet-5") is None
    assert solde["lu"] == ["org:42"]


def test_le_plancher_suit_le_mode_de_la_fonction_du_modele(monkeypatch, solde):
    monkeypatch.setenv("OTO_CREDITS_MODE", "report")
    monkeypatch.setenv("OTO_CREDITS_MODE_FUNCTIONS", "anthropic.messages:*=enforce")
    monkeypatch.setenv("OTO_CREDITS_JOB_FLOOR", "500")
    solde["solde"] = {"balance": 10, "holds_open": 0}
    assert tours.sous_le_plancher("alexis", 42, "claude-sonnet-5") == \
        {"code": "insufficient_credits", "needed": 500, "available": 10}


def test_en_off_le_plancher_ne_lit_rien(monkeypatch, solde):
    monkeypatch.setenv("OTO_CREDITS_MODE", "off")
    monkeypatch.setenv("OTO_CREDITS_JOB_FLOOR", "500")
    assert tours.sous_le_plancher("alexis", 42, "claude-sonnet-5") is None
    assert solde["lu"] == []


def test_un_solde_illisible_est_un_refus_REJOUABLE_en_enforce_seulement(monkeypatch, solde):
    monkeypatch.setattr(db_credits, "solde", lambda payer: 1 / 0)
    monkeypatch.setenv("OTO_CREDITS_JOB_FLOOR", "500")
    monkeypatch.setenv("OTO_CREDITS_MODE", "enforce")
    assert tours.sous_le_plancher("alexis", 42, "claude-sonnet-5") == \
        {"code": "spend_ledger_unavailable", "needed": 500, "available": None}, \
        "illisible n'est pas insuffisant : rejouable"
    monkeypatch.setenv("OTO_CREDITS_MODE", "report")
    assert tours.sous_le_plancher("alexis", 42, "claude-sonnet-5") is None
