"""Un agent travaille dans un des projets de sa procédure (tranché le 24/10/2026).

Une procédure peut être rangée dans plusieurs projets de l'org ; un agent, dans UN
d'entre eux — chaque run se fait dans un projet, qui décide des tableaux de ses slots
et des comptes qu'il utilise. Pour qu'une procédure travaille pour deux projets, on lui
donne un agent dans chacun. Les bancs :
- la pose et la retouche : le premier projet par défaut, un autre des siens accepté, un
  projet hors d'eux refusé avec le geste qui y range la procédure, une procédure rangée
  nulle part rangée là où l'agent travaille — après l'écriture, jamais avant ;
- plusieurs agents par procédure, dans un même projet aussi (la limite d'un agent de
  chaque genre est levée) ;
- le run et la lecture d'un agent sans projet à lui : le premier de sa procédure ;
- sur base réelle, la lecture des projets d'une procédure : ceux de l'org, non archivés,
  sous l'une ou l'autre écriture du lien, le premier lié d'abord.
"""
from __future__ import annotations

import asyncio
import datetime
import uuid

import pytest

from oto_mcp import runner_tick
from oto_mcp.capabilities import _projet_de_l_agent as PA
from oto_mcp.capabilities import runner_triggers as RT
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx

ORG = 2


def _ctx():
    return ResolvedCtx(sub="alexis", org_id=ORG)


def _appel(**kw):
    if kw.get("op") == "create":
        kw.setdefault("model", "claude-sonnet-5")
    return asyncio.run(RT._triggers(_ctx(), RT.TriggerInput(**kw)))


@pytest.fixture
def banc(monkeypatch):
    """Une org servie, des projets de l'org lisibles et inscriptibles, et la procédure
    `veille` rangée dans les projets 10 puis 20. Ce que la pose écrit est relevé."""
    from oto_mcp import db, org_store, ownership
    from oto_mcp.capabilities import _acces_agent
    vu: dict = {"liens": [], "agents": []}
    monkeypatch.setattr(_acces_agent, "niveaux",
                        lambda sub, org_id, agents: {int(t["id"]): "owner" for t in agents})
    monkeypatch.setattr("oto_mcp.db.connector_settings.get_connector_setting",
                        lambda *a, **k: None)
    monkeypatch.setattr(db.runner_links, "depuis_l_agent", lambda *a: [])
    monkeypatch.setattr(db.runner_links, "vers_l_agent", lambda *a: [])
    monkeypatch.setattr(RT.db, "runner_arme",
                        lambda org: {"armed": True, "workers": 1,
                                     "last_seen": "2026-10-24 07:00:00",
                                     "families": ["anthropic"]})
    monkeypatch.setattr(RT.db, "get_project_by_id",
                        lambda pid: {"id": pid, "owner_type": "org", "owner_id": str(ORG)})
    monkeypatch.setattr(ownership, "can_access", lambda *a, **k: True)
    monkeypatch.setattr(ownership, "visible_in_org", lambda *a, **k: True)
    rangees = {"veille": [10, 20]}
    monkeypatch.setattr(RT.db, "projets_des_procedures",
                        lambda org, slugs: {s: rangees[s] for s in slugs if s in rangees})
    monkeypatch.setattr(RT.db, "maison_de_la_procedure",
                        lambda org, slug: (rangees.get(slug) or [None])[0])
    monkeypatch.setattr(RT.db, "maisons_des_procedures",
                        lambda org, slugs: {s: rangees[s][0] for s in slugs if s in rangees})
    monkeypatch.setattr(RT.db, "triggers_for_procedure", lambda org, p: list(vu["agents"]))
    monkeypatch.setattr(RT.db, "create_trigger",
                        lambda org, sub, **kw: vu.update(pose=kw) or {"id": 1, "org_id": org, **kw})
    monkeypatch.setattr(org_store, "get_instruction",
                        lambda scope, owner, slug, *a, **k: {"id": 7, "slug": slug})
    monkeypatch.setattr(RT.db, "add_project_link",
                        lambda pid, t, ref, *a, **k: vu["liens"].append((pid, t, ref)) or {})
    monkeypatch.setattr(RT.db, "log_project_activity", lambda *a, **k: None)
    vu["rangees"] = rangees
    return vu


def _pose(**kw):
    return _appel(op="create", procedure=kw.pop("procedure", "veille"), cron="5 6 * * *",
                  tools=["data_write"], **kw)


# ── la pose ───────────────────────────────────────────────────────────────────

def test_sans_projet_l_agent_travaille_dans_le_premier_de_sa_procedure(banc):
    _pose()
    assert banc["pose"]["project_id"] == 10


def test_un_autre_des_projets_de_la_procedure_est_accepte(banc):
    _pose(project_id=20)
    assert banc["pose"]["project_id"] == 20
    assert banc["liens"] == [], "la procédure y est déjà : rien à ranger"


def test_un_projet_hors_de_la_procedure_est_refuse_avec_le_geste_qui_l_y_range(banc):
    with pytest.raises(AuthzDenied) as e:
        _pose(project_id=30)
    assert (e.value.status, e.value.code) == (409, "project_not_of_procedure")
    assert "oto_project op=link project_id=30 target_type=procedure target_ref=veille" in (
        e.value.message)
    assert "pose" not in banc, "refusé AVANT d'écrire l'agent"


def test_une_procedure_rangee_nulle_part_l_est_la_ou_travaille_l_agent(banc):
    banc["rangees"].clear()
    _pose(project_id=30)
    assert banc["pose"]["project_id"] == 30
    assert banc["liens"] == [(30, "procedure", "7")], "rangée par son identifiant"


def test_un_refus_apres_le_projet_ne_range_rien(banc, monkeypatch):
    """Le rangement suit l'écriture de l'agent : une pose refusée plus loin (aucun outil)
    ne laisse pas une procédure rangée par un appel échoué."""
    banc["rangees"].clear()
    monkeypatch.setattr(RT, "_outils_de_la_procedure", lambda ctx, slug: [])
    with pytest.raises(AuthzDenied) as e:
        _appel(op="create", procedure="veille", cron="5 6 * * *", project_id=30)
    assert e.value.code == "no_tools"
    assert banc["liens"] == []


# ── plusieurs agents par procédure ────────────────────────────────────────────

@pytest.mark.parametrize("projet", [10, 20])
def test_un_second_agent_programme_est_accepte_dans_un_projet_comme_dans_l_autre(banc, projet):
    """Deux agents d'une procédure ne font pas forcément le même travail : chacun a ses
    instructions, et une procédure en sections se partage entre eux."""
    banc["agents"].append({"id": 4, "kind": "schedule", "project_id": 10, "cron": "0 8 * * *"})
    _pose(project_id=projet, input="Applique UNIQUEMENT la section B.")
    assert banc["pose"]["project_id"] == projet
    assert banc["pose"]["input"] == "Applique UNIQUEMENT la section B."


# ── la retouche ───────────────────────────────────────────────────────────────

@pytest.fixture
def retouche(banc, monkeypatch):
    actuel = {"id": 3, "sub": "alexis", "kind": "schedule", "procedure": "autre",
              "project_id": 20, "cron": "5 6 * * *", "tz": "Europe/Paris", "enabled": True,
              "model": "claude-sonnet-5"}
    monkeypatch.setattr(RT.db, "get_trigger", lambda i, o: dict(actuel))
    monkeypatch.setattr(RT.db, "update_trigger",
                        lambda i, o, champs, **k: banc.update(ecrit=dict(champs))
                        or {**actuel, **champs})
    banc["actuel"] = actuel
    return banc


def test_une_autre_procedure_garde_le_projet_de_l_agent_s_il_en_est_un(retouche):
    _appel(op="update", trigger_id=3, procedure="veille")
    assert retouche["ecrit"]["project_id"] == 20


def test_une_autre_procedure_emmene_l_agent_dans_son_premier_projet(retouche):
    retouche["actuel"]["project_id"] = 99
    _appel(op="update", trigger_id=3, procedure="veille")
    assert retouche["ecrit"]["project_id"] == 10


def test_retoucher_le_projet_hors_de_la_procedure_est_refuse(retouche):
    retouche["actuel"]["procedure"] = "veille"
    with pytest.raises(AuthzDenied) as e:
        _appel(op="update", trigger_id=3, project_id=30)
    assert e.value.code == "project_not_of_procedure"
    assert "ecrit" not in retouche


def test_retoucher_vers_un_projet_ou_travaille_un_autre_agent_est_accepte(retouche):
    retouche["actuel"]["procedure"] = "veille"
    retouche["agents"].append({"id": 4, "kind": "schedule", "project_id": 10})
    _appel(op="update", trigger_id=3, project_id=10)
    assert retouche["ecrit"]["project_id"] == 10


def test_une_retouche_ordinaire_ne_lit_pas_les_projets(retouche, monkeypatch):
    monkeypatch.setattr(RT.db, "projets_des_procedures",
                        lambda *a: pytest.fail("renommer n'a rien à lire des projets"))
    _appel(op="update", trigger_id=3, label="Nouveau nom")
    assert "project_id" not in retouche["ecrit"]


# ── le run et la lecture d'un agent sans projet à lui ─────────────────────────

def _declencheur(**kw):
    base = {"id": 5, "org_id": ORG, "cron": "5 6 * * *", "tz": "Europe/Paris",
            "procedure": "veille", "project_id": None, "tools": ["data_write"],
            "input": None, "label": None, "max_steps": None, "model": "claude-sonnet-5"}
    base.update(kw)
    return base


def test_le_run_d_un_agent_sans_projet_travaille_dans_le_premier_de_sa_procedure(monkeypatch):
    monkeypatch.setattr(PA.db, "maison_de_la_procedure", lambda org, slug: 10)
    quand = datetime.datetime(2026, 10, 24, 6, 5, tzinfo=datetime.timezone.utc)
    assert runner_tick.charge_du_declencheur(_declencheur(), quand)["project_id"] == 10
    assert runner_tick.charge_du_declencheur(_declencheur(project_id=20), quand)["project_id"] == 20


def test_une_lecture_qui_echoue_laisse_partir_le_run_sans_projet(monkeypatch):
    def panne(org, slug):
        raise RuntimeError("base indisponible")
    monkeypatch.setattr(PA.db, "maison_de_la_procedure", panne)
    assert PA.de_l_agent(_declencheur()) is None


def test_la_lecture_sert_le_premier_projet_de_la_procedure_aux_seuls_agents_sans_projet(
        monkeypatch):
    monkeypatch.setattr(PA.db, "maisons_des_procedures", lambda org, slugs: {"veille": 10})
    servis = PA.servis(ORG, [_declencheur(id=1), _declencheur(id=2, project_id=20),
                             _declencheur(id=3, procedure="ailleurs")])
    assert [t["project_id"] for t in servis] == [10, 20, None]


# ── sur base réelle : les projets d'une procédure ─────────────────────────────

def test_les_projets_d_une_procedure_sur_base_reelle(live):
    """Ceux de l'org, non archivés, sous l'id OU le slug du lien, le premier lié
    d'abord. Ni le projet personnel d'un membre, ni le projet d'une autre org qui range
    une procédure du même slug, ni un projet archivé."""
    from oto_mcp import db, org_store
    from oto_mcp.db._conn import _connect

    u = uuid.uuid4().hex[:8]
    moi = f"moi_{u}"
    db.upsert_user(moi, email=f"{moi}@example.test")
    org_a = org_store.create_org(f"a_{u}", created_by=moi)
    org_store.add_org_member(org_a, moi)
    org_b = org_store.create_org(f"b_{u}", created_by=moi)
    org_store.add_org_member(org_b, moi)
    slug = f"veille-{u}"
    for org in (org_a, org_b):
        org_store.set_instruction("org", org, slug, "# Veille\n", title="Veille", set_by=moi)
    proc = int(org_store.get_instruction("org", org_a, slug)["id"])
    proc_b = int(org_store.get_instruction("org", org_b, slug)["id"])

    deuxieme = db.create_project("org", str(org_a), f"deuxieme_{u}", created_by=moi)
    premier = db.create_project("org", str(org_a), f"premier_{u}", created_by=moi)
    archive = db.create_project("org", str(org_a), f"archive_{u}", created_by=moi)
    perso = db.create_project("user", moi, f"perso_{u}", created_by=moi)
    ailleurs = db.create_project("org", str(org_b), f"ailleurs_{u}", created_by=moi)
    db.add_project_link(premier, "procedure", str(proc), None)
    db.add_project_link(deuxieme, "procedure", slug, None)   # l'écriture d'avant l'id
    db.add_project_link(archive, "procedure", str(proc), None)
    db.add_project_link(perso, "procedure", str(proc), None)
    db.add_project_link(ailleurs, "procedure", str(proc_b), None)
    db.archive_project(archive)
    with _connect() as c:   # l'ordre des liens, posé plutôt que laissé à l'horloge
        for pid, jour in ((premier, "2026-10-01"), (deuxieme, "2026-10-02")):
            c.execute("UPDATE project_links SET created_at = %s WHERE project_id = %s",
                      (jour, pid))

    assert db.projets_des_procedures(org_a, [slug, f"absente-{u}"]) == {slug: [premier, deuxieme]}
    assert db.maison_de_la_procedure(org_a, slug) == premier
    assert db.maisons_des_procedures(org_b, [slug]) == {slug: ailleurs}
    assert db.maison_de_la_procedure(org_a, f"absente-{u}") is None
