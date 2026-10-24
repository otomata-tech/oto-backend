"""`runner.triggers op=list` en une passe (oto-backend#1148).

`POST /api/me/runner/triggers` : médiane 3,6 s, p95 11,9 s en production les
03-04/10/2026. L'écran des automatisations la lit à chaque ouverture. Chaque ligne de
la liste payait :

- son catalogue d'outils (`_avec_tool_warnings` → `catalogue_avec_etat`), recalculé
  par déclencheur alors qu'il ne dépend que de (porteur, org) — une douzaine de
  lectures et ~12 ms de boucle pour ~870 outils, PAR LIGNE ;
- ses pertes (`comptage_perime`) et, pour un webhook, ses livraisons et sa file
  (`comptage_livraisons`, `file_du_declencheur`) — un à trois emprunts au pool, dont un
  parcours complet de `runner_jobs` par webhook (aucun index ne sert `held`).

Mesuré sur une base locale, 20 agents dont 10 webhooks, 100 000 travaux : 302
emprunts et 551 ms avant, 18 emprunts et 43 ms après. Ce banc garde les deux
invariants : un catalogue par org et par réponse, un nombre de lectures qui ne
grandit pas avec la liste.
"""
from __future__ import annotations

import asyncio

import pytest

from oto_mcp.capabilities import runner_triggers as RT
from oto_mcp.capabilities._types import ResolvedCtx

# Ce banc double la base des agents et ne parle pas de leur partage (conftest).
pytestmark = pytest.mark.usefixtures("agents_sans_partages")

ORG = 8148


def _ctx():
    return ResolvedCtx(sub="proprio", org_id=ORG)


def _agents(n_programmes: int, n_webhooks: int) -> list[dict]:
    return ([{"id": i, "org_id": ORG, "sub": "proprio", "kind": "schedule",
              "tools": ["data_write"]} for i in range(1, n_programmes + 1)]
            + [{"id": 100 + i, "org_id": ORG, "sub": "proprio", "kind": "webhook",
                "tools": ["data_write", "oto_doc"]} for i in range(1, n_webhooks + 1)])


@pytest.fixture
def _sans_base(monkeypatch):
    """Tout ce que la liste lit hors de son sujet, posé ; les lectures UNITAIRES lèvent :
    la liste ne doit plus passer par elles."""
    monkeypatch.setattr(RT.db, "runner_arme",
                        lambda org: {"armed": True, "workers": 1, "last_seen": None,
                                     "families": []})
    for unitaire in ("comptage_perime", "comptage_livraisons", "file_du_declencheur"):
        def _interdit(*a, _nom=unitaire, **k):
            raise AssertionError(f"`{_nom}` appelé ligne par ligne dans une liste")
        monkeypatch.setattr(RT.db, unitaire, _interdit)
    lus: dict[str, list] = {"perimes": [], "livraisons": [], "files": []}
    monkeypatch.setattr(RT.db, "comptages_perimes", lambda org, ids: lus["perimes"].append(
        list(ids)) or {i: {"expired_count": i, "expired_since": None, "expired_last": None}
                       for i in ids})
    monkeypatch.setattr(RT.db, "comptages_livraisons", lambda org, ids: lus["livraisons"].append(
        list(ids)) or {i: {"recues_24h": 2, "refusees_24h": 1, "derniere": None} for i in ids})
    monkeypatch.setattr(RT.db, "files_des_declencheurs", lambda org, ids: lus["files"].append(
        list(ids)) or {i: {"pending": 3, "held": 0} for i in ids})
    return lus


def test_la_liste_lit_chaque_mesure_une_fois_pour_toutes_ses_lignes(monkeypatch, _sans_base):
    monkeypatch.setattr(RT.tool_registry, "bound_instance", lambda: None)
    monkeypatch.setattr(RT.db, "list_triggers", lambda org: _agents(3, 2))
    out = asyncio.run(RT._triggers(_ctx(), RT.TriggerInput(op="list")))
    assert _sans_base == {"perimes": [[1, 2, 3, 101, 102]],
                          "livraisons": [[101, 102]], "files": [[101, 102]]}, (
        "une requête par mesure pour toute la liste ; les comptes d'un webhook ne se "
        "lisent que pour les webhooks")
    par_id = {t["id"]: t for t in out["triggers"]}
    assert [t["id"] for t in out["triggers"]] == [1, 2, 3, 101, 102], "l'ordre est gardé"
    assert par_id[2]["expired_count"] == 2 and "hook_url" not in par_id[2]
    w = par_id[101]
    assert (w["expired_count"], w["deliveries_24h"], w["deliveries_refused_24h"],
            w["queue_pending"], w["queue_held"]) == (101, 2, 1, 3, 0)
    assert w["my_access"] == "owner", "l'accès se pose toujours sur la ligne servie"


def test_un_catalogue_par_org_pour_toute_la_reponse(monkeypatch, _sans_base):
    monkeypatch.setattr(RT.tool_registry, "bound_instance", lambda: object())
    calculs = []

    async def _catalogue(ctx, sub, prefix, *, org=None):
        calculs.append(org)
        return [{"name": "data_write", "state": "installed"},
                {"name": "oto_doc", "state": "installable"}]
    monkeypatch.setattr(RT.tool_catalogue, "catalogue_avec_etat", _catalogue)
    monkeypatch.setattr(RT.db, "list_triggers", lambda org: _agents(6, 6))
    out = asyncio.run(RT._triggers(_ctx(), RT.TriggerInput(op="list")))
    assert calculs == [ORG], f"un calcul par org, pas par ligne : {calculs}"
    avertis = {t["id"]: [a["tool"] for a in t["tool_warnings"]] for t in out["triggers"]}
    assert avertis[1] == [] and avertis[101] == ["oto_doc"], (
        "le catalogue partagé rend à chaque ligne SES avertissements")


def test_un_catalogue_illisible_ne_casse_pas_la_liste_et_n_est_pas_retente(
        monkeypatch, _sans_base):
    monkeypatch.setattr(RT.tool_registry, "bound_instance", lambda: object())
    essais = []

    async def _panne(ctx, sub, prefix, *, org=None):
        essais.append(org)
        raise RuntimeError("couches illisibles")
    monkeypatch.setattr(RT.tool_catalogue, "catalogue_avec_etat", _panne)
    monkeypatch.setattr(RT.db, "list_triggers", lambda org: _agents(4, 0))
    out = asyncio.run(RT._triggers(_ctx(), RT.TriggerInput(op="list")))
    assert len(essais) == 1, "l'échec est mémorisé pour la réponse, pas retenté par ligne"
    assert all("tool_warnings" not in t for t in out["triggers"]), "fail-soft inchangé"


# ── contre la base : le nombre de lectures ne grandit pas avec la liste ────────

def _lectures_d_une_liste(monkeypatch, org: int) -> int:
    from oto_mcp.db import _conn as dbconn
    lectures = {"n": 0}
    monkeypatch.setattr(dbconn._hors_boucle, "verifier",
                        lambda: lectures.__setitem__("n", lectures["n"] + 1))
    monkeypatch.setattr(RT.tool_registry, "bound_instance", lambda: None)
    RT._triggers_sync(ResolvedCtx(sub="proprio", org_id=org), RT.TriggerInput(op="list"))
    return lectures["n"]


def test_les_lectures_d_une_liste_ne_dependent_pas_de_sa_longueur(live, monkeypatch):
    from oto_mcp import db
    monkeypatch.setattr("oto_mcp.db.connector_settings.get_connector_setting",
                        lambda *a, **k: None)
    for org, n in ((ORG, 2), (ORG + 1, 12)):
        for i in range(n):
            db.create_trigger(org, "proprio", procedure=f"p{i}", tz="UTC",
                              tools=["data_write"],
                              kind="webhook" if i % 2 else "schedule",
                              cron=None if i % 2 else "5 6 * * *",
                              next_due=None if i % 2 else "2030-01-01 00:00:00")
    courte, longue = (_lectures_d_une_liste(monkeypatch, ORG),
                      _lectures_d_une_liste(monkeypatch, ORG + 1))
    assert courte == longue, (
        f"2 agents : {courte} lectures, 12 agents : {longue} — une lecture par ligne "
        "est revenue dans la liste")


def test_les_mesures_groupees_comptent_chaque_declencheur_a_part(live):
    """Les trois lectures groupées rendent, par déclencheur, ce que rendaient les
    lectures unitaires : bornées à l'org, `0` pour qui n'a rien, chaque id présent."""
    from oto_mcp import db
    from oto_mcp.db import _conn as dbconn
    a = db.create_trigger(ORG + 2, "proprio", procedure="a", tz="UTC", tools=["x"],
                          kind="webhook")["id"]
    b = db.create_trigger(ORG + 2, "proprio", procedure="b", tz="UTC", tools=["x"],
                          kind="webhook")["id"]
    with dbconn._connect() as c:
        for tid, statut, n in ((a, "pending", 2), (a, "held", 1), (a, "expired", 3),
                               (b, "pending", 1), (b, "done", 4)):
            for _ in range(n):
                c.execute("INSERT INTO runner_jobs (org_id, kind, status, payload) "
                          "VALUES (%s, 'start', %s, jsonb_build_object('trigger_id', %s::text))",
                          (ORG + 2, statut, str(tid)))
        # Même déclencheur vu depuis une AUTRE org : ne compte pas.
        c.execute("INSERT INTO runner_jobs (org_id, kind, status, payload) "
                  "VALUES (%s, 'start', 'expired', jsonb_build_object('trigger_id', %s::text))",
                  (ORG + 3, str(a)))
        for tid, issue in ((a, "queued"), (a, "refused_rate"), (b, "queued")):
            c.execute("INSERT INTO runner_hook_deliveries (trigger_id, org_id, outcome) "
                      "VALUES (%s, %s, %s)", (tid, ORG + 2, issue))
    absent = b + 1000
    files = db.files_des_declencheurs(ORG + 2, [a, b, absent])
    assert files == {a: {"pending": 2, "held": 1}, b: {"pending": 1, "held": 0},
                     absent: {"pending": 0, "held": 0}}
    pertes = db.comptages_perimes(ORG + 2, [a, b])
    assert (pertes[a]["expired_count"], pertes[b]["expired_count"]) == (3, 0)
    assert pertes[b]["expired_since"] is None and pertes[a]["expired_since"] is not None
    livraisons = db.comptages_livraisons(ORG + 2, [a, b])
    assert ((livraisons[a]["recues_24h"], livraisons[a]["refusees_24h"]),
            (livraisons[b]["recues_24h"], livraisons[b]["refusees_24h"])) == ((2, 1), (1, 0))
    # Les lectures unitaires sont la même requête, pour un seul id.
    assert db.file_du_declencheur(a, ORG + 2) == files[a]
    assert db.comptage_perime(ORG + 2, a) == pertes[a]
    assert db.comptage_livraisons(a, ORG + 2) == livraisons[a]
    assert db.comptages_perimes(ORG + 2, []) == {}, "une liste vide ne lit rien"


def test_les_outils_non_accordes_lisent_chaque_procedure_une_fois(live, monkeypatch):
    """`missing_tools` lit la procédure d'un agent (et ses recettes) : une fois par
    procédure pour toute la réponse, jamais une fois par agent qui la porte."""
    from oto_mcp import db
    from oto_mcp.db import _conn as dbconn
    monkeypatch.setattr("oto_mcp.db.connector_settings.get_connector_setting",
                        lambda *a, **k: None)
    monkeypatch.setattr(RT.tool_registry, "bound_instance", lambda: None)
    for org, n in ((ORG + 4, 2), (ORG + 5, 12)):
        db.set_guide_db("org", str(org), "veille", "# Veille\n\n<tool:fr_siret>")
        for _ in range(n):
            db.create_trigger(org, "proprio", procedure="veille", tz="UTC",
                              tools=["data_write"], cron="5 6 * * *",
                              next_due="2030-01-01 00:00:00")

    def _lectures(org: int) -> tuple[int, list]:
        ctx = ResolvedCtx(sub="proprio", org_id=org)
        rep = RT._triggers_sync(ctx, RT.TriggerInput(op="list"))
        avant = compteur["n"]
        out = asyncio.run(RT._ajouter_tool_warnings(ctx, rep))
        return compteur["n"] - avant, [t.get("missing_tools") for t in out["triggers"]]

    compteur = {"n": 0}
    monkeypatch.setattr(dbconn._hors_boucle, "verifier",
                        lambda: compteur.__setitem__("n", compteur["n"] + 1))

    (courte, m2), (longue, m12) = _lectures(ORG + 4), _lectures(ORG + 5)
    assert m2 == [["fr_siret"]] * 2 and m12 == [["fr_siret"]] * 12
    assert courte == longue, (
        f"2 agents : {courte} lectures, 12 agents : {longue} — la procédure se relit par agent")
