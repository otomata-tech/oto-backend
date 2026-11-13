"""Un travail hébergé ne passe la main à plus outillé que lui que par un lien déclaré, ne
crée pas de campagne plus outillée que lui, n'arme aucune campagne, et ne prête jamais le
rôle plateforme (#1499).

La liste d'outils d'un travail est tenue par le serveur sur chacun de SES appels
(`outils_du_travail`). Lancer un AUTRE agent (`oto_trigger op=run_now`) : l'enfant tourne
avec SA liste, qui tient dans celle du parent (`chain_widens_tools`) sauf si une personne a
déclaré un lien `starts` du parent vers lui. Une campagne : sa création est bornée par la
liste du travail (`tools_human_only`), sa liste ne s'élargit pas (`oto_fleet op=update`,
`tools_human_only`), et l'armer est un geste de personne (`not_from_a_run`). Dans tous les
cas, un enfant n'exerce pas des `oto_admin_*` avec le rôle plateforme du porteur que le
parent n'exerce pas (`chain_widens_platform_tools`).

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


def _poser_le_travail(org: int, **charge):
    """Enfile un travail parent de charge `charge`, émet son jeton par le vrai chemin du
    runner et pose le verrou que rend sa vraie vérification. Rend `(job_id, jeton)`."""
    from oto_mcp import db, verrou_org
    from oto_mcp.capabilities import runner_jobs
    job = db.enqueue_job(org, "start", sub=SUB, payload={"procedure": "p", "input": "x",
                                                         **charge})
    jeton = runner_jobs._delegue({"id": job["id"], "sub": SUB, "org_id": org},
                                 bail_s=60, claimant="test")["delegated_token"]
    return job["id"], verrou_org.poser(verrou_org.depuis_ligne(db.verify_api_token(jeton)))


@pytest.fixture
def depuis_le_travail(org):
    """Pose, pour la durée du test, le verrou du jeton d'un travail parent dont la liste
    est `PARENT` (et sa procédure, d'où `oto_procedure`)."""
    from oto_mcp import verrou_org
    job, t = _poser_le_travail(org, tools=list(PARENT))
    yield job
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


def test_sans_lien_un_enfant_plus_outille_ne_part_pas(org, depuis_le_travail):
    from oto_mcp.capabilities._types import AuthzDenied
    enfant = _agent(org, ["acme_people", "acme_siret", "acme_mail"])
    with pytest.raises(AuthzDenied) as e:
        _run_now(org, enfant)
    assert (e.value.status, e.value.code) == (403, "chain_widens_tools")
    assert "acme_mail" in e.value.message
    assert _en_file(org, enfant) == 0, "rien n'est enfilé"


def test_un_lien_declare_laisse_partir_un_enfant_plus_outille(org):
    """Le relais vers un agent spécialisé, décidé par une personne (un lien `starts`) :
    l'enfant a des outils que le parent n'a pas, il est enfilé avec SA liste, chaîné au
    parent."""
    from oto_mcp import db, verrou_org
    parent = _agent(org, list(PARENT))
    enfant = _agent(org, ["acme_people", "acme_siret", "acme_mail"])
    db.runner_links.poser(org, parent, enfant, db.runner_links.DEMARRE, None, SUB)
    job, t = _poser_le_travail(org, tools=list(PARENT), trigger_id=str(parent))
    try:
        out = _run_now(org, enfant)
    finally:
        verrou_org.lever(t)
    assert out["ok"] and out["job"]["chain"]["parent_job"] == job
    assert _en_file(org, enfant) == 1
    servis = db.charge_du_travail(int(out["job"]["id"]))["tools"]
    assert {"acme_siret", "acme_mail"} <= set(servis), servis
    assert "oto_trigger" not in servis, "l'enfant ne reçoit pas la liste du parent"


def test_un_enfant_dont_la_liste_tient_dans_celle_du_parent_part(org, depuis_le_travail):
    enfant = _agent(org, ["acme_people"])
    out = _run_now(org, enfant)
    assert out["ok"] and out["job"]["chain"]["parent_job"] == depuis_le_travail
    assert _en_file(org, enfant) == 1


def test_une_personne_lance_l_agent_qu_elle_veut(org):
    """Hors jeton de délégation, rien ne change : le bouton de l'écran lance l'agent."""
    enfant = _agent(org, ["acme_people", "acme_siret"])
    out = _run_now(org, enfant)
    assert out["ok"] and "chain" not in out["job"]


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


@pytest.fixture
def armement_servi(monkeypatch):
    """Ce que l'armement exige d'un worker et de la boîte de l'org — hors sujet ici."""
    from oto_mcp.capabilities import runner_fleets as RF
    monkeypatch.setattr(RF._outils_manquants, "manquants", lambda *a, **k: None)
    monkeypatch.setattr(RF._cle_exigee, "exiger_a_la_pose", lambda *a, **k: None)
    monkeypatch.setattr(RF._abonnement, "exiger_a_la_pose", lambda *a, **k: None)
    monkeypatch.setattr(RF._modele, "exige_un_runner", lambda org: {})
    monkeypatch.setattr(RF._modele, "exige_servi", lambda *a, **k: None)


@pytest.mark.parametrize("outils", [["acme_people", "acme_siret"], ["acme_people"]])
def test_un_travail_n_arme_aucune_campagne(org, depuis_le_travail, armement_servi, outils):
    """Sans nommer son run (`_run_id`) : le jeton de délégation suffit à le reconnaître."""
    from oto_mcp import db
    from oto_mcp.capabilities._types import AuthzDenied
    f = _campagne(org, outils)
    with pytest.raises(AuthzDenied) as e:
        _armer(org, f)
    assert (e.value.status, e.value.code) == (403, "not_from_a_run")
    assert db.get_fleet(f, org)["status"] == "draft"


def test_une_personne_arme_une_campagne(org, armement_servi):
    from oto_mcp import db
    f = _campagne(org, ["acme_people", "acme_siret"])
    _armer(org, f)
    assert db.get_fleet(f, org)["status"] == "armed"


def _creer(org: int, outils: list) -> dict:
    from oto_mcp.capabilities import runner_fleets as RF
    from oto_mcp.capabilities._types import ResolvedCtx
    return RF._fleets(ResolvedCtx(sub=SUB, org_id=org),
                      RF.FleetInput(op="create", label=f"c-{uuid.uuid4().hex[:6]}",
                                    procedure="p", tools=outils, model="claude-sonnet-5"))


def test_un_travail_ne_cree_pas_une_campagne_plus_outillee_que_lui(org, depuis_le_travail,
                                                                  armement_servi):
    from oto_mcp import db
    from oto_mcp.capabilities._types import AuthzDenied
    avant = len(db.list_fleets(org, None))
    with pytest.raises(AuthzDenied) as e:
        _creer(org, ["acme_people", "acme_siret"])
    assert (e.value.status, e.value.code) == (403, "tools_human_only")
    assert "acme_siret" in e.value.message
    assert len(db.list_fleets(org, None)) == avant, "rien n'est écrit"


def test_un_travail_cree_une_campagne_avec_une_partie_de_ses_outils(org, depuis_le_travail,
                                                                   armement_servi):
    assert _creer(org, ["acme_people"])["fleet"]["tools"] == ["acme_people"]


def test_une_personne_cree_une_campagne_avec_n_importe_quels_outils(org, armement_servi):
    out = _creer(org, ["acme_people", "acme_siret"])
    assert out["fleet"]["tools"] == ["acme_people", "acme_siret"]


def _retoucher(org: int, fleet_id: int, outils: list) -> dict:
    from oto_mcp.capabilities import runner_fleets as RF
    from oto_mcp.capabilities._types import ResolvedCtx
    return RF._fleets(ResolvedCtx(sub=SUB, org_id=org),
                      RF.FleetInput(op="update", fleet_id=fleet_id, tools=outils))


def test_un_travail_n_elargit_pas_une_campagne_armee(org, depuis_le_travail):
    """Chaque passage d'une campagne relit SA liste (`campagne_a_servir`) : une campagne
    armée par une personne s'élargissait au passage suivant par un simple `update`."""
    from oto_mcp import db
    from oto_mcp.capabilities._types import AuthzDenied
    f = _campagne(org, ["acme_people"])
    assert db.armer(f, org)["status"] == "armed"   # armée par une personne
    with pytest.raises(AuthzDenied) as e:
        _retoucher(org, f, ["acme_people", "acme_siret"])
    assert (e.value.status, e.value.code) == (403, "tools_human_only")
    assert "acme_siret" in e.value.message
    assert db.get_fleet(f, org)["tools"] == ["acme_people"], "rien n'est écrit"


def test_un_travail_reduit_ou_repose_la_liste_d_une_campagne(org, depuis_le_travail):
    from oto_mcp import db
    f = _campagne(org, ["acme_people", "acme_siret"])
    db.armer(f, org)
    assert _retoucher(org, f, ["acme_siret", "acme_people"])["fleet"]["tools"]
    assert _retoucher(org, f, ["acme_people"])["fleet"]["tools"] == ["acme_people"]


def test_une_personne_elargit_une_campagne(org):
    f = _campagne(org, ["acme_people"])
    out = _retoucher(org, f, ["acme_people", "acme_siret"])
    assert out["fleet"]["tools"] == ["acme_people", "acme_siret"]


# ── Le rôle plateforme d'un enfant : `outils_plateforme` (#1499) ────────────────

ADMIN = "oto_admin_monitoring"


@pytest.fixture
def famille_d_abonnement_servie(monkeypatch):
    """Les `oto_admin_*` d'un agent ne valent que sur l'abonnement personnel de son
    porteur (`db.outils_plateforme_du_travail`) : la famille doit être servie."""
    from oto_mcp.capabilities import runner_triggers as RT
    monkeypatch.setattr(RT.db, "runner_arme",
                        lambda org: {"armed": True, "workers": 1,
                                     "last_seen": "2026-10-21 07:00:00",
                                     "families": ["anthropic", "claude_subscription"]})


def _agent_admin(org: int, plateforme: list) -> int:
    """Un agent sur l'abonnement de SUB, seul à pouvoir le changer, qui exerce
    `plateforme` avec le rôle plateforme de son porteur."""
    from oto_mcp import db
    t = db.create_trigger(org, SUB, procedure=f"admin-{uuid.uuid4().hex[:6]}",
                          cron="5 6 * * *", tz="UTC",
                          next_due=datetime.datetime.now(datetime.timezone.utc),
                          tools=["acme_people", ADMIN], model="sub:sonnet")
    db.update_trigger(int(t["id"]), org, {"outils_plateforme": plateforme})
    return int(t["id"])


@pytest.mark.parametrize("outils_du_parent", [
    ["acme_people", ADMIN],          # même liste que l'enfant
    ["acme_people", "oto_trigger"],  # moins outillé, relié : le relais passe, pas le rôle
])
def test_un_travail_ne_lance_pas_un_agent_qui_exerce_un_role_plateforme_qu_il_n_a_pas(
        org, famille_d_abonnement_servie, outils_du_parent):
    """L'enfant tourne sous le MÊME porteur : `outils_plateforme_du_travail` lui rendrait
    les `oto_admin_*` de son agent, que le parent n'exerce pas. Un lien `starts` ouvre le
    relais vers un agent plus outillé, pas le prêt du rôle plateforme."""
    from oto_mcp import db, verrou_org
    from oto_mcp.capabilities._types import AuthzDenied
    parent = _agent(org, list(outils_du_parent))
    enfant = _agent_admin(org, [ADMIN])
    db.runner_links.poser(org, parent, enfant, db.runner_links.DEMARRE, None, SUB)
    _, t = _poser_le_travail(org, tools=outils_du_parent, trigger_id=str(parent))
    try:
        assert verrou_org.outils_permis() == frozenset()
        with pytest.raises(AuthzDenied) as e:
            _run_now(org, enfant)
    finally:
        verrou_org.lever(t)
    assert (e.value.status, e.value.code) == (403, "chain_widens_platform_tools")
    assert ADMIN in e.value.message
    assert _en_file(org, enfant) == 0, "rien n'est enfilé"


def test_un_travail_qui_exerce_deja_ces_outils_d_admin_lance_l_agent(
        org, famille_d_abonnement_servie):
    from oto_mcp import verrou_org
    parent = _agent_admin(org, [ADMIN])
    enfant = _agent_admin(org, [ADMIN])
    _, t = _poser_le_travail(org, tools=["acme_people", ADMIN], trigger_id=str(parent))
    try:
        assert verrou_org.outils_permis() == frozenset({ADMIN})
        assert _run_now(org, enfant)["ok"]
    finally:
        verrou_org.lever(t)


def test_un_agent_qui_ne_prete_pas_son_role_se_lance(org, famille_d_abonnement_servie):
    """La liste servie est celle que le serveur TIENT : un agent dont la liste ne vaut
    rien (ici, partagé en écriture) ne prête aucun rôle — le lancer n'élargit rien."""
    from oto_mcp import db, verrou_org
    enfant = _agent_admin(org, [ADMIN])
    db.grant_resource("runner_trigger", str(enfant), "user", "un-editeur", role="editor",
                      granted_by=SUB)
    _, t = _poser_le_travail(org, tools=["acme_people", ADMIN])
    try:
        assert _run_now(org, enfant)["ok"]
    finally:
        verrou_org.lever(t)
