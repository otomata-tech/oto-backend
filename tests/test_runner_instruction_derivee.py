"""L'instruction de départ se compose côté PLATEFORME — jamais dans le worker.

Le worker est un client MCP : il exécute une instruction, il ne sait pas ce
qu'elle contient et n'a rien à y ajouter. Tant qu'il portait une instruction par
défaut (`DEFAULT_INPUT`, retiré du runner), il inventait le travail à la place
de qui l'avait déclaré, depuis le seul étage qui ne connaît pas le métier :
personne ne pouvait la relire ni la corriger depuis le produit.

Ces bancs tiennent les trois conséquences :

1. **Déclarer suffit.** Une campagne créée sans instruction en reçoit une, qui
   POINTE la procédure et la file — jamais un travail rédigé à la main.
2. **Ce qui est écrit est servi.** Composer ne se substitue à personne : une
   instruction fournie passe intacte.
3. **On n'arme pas une campagne muette.** Le refus du worker est correct mais
   il arrive trop tard et trop loin : la flotte resterait `armed` sans avancer,
   et le symptôme lu depuis le produit serait « l'ordonnanceur est mort ».
"""
from __future__ import annotations

import pathlib

import pytest

from oto_mcp.capabilities import _instruction
from oto_mcp.capabilities import runner_fleets as RF
from oto_mcp.capabilities import runner_triggers as RT
from oto_mcp.capabilities._types import ResolvedCtx


@pytest.fixture(autouse=True)
def _compte_beta(monkeypatch):
    monkeypatch.setattr(RF.access, "has_option", lambda sub, o, *, org=None: True)


def _ctx():
    return ResolvedCtx(sub="alexis", org_id=2)


# ── déclarer suffit ───────────────────────────────────────────────────────────

def test_une_campagne_sans_instruction_en_recoit_une_qui_pointe_sa_file(monkeypatch):
    vus = {}
    monkeypatch.setattr(RF.db, "create_fleet",
                        lambda *a, **kw: vus.update(kw) or {"id": 1})
    RF._fleets(_ctx(), RF.FleetInput(
        op="create", label="essai", procedure="enrichissement", tools=["data_write"],
        namespace="edition-vivier", row_filter={"statut": "a_enrichir"}))
    servie = vus["input"]
    assert "`enrichissement`" in servie          # l'objet qui fait autorité
    assert "`edition-vivier`" in servie          # la file, nommée
    assert "a_enrichir" in servie                # et son périmètre
    assert "data_claim_next" in servie           # la mécanique, qui est à nous


def test_sans_cible_declaree_aucune_file_n_est_inventee():
    """Une flotte sans tableau ne reçoit pas un protocole de réservation qui
    désignerait une file imaginaire — l'instruction reste nue."""
    nue = _instruction.de_file("veille", None, None)
    assert "`veille`" in nue
    assert "data_claim_next" not in nue and "tableau" not in nue


def test_un_declencheur_sans_instruction_pointe_sa_procedure(monkeypatch):
    vus = {}
    monkeypatch.setattr(RT.db, "create_trigger", lambda *a, **kw: vus.update(kw) or {"id": 7})
    monkeypatch.setattr(RT.db, "runner_arme", lambda org: {"armed": True, "workers": 1})
    monkeypatch.setattr(RT.db, "triggers_for_procedure", lambda org, p: [])
    monkeypatch.setattr(RT, "_outils_de_la_procedure", lambda ctx, slug: ["oto_kb"])
    RT._triggers(_ctx(), RT.TriggerInput(
        op="create", procedure="veille-hebdo", cron="0 8 * * 1"))
    assert "`veille-hebdo`" in vus["input"]


# ── ce qui est écrit est servi ────────────────────────────────────────────────

def test_une_instruction_fournie_passe_intacte(monkeypatch):
    vus = {}
    monkeypatch.setattr(RF.db, "create_fleet",
                        lambda *a, **kw: vus.update(kw) or {"id": 1})
    ecrite = "Traite la file de droite à gauche et ne conclus rien."
    RF._fleets(_ctx(), RF.FleetInput(
        op="create", label="essai", procedure="p", tools=["data_write"],
        namespace="t", input=ecrite))
    assert vus["input"] == ecrite


# ── on n'arme pas une campagne muette ─────────────────────────────────────────

def _launch_avec(monkeypatch, flotte):
    """Joue `launch` sur `flotte` en enregistrant l'ORDRE des gestes de base."""
    from oto_mcp import roles
    monkeypatch.setattr(roles, "is_org_admin", lambda sub, org: True)
    monkeypatch.setattr(RF, "_run_courant", lambda: None)
    trace = []

    def _update(fid, org, champs):
        trace.append(("update", champs.get("input")))
        return flotte

    def _armer(fid, org, **kw):
        # `**kw` et non une signature figée : `db.armer` a gagné `rows_at_launch`
        # (oto-backend#836, le dénominateur d'un passage). Un stub qui épingle la
        # signature exacte du seam qu'il remplace rougit à chaque paramètre ajouté
        # ailleurs — et ce banc-ci ne parle pas de l'armement, il parle de l'ORDRE
        # (réparer l'instruction AVANT d'armer). Il ne doit tomber que si cet ordre
        # change.
        trace.append(("armer", kw.get("rows_at_launch")))
        return dict(flotte, status="armed")

    monkeypatch.setattr(RF.db, "get_fleet", lambda fid, org: flotte)
    monkeypatch.setattr(RF.db, "update_fleet", _update)
    monkeypatch.setattr(RF.db, "armer", _armer)
    RF._fleets(_ctx(), RF.FleetInput(op="launch", fleet_id=flotte["id"]))
    return trace


def test_une_campagne_muette_recoit_son_instruction_avant_d_etre_armee(monkeypatch):
    trace = _launch_avec(monkeypatch, {
        "id": 12, "input": None, "procedure": "enrichissement",
        "namespace": "edition-vivier", "row_filter": None, "status": "draft"})
    gestes = [g for g, _ in trace]
    assert gestes == ["update", "armer"], (
        "l'instruction se pose AVANT l'armement : entre les deux, un ordonnanceur "
        "peut prendre la campagne")
    assert "`enrichissement`" in trace[0][1]


def test_une_campagne_qui_parle_deja_n_est_pas_reecrite(monkeypatch):
    trace = _launch_avec(monkeypatch, {
        "id": 13, "input": "la mienne", "procedure": "p", "namespace": "t",
        "row_filter": None, "status": "draft"})
    assert [g for g, _ in trace] == ["armer"]


# ── un seul domicile, pour toute la classe ────────────────────────────────────

def test_aucune_autre_capacite_ne_redige_sa_propre_instruction():
    """Deux surfaces déclarent un agent (flotte, déclencheur) ; une troisième
    viendra. Si chacune rédige sa variante, la même règle vit à plusieurs
    endroits et l'une d'elles finit par mentir — c'est le défaut que ce module
    existe pour fermer, et il se garde par CLASSE, pas fichier par fichier."""
    paquet = pathlib.Path(_instruction.__file__).parent
    coupables = [
        f.name for f in paquet.glob("*.py")
        if f.name != "_instruction.py" and "Lis la procédure" in f.read_text()]
    assert not coupables, (
        f"{coupables} rédige(nt) une instruction au lieu de la dériver de "
        "`_instruction`")
