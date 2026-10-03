"""`tool_warnings` sur `oto_trigger` — un outil déclaré qui pourrait ne pas
atteindre le run, dit AVANT qu'il tourne à vide.

Né du 16/09/2026 : un déclencheur webhook déclarait deux outils actifs, crédités,
pour son org — et le run ne les a jamais vus, sans un mot (le calcul de visibilité
d'une session hébergée se dérive contre l'org MAISON du délégué, pas celle du
travail ; corrigé séparément, feedback #1021, PAS ce lot-ci). Ce que CE lot ajoute :
un signal, calculé contre l'org du TRAVAIL, jamais stocké, qui ne bloque rien —
même régime que `diagram_warning` (`procedure_diagram.py`).

Trois choses à tenir, chacune son banc :
1. Le calcul est FAIL-SOFT : hors d'un serveur booté (`tool_registry.bound_instance()
   is None` — l'état RÉEL de ces bancs, sans doublure), aucune clé n'apparaît et
   rien ne casse. C'est la preuve que ce lot ne peut pas transformer une pose en
   refus.
2. Un outil `installable`/`not_exposed`/absent du registre est nommé, avec le
   même texte que `oto_list_my_tools` (jamais recopié).
3. Les outils DÉDUITS d'une procédure (`_outils_de_la_procedure`) sont vérifiés
   comme les outils déclarés à la main — un `<tool:…>` retiré depuis doit lever
   `unknown_tool`, pas juste `installed` par défaut.
"""
from __future__ import annotations

import asyncio
import types

import pytest

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

def _ctx(sub="alexis", org_id=77):
    return ResolvedCtx(sub=sub, org_id=org_id)


@pytest.fixture(autouse=True)
def _cle_de_modele_non_exigee(monkeypatch):
    monkeypatch.setattr("oto_mcp.db.connector_settings.get_connector_setting",
                        lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _runner_arme(monkeypatch):
    monkeypatch.setattr(RT.db, "runner_arme",
                        lambda org: {"armed": True, "workers": 1,
                                     "last_seen": "2026-09-16 07:00:00",
                                     "families": ["anthropic"]})
    monkeypatch.setattr(RT.db, "triggers_for_procedure", lambda o, p: [])
    # `get`/`list` passent par `_avec_pertes`, qui lit la vraie base sans cette
    # doublure — ces bancs parlent des outils, pas des occurrences perdues.
    monkeypatch.setattr(RT.db, "comptage_perime", lambda org, tid: {})


# ── 1. fail-soft : l'état RÉEL de ces bancs (aucun serveur booté) ────────────

def test_sans_serveur_boote_aucune_cle_n_apparait_et_rien_ne_casse(monkeypatch):
    """`tool_registry.bound_instance()` rend None hors d'un serveur réellement
    démarré — le cas RÉEL de tout banc de ce fichier tant qu'aucun autre test du
    même process n'a booté de FastMCP. ⚠️ Posé EXPLICITEMENT plutôt que supposé
    de l'état ambiant : `bound_instance` mémorise l'instance au niveau du
    PROCESS (`tool_registry.bind`), et un autre fichier de la suite
    (`test_catalogue_complet_avec_etat.py`) en boote une réellement — ce banc
    tournait seul mais rougissait dans la suite complète tant qu'il lisait
    l'état global au lieu de le poser."""
    monkeypatch.setattr(RT.tool_registry, "bound_instance", lambda: None)
    vu = {}
    monkeypatch.setattr(RT.db, "create_trigger",
                        lambda org, sub, **kw: vu.update(kw, org=org) or {"id": 1, **kw})
    out = asyncio.run(RT._triggers(_ctx(), RT.TriggerInput(
        op="create", procedure="veille", cron="5 6 * * *", model="claude-sonnet-5",
        tools=["linkedin_aiark_search", "lusha_search_and_enrich"])))
    assert "tool_warnings" not in out["trigger"] or out["trigger"]["tool_warnings"] is None
    assert out["trigger"]["id"] == 1


# ── 2. l'état, nommé, avec le texte d'`oto_list_my_tools` ────────────────────

@pytest.fixture()
def _catalogue_simule(monkeypatch):
    """Serveur booté simulé + un catalogue à trois états, aux noms choisis pour
    rappeler l'incident réel : deux outils actifs (le troisième `installable`,
    le quatrième `not_exposed`)."""
    monkeypatch.setattr(RT.tool_registry, "bound_instance", lambda: object())

    async def _faux_catalogue(ctx, sub, prefix, *, org=None):
        return [
            {"name": "linkedin_aiark_search", "state": "installed"},
            {"name": "salesforce_record", "state": "installed"},
            {"name": "lusha_search_and_enrich", "state": "installable"},
            {"name": "kaspr_enrich_linkedin", "state": "not_exposed"},
        ]
    monkeypatch.setattr(RT.tool_catalogue, "catalogue_avec_etat", _faux_catalogue)
    return _faux_catalogue


def test_un_outil_installable_ou_not_exposed_est_nomme_avec_le_texte_du_catalogue(
        monkeypatch, _catalogue_simule):
    monkeypatch.setattr(RT.db, "get_trigger", lambda i, o: {
        "id": 4, "org_id": 77,
        "tools": ["linkedin_aiark_search", "lusha_search_and_enrich",
                 "kaspr_enrich_linkedin"],
    })
    out = asyncio.run(RT._triggers(_ctx(), RT.TriggerInput(op="get", trigger_id=4)))
    avertis = {a["tool"]: a["issue"] for a in out["trigger"]["tool_warnings"]}
    assert avertis == {"lusha_search_and_enrich": "installable",
                       "kaspr_enrich_linkedin": "not_exposed"}
    detail = next(a["detail"] for a in out["trigger"]["tool_warnings"]
                  if a["tool"] == "lusha_search_and_enrich")
    assert detail == RT.tool_catalogue.LEGENDE["installable"]


def test_un_outil_installe_ne_produit_aucun_avertissement(monkeypatch, _catalogue_simule):
    monkeypatch.setattr(RT.db, "get_trigger", lambda i, o: {
        "id": 5, "org_id": 77,
        "tools": ["linkedin_aiark_search", "salesforce_record"],
    })
    out = asyncio.run(RT._triggers(_ctx(), RT.TriggerInput(op="get", trigger_id=5)))
    assert out["trigger"]["tool_warnings"] == []


def test_un_nom_absent_du_registre_leve_unknown_tool(monkeypatch, _catalogue_simule):
    monkeypatch.setattr(RT.db, "get_trigger", lambda i, o: {
        "id": 6, "org_id": 77,
        "tools": ["linkedin_aiark_search", "un_outil_retire_depuis"],
    })
    out = asyncio.run(RT._triggers(_ctx(), RT.TriggerInput(op="get", trigger_id=6)))
    assert out["trigger"]["tool_warnings"] == [
        {"tool": "un_outil_retire_depuis", "issue": "unknown_tool",
         "detail": "ce nom n'existe dans aucun registre — faute de frappe, "
                   "ou outil retiré depuis."}]


# ── 3. les outils DÉDUITS d'une procédure sont vérifiés comme les autres ────

def test_un_outil_deduit_de_la_procedure_et_retire_depuis_leve_unknown_tool(
        monkeypatch, _catalogue_simule):
    """`tools` omis à la pose se DÉDUIT de la procédure (`_outils_de_la_procedure`,
    03/09) — un `<tool:…>` qui ne désigne plus rien doit être vu ici comme il le
    serait si l'appelant l'avait tapé à la main."""
    monkeypatch.setattr(RT, "_outils_de_la_procedure",
                        lambda ctx, slug: ["linkedin_aiark_search", "un_outil_retire_depuis"])
    vu = {}
    monkeypatch.setattr(RT.db, "create_trigger",
                        lambda org, sub, **kw: vu.update(kw, org=org) or {"id": 7, "org_id": org, **kw})
    out = asyncio.run(RT._triggers(_ctx(), RT.TriggerInput(
        op="create", procedure="veille-retiree", cron="5 6 * * *",
        model="claude-sonnet-5")))
    avertis = {a["tool"]: a["issue"] for a in out["trigger"]["tool_warnings"]}
    assert avertis == {"un_outil_retire_depuis": "unknown_tool"}
