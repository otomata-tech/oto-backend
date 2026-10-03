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

import asyncio

import pathlib

import pytest

from oto_mcp.capabilities import _instruction
from oto_mcp.capabilities import runner_fleets as RF
from oto_mcp.capabilities import runner_triggers as RT
from oto_mcp.capabilities._types import ResolvedCtx



@pytest.fixture(autouse=True)
def _l_appelant_possede_l_agent(monkeypatch):
    """Ce fichier ne parle pas du PARTAGE d'agents — il a son banc
    (`test_partage_agents.py`). L'appelant y est lu propriétaire de tout agent
    qu'on lui présente ; sans cette doublure, la lecture de rôle irait chercher la
    vraie base."""
    from oto_mcp.capabilities import _acces_agent
    monkeypatch.setattr(_acces_agent, "niveaux",
                        lambda sub, org_id, agents: {int(t["id"]): "owner" for t in agents})

@pytest.fixture(autouse=True)
def _cle_de_modele_non_exigee(monkeypatch):
    """Ce fichier ne parle pas de la garde de clé de modèle — elle a son propre banc
    (`test_cle_de_modele_exigee.py`). Le réglage est lu ÉTEINT, comme sur toute
    plateforme qui ne l'a pas allumé : sans cette doublure, la lecture irait
    chercher la vraie base et chaque banc tomberait sur une raison qui n'est pas
    la sienne."""
    monkeypatch.setattr("oto_mcp.db.connector_settings.get_connector_setting",
                        lambda *a, **k: None)



def _ctx():
    return ResolvedCtx(sub="alexis", org_id=2)


# ── déclarer suffit ───────────────────────────────────────────────────────────

def test_une_campagne_sans_instruction_en_recoit_une_qui_pointe_sa_file(monkeypatch):
    vus = {}
    monkeypatch.setattr(RF.db, "create_fleet",
                        lambda *a, **kw: vus.update(kw) or {"id": 1})
    monkeypatch.setattr(RF._lignes_reservables, "cle_a_la_declaration",
                        lambda adresse, *, sub, org_id: 77)
    RF._fleets(_ctx(), RF.FleetInput(
        op="create", model="claude-sonnet-5", label="essai", procedure="enrichissement", tools=["data_write"],
        namespace="edition-vivier", row_filter={"statut": "a_enrichir"}))
    servie = vus["input"]
    assert "`enrichissement`" in servie          # l'objet qui fait autorité
    assert "`77`" in servie                      # la file, par sa clé (#1067)
    assert "edition-vivier" not in servie
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
    monkeypatch.setattr(RT.db, "runner_arme", lambda org: {"armed": True, "workers": 1, "families": ["anthropic"]})
    monkeypatch.setattr(RT.db, "triggers_for_procedure", lambda org, p: [])
    monkeypatch.setattr(RT, "_outils_de_la_procedure", lambda ctx, slug: ["oto_kb"])
    asyncio.run(RT._triggers(_ctx(), RT.TriggerInput(
        op="create", model="claude-sonnet-5", procedure="veille-hebdo", cron="0 8 * * 1")))
    assert "`veille-hebdo`" in vus["input"]


# ── ce qui est écrit est servi ────────────────────────────────────────────────

def test_une_instruction_fournie_passe_intacte(monkeypatch):
    vus = {}
    monkeypatch.setattr(RF.db, "create_fleet",
                        lambda *a, **kw: vus.update(kw) or {"id": 1})
    monkeypatch.setattr(RF._lignes_reservables, "cle_a_la_declaration",
                        lambda adresse, *, sub, org_id: 77)
    ecrite = "Traite la file de droite à gauche et ne conclus rien."
    RF._fleets(_ctx(), RF.FleetInput(
        op="create", model="claude-sonnet-5", label="essai", procedure="p", tools=["data_write"],
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
        # `**kw` et non une signature figée : celle de `db.armer` a déjà bougé deux
        # fois (`rows_at_launch` ajouté par oto-backend#836, retiré le 13/09/2026). Un
        # stub qui épingle la signature exacte du seam qu'il remplace rougit à chaque
        # paramètre qui change ailleurs — et ce banc-ci ne parle pas de l'armement, il
        # parle de l'ORDRE (réparer l'instruction AVANT d'armer). Il ne doit tomber que
        # si cet ordre change.
        trace.append(("armer", kw))
        return dict(flotte, status="armed")

    flotte = {"model": "claude-sonnet-5", **flotte}
    monkeypatch.setattr(RF.db, "get_fleet", lambda fid, org: flotte)
    monkeypatch.setattr(RF.db, "update_fleet", _update)
    monkeypatch.setattr(RF.db, "armer", _armer)
    monkeypatch.setattr(RF.db, "runner_arme", lambda org: {
        "armed": True, "workers": 1, "last_seen": None, "families": ["anthropic"]})
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

# ── 4. le TICK : ce qui part vraiment ────────────────────────────────────────
#
# ⚠️ Constaté en production le 14/09/2026. `create` compose bien l'instruction —
# mais c'est le SEUL site qui le fasse, et il n'existe que depuis le 02/09. Les
# six déclencheurs de l'org 196 sont nés avant : ils portent `input = NULL`, et
# CHACUNE de leurs occurrences partait donc sans instruction de départ, pour se
# faire refuser par le worker (« il exécute une instruction, il n'en compose
# pas »). Un agent actif, un runner vivant, un modèle servi — et rien qui marche,
# sans que rien ne le dise.
#
# Aucune mise à jour ne répare la colonne : réparer AU TICK couvre d'un coup les
# rangs anciens et le jour où quelqu'un vide l'invite depuis le produit.

def _tick_avec(monkeypatch, **declencheur):
    from oto_mcp import runner_tick

    enfile = {}
    t = {"id": 5, "org_id": 2, "cron": "5 6 * * *", "tz": "Europe/Paris",
         "next_due": "2026-09-12 04:05:00", "procedure": "veille-du-matin",
         "project_id": None, "tools": ["data_write"], "input": None,
         "label": None, "max_steps": None, "model": None, **declencheur}
    monkeypatch.setattr(runner_tick.db, "due_triggers", lambda limit=50: [t])
    monkeypatch.setattr(runner_tick.db, "consume_due", lambda i, vu, p: True)
    monkeypatch.setattr(runner_tick.db, "perimer_travaux_du_declencheur",
                        lambda t, o: 0)
    monkeypatch.setattr(runner_tick.db, "enqueue_job",
                        lambda org, kind, payload=None, **_: enfile.update(payload))
    assert runner_tick._tick() == 1
    return enfile


def test_un_declencheur_sans_instruction_en_recoit_une_au_tick(monkeypatch):
    charge = _tick_avec(monkeypatch, input=None)
    assert charge.get("input"), (
        "sans instruction le worker REFUSE le travail : un déclencheur né avant "
        "que `create` en compose une échouerait à chacune de ses occurrences")
    assert "veille-du-matin" in charge["input"], (
        "l'instruction dérivée POINTE la procédure — elle ne la réécrit pas")


def test_une_instruction_ecrite_part_intacte(monkeypatch):
    charge = _tick_avec(monkeypatch, input="Ne regarde que les appels d'hier.")
    assert charge["input"] == "Ne regarde que les appels d'hier.", (
        "composer ne se substitue à personne : ce qui est écrit est servi")


def test_une_invite_videe_depuis_le_produit_ne_casse_pas_l_agent(monkeypatch):
    # Le produit efface l'invite par une chaîne VIDE (un NULL serait jeté par
    # `update_trigger`). Sans la dérivation, ce geste rendait l'agent muet.
    charge = _tick_avec(monkeypatch, input="")
    assert charge.get("input"), "une invite vidée retombe sur l'instruction dérivée"


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
