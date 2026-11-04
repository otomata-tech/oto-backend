"""Un travail hébergé ne lance pas un agent plus outillé que lui (`chain_widens_tools`, #1499).

La liste d'outils d'un travail est tenue par le serveur sur chaque appel
(`outils_du_travail`) — mais `oto_trigger op=run_now` lançait un AUTRE agent, qui tournait
avec SA liste. Un travail dont la liste porte `oto_trigger` sortait donc de la sienne en
une marche : il lançait un agent qui avait les outils qui lui manquaient. Sous un jeton de
délégation, la liste SERVIE de l'enfant (`outils_du_travail.outils_servis`, au canonique,
outils de l'hôte hors du calcul) doit tenir dans celle du parent ; sinon refus nommé, rien
n'est enfilé.

Sur une vraie base (`live`) : le travail parent est ENFILÉ, son jeton émis par le vrai
chemin du runner (`runner_jobs._delegue`), le verrou posé depuis la ligne que rend la vraie
vérification du jeton, l'agent enfant créé en base, et `run_now` joué par la capacité.
Seules les gardes d'exécution sans rapport (runner armé, clé de modèle, abonnement) sont
doublées : elles ont leurs bancs.
"""
from __future__ import annotations

import asyncio
import datetime
import uuid

import pytest

SUB = "sub-chaine-outils"
PARENT = ["acme_people", "oto_trigger", "zoho_connect"]


@pytest.fixture(scope="module")
def org(live):
    from oto_mcp import db
    from oto_mcp.org_store import members, orgs
    db.upsert_user(SUB, email=f"{SUB}@acme.test", name=SUB)
    o = orgs.create_org(f"Acme chaine {uuid.uuid4().hex[:4]}", created_by=SUB)
    members.add_org_member(o, SUB, "org_admin")
    return o


@pytest.fixture(autouse=True)
def _gardes_d_execution(monkeypatch):
    """Ce que `run_now` exige pour qu'un worker PRENNE le travail — hors sujet ici."""
    from oto_mcp.capabilities import runner_triggers as RT
    monkeypatch.setattr(RT.db, "runner_arme",
                        lambda org: {"armed": True, "workers": 1,
                                     "last_seen": "2026-10-21 07:00:00",
                                     "families": ["anthropic"]})
    monkeypatch.setattr(RT._cle_exigee, "exiger_a_la_pose", lambda *a, **k: None)
    monkeypatch.setattr(RT.runner_jobs._abonnement, "exiger_a_la_pose",
                        lambda *a, **k: None)
    monkeypatch.setattr(RT.procedure_retiree, "archivee", lambda *a: None)


@pytest.fixture
def depuis_le_travail(org):
    """Pose, pour la durée du test, le verrou du jeton d'un travail parent dont la liste
    est `PARENT` (et sa procédure, d'où `oto_procedure`)."""
    from oto_mcp import db, verrou_org
    from oto_mcp.capabilities import runner_jobs
    job = db.enqueue_job(org, "start", sub=SUB, payload={
        "tools": list(PARENT), "procedure": "p", "input": "x"})
    jeton = runner_jobs._delegue({"id": job["id"], "sub": SUB, "org_id": org},
                                 bail_s=60, claimant="test")["delegated_token"]
    t = verrou_org.poser(verrou_org.depuis_ligne(db.verify_api_token(jeton)))
    yield job["id"]
    verrou_org.lever(t)


def _agent(org: int, outils: list) -> int:
    from oto_mcp import db
    t = db.create_trigger(org, SUB, procedure=f"enfant-{uuid.uuid4().hex[:6]}",
                          cron="5 6 * * *", tz="UTC",
                          next_due=datetime.datetime.now(datetime.timezone.utc),
                          tools=outils, model="claude-sonnet-5")
    return int(t["id"])


def _run_now(org: int, trigger_id: int) -> dict:
    from oto_mcp.capabilities import runner_triggers as RT
    from oto_mcp.capabilities._types import ResolvedCtx
    return asyncio.run(RT._triggers(ResolvedCtx(sub=SUB, org_id=org),
                                    RT.TriggerInput(op="run_now", trigger_id=trigger_id)))


def _en_file(org: int, trigger_id: int) -> int:
    from oto_mcp import db
    return db.file_du_declencheur(trigger_id, org)["pending"]


def test_un_enfant_plus_outille_est_refuse_en_nommant_ses_outils_en_trop(
        org, depuis_le_travail):
    from oto_mcp.capabilities._types import AuthzDenied
    enfant = _agent(org, ["acme_people", "acme_siret", "acme_mail"])
    with pytest.raises(AuthzDenied) as e:
        _run_now(org, enfant)
    assert (e.value.status, e.value.code) == (403, "chain_widens_tools")
    assert "acme_siret" in e.value.message and "acme_mail" in e.value.message
    assert "acme_people" not in e.value.message
    assert _en_file(org, enfant) == 0, "rien n'est enfilé"


def test_un_enfant_dont_la_liste_tient_dans_celle_du_parent_part(org, depuis_le_travail):
    enfant = _agent(org, ["acme_people"])
    out = _run_now(org, enfant)
    assert out["ok"] and out["job"]["chain"]["parent_job"] == depuis_le_travail
    assert _en_file(org, enfant) == 1


def test_les_outils_de_l_hote_sont_hors_du_calcul(org, depuis_le_travail):
    enfant = _agent(org, ["acme_people", "run_start", "run_finish"])
    assert _run_now(org, enfant)["ok"]


def test_la_comparaison_se_fait_au_canonique(org, depuis_le_travail):
    """L'ancien nom d'un outil que le parent porte sous son nom canonique n'élargit rien."""
    enfant = _agent(org, ["oto_zoho_connect"])
    assert _run_now(org, enfant)["ok"]


def test_une_personne_lance_l_agent_qu_elle_veut(org):
    """Hors jeton de délégation, rien ne change : le bouton de l'écran lance l'agent."""
    enfant = _agent(org, ["acme_people", "acme_siret"])
    out = _run_now(org, enfant)
    assert out["ok"] and "chain" not in out["job"]


def test_un_parent_introuvable_ne_permet_que_l_hote(org):
    """Le parent se lit en base, jamais dans l'appel : introuvable, il ne permet que les
    outils de l'hôte (fail-closed, comme à l'appel)."""
    from oto_mcp import outils_du_travail as odt
    from oto_mcp import verrou_org
    t = verrou_org.poser(verrou_org.Verrou(sub=SUB, org_id=org, job_id=10 ** 12))
    try:
        assert odt.en_trop({"tools": ["acme_people", "run_start"]}) == ["acme_people"]
    finally:
        verrou_org.lever(t)


def test_hors_travail_heberge_rien_n_est_calcule(org):
    from oto_mcp import outils_du_travail as odt
    assert odt.en_trop({"tools": ["acme_people"]}) is None


# ── L'autre chemin qui lance depuis un travail : armer une campagne ─────────────

def _campagne(org: int, outils: list) -> int:
    from oto_mcp import db
    f = db.create_fleet(org, SUB, label=f"c-{uuid.uuid4().hex[:6]}", procedure="p",
                        tools=outils, model="claude-sonnet-5", provider="anthropic")
    return int(f["id"])


def _armer(org: int, fleet_id: int) -> dict:
    from oto_mcp.capabilities import runner_fleets as RF
    from oto_mcp.capabilities._types import ResolvedCtx
    return RF._fleets(ResolvedCtx(sub=SUB, org_id=org),
                      RF.FleetInput(op="launch", fleet_id=fleet_id))


def test_armer_une_campagne_plus_outillee_est_refuse(org, depuis_le_travail):
    from oto_mcp import db
    from oto_mcp.capabilities._types import AuthzDenied
    f = _campagne(org, ["acme_people", "acme_siret"])
    with pytest.raises(AuthzDenied) as e:
        _armer(org, f)
    assert (e.value.status, e.value.code) == (403, "chain_widens_tools")
    assert "acme_siret" in e.value.message
    assert db.get_fleet(f, org)["status"] == "draft", "rien n'est armé"


def test_armer_une_campagne_qui_tient_dans_la_liste_passe_la_garde(org, depuis_le_travail):
    """La garde laisse passer ; ce qui suit (outils montés dans l'org…) a ses bancs."""
    from oto_mcp.capabilities._types import AuthzDenied
    f = _campagne(org, ["acme_people"])
    try:
        _armer(org, f)
    except AuthzDenied as e:
        assert e.code != "chain_widens_tools"
