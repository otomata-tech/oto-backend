"""Le pool exclusif et le don, EN BASE — ce qu'une doublure ne peut pas prouver (31/10/2026).

1. Une org qui sert `claude_subscription` par son POOL (`mode = pool`) ne fait jamais
   tourner un agent `sub:*` sur le forfait de son propriétaire : un partage en écriture
   à l'org posé par un ADMIN le rend modifiable et rallumable par un membre — la pose se
   juge alors sur le propriétaire, et le modèle reste celui du moment. Sans partage en
   écriture, un admin ne peut que l'éteindre.
2. Une org en `personnel`, ou qui n'a rien réglé, garde la règle du prêt (#1424) : le même
   partage d'admin ne donne que la pause.
3. Sur un agent servi par le pool, un partage en écriture ferme ses `oto_admin_*`.
4. `give` à qui a posé `default_sharing=shared` fait ses deux gestes (partage à l'org,
   prêt) ; sans préréglage, rien n'est prêté. L'ancien propriétaire reste éditeur nommé :
   il garde la main là où le forfait du nouveau n'est pas en jeu, ou s'il y a consenti.
Joué sur une vraie base.
"""
from __future__ import annotations

import asyncio

import psycopg
import pytest

from oto_mcp import org_store
from oto_mcp.capabilities import runner_triggers as RT
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
from oto_mcp.db import org_subscription_pool

PROPRIO, ADMIN, MEMBRE, NOUVEAU = "pool-proprio", "pool-admin", "pool-membre", "pool-nouveau"
MODELE, FAMILLE = "sub:sonnet", "claude_subscription"


@pytest.fixture
def org_reelle(live, pg_module_dsn, monkeypatch):
    """Une org en base et ses membres. Ce banc ne parle ni du runner ni des clés ; la
    garde de pose d'un abonnement est TRACÉE : au nom de qui elle se juge. La famille
    d'un modèle reste lue au catalogue (`famille_declaree`) : c'est elle qui décide de
    juger un changement de modèle."""
    for nom in ("exige_un_modele", "exige_un_runner", "exige_servi"):
        monkeypatch.setattr(RT._modele, nom, lambda *a, **k: None)
    monkeypatch.setattr(RT._cle_exigee, "exiger_a_la_pose", lambda *a, **k: None)
    poses: list[tuple] = []
    monkeypatch.setattr(RT._abonnement, "exiger_a_la_pose",
                        lambda sub, proprietaire, famille, **k:
                        poses.append((sub, proprietaire, famille)))
    with psycopg.connect(pg_module_dsn, autocommit=True) as c:
        org = c.execute("INSERT INTO orgs (name) VALUES ('Pool') RETURNING id").fetchone()[0]
        for m in (PROPRIO, ADMIN, MEMBRE, NOUVEAU):
            c.execute("INSERT INTO users (sub) VALUES (%s) ON CONFLICT DO NOTHING", (m,))
    for m in (PROPRIO, MEMBRE, NOUVEAU):
        org_store.add_org_member(org, m, "org_member")
    org_store.add_org_member(org, ADMIN, "org_admin")
    return org, poses


def _appel(org, sub=PROPRIO, **kw):
    return asyncio.run(RT._triggers(ResolvedCtx(sub=sub, org_id=org), RT.TriggerInput(**kw)))


def _creer(org, **kw):
    return _appel(org, op="create", procedure="veille", cron="0 8 * * *", tools=["a"],
                  model=MODELE, enabled=False, **kw)["trigger"]


def _refus(org, sub, **kw) -> AuthzDenied:
    with pytest.raises(AuthzDenied) as e:
        _appel(org, sub=sub, **kw)
    return e.value


def test_en_pool_l_editeur_pose_par_un_admin_modifie_et_rallume(org_reelle):
    org, poses = org_reelle
    org_subscription_pool.poser_mode(org, FAMILLE, "pool", ADMIN)
    t = _creer(org)
    _appel(org, sub=ADMIN, op="share", trigger_id=t["id"], everyone=True, role="editor")

    vu = _appel(org, sub=MEMBRE, op="get", trigger_id=t["id"])["trigger"]
    assert (vu["my_access"], vu["can_edit"]) == ("editor", True)
    ecrit = _appel(org, sub=MEMBRE, op="update", trigger_id=t["id"],
                   input="par le membre")["trigger"]
    assert (ecrit["input"], ecrit["updated_by"]) == ("par le membre", MEMBRE)

    poses.clear()
    assert _appel(org, sub=MEMBRE, op="update", trigger_id=t["id"],
                  enabled=True)["trigger"]["enabled"] is True
    assert poses == [(PROPRIO, PROPRIO, FAMILLE)], (
        "la pose se juge sur le propriétaire, comme pour un éditeur à qui il prête")

    e = _refus(org, MEMBRE, op="update", trigger_id=t["id"], model="sub:haiku")
    assert e.code == "subscription_personal_only", "lié au modèle, comme le prêt collectif"

    # L'admin qui a posé le partage à l'org en est membre : il le modifie lui aussi.
    assert _appel(org, sub=ADMIN, op="get", trigger_id=t["id"])["trigger"]["can_edit"] is True


def test_en_pool_sans_partage_en_ecriture_l_admin_ne_peut_que_l_eteindre(org_reelle):
    org, _ = org_reelle
    org_subscription_pool.poser_mode(org, FAMILLE, "pool", ADMIN)
    t = _creer(org, private=True)
    vu = _appel(org, sub=ADMIN, op="get", trigger_id=t["id"])["trigger"]
    assert (vu["my_access"], vu["can_edit"]) == ("admin", False)
    assert _refus(org, ADMIN, op="update", trigger_id=t["id"],
                  input="x").code == "subscription_personal_only"
    assert _appel(org, sub=ADMIN, op="update", trigger_id=t["id"],
                  enabled=False)["trigger"]["enabled"] is False


@pytest.mark.parametrize("mode", [None, "personnel"])
def test_hors_pool_le_partage_d_un_admin_ne_donne_que_la_pause(org_reelle, mode):
    """La règle de #1424 : seul le propriétaire prête son forfait."""
    org, _ = org_reelle
    if mode:
        org_subscription_pool.poser_mode(org, FAMILLE, mode, ADMIN)
    t = _creer(org)
    _appel(org, sub=ADMIN, op="share", trigger_id=t["id"], everyone=True, role="editor")
    _appel(org, sub=ADMIN, op="share", trigger_id=t["id"], share_with_sub=ADMIN,
           role="editor")

    for qui in (MEMBRE, ADMIN):
        vu = _appel(org, sub=qui, op="get", trigger_id=t["id"])["trigger"]
        assert vu["can_edit"] is False, qui
        assert _refus(org, qui, op="update", trigger_id=t["id"],
                      input="x").code == "subscription_personal_only", qui
    assert _appel(org, sub=MEMBRE, op="update", trigger_id=t["id"],
                  enabled=False)["trigger"]["enabled"] is False


def test_en_pool_un_partage_en_ecriture_ferme_les_outils_plateforme(org_reelle):
    from oto_mcp import db
    org, _ = org_reelle
    liste = ["oto_admin_org"]
    t = db.create_trigger(org, PROPRIO, procedure="veille", tz="UTC", tools=["a", *liste],
                          cron="0 8 * * *", model=MODELE)
    db.update_trigger(t["id"], org, {"outils_plateforme": liste})

    def travail():
        return db.enqueue_job(org, "start", {"trigger_id": t["id"]}, sub=PROPRIO)["id"]

    org_subscription_pool.poser_mode(org, FAMILLE, "pool", ADMIN)
    assert db.outils_plateforme_du_travail(travail()) == liste, "aucun partage : ouverte"
    db.grant_resource("runner_trigger", str(t["id"]), "user", MEMBRE, role="viewer",
                      granted_by=ADMIN)
    assert db.outils_plateforme_du_travail(travail()) == liste, "la lecture ne modifie rien"
    db.grant_resource("runner_trigger", str(t["id"]), "org", str(org), role="editor",
                      granted_by=ADMIN)
    assert db.outils_plateforme_du_travail(travail()) == [], "pool + partage en écriture"
    org_subscription_pool.poser_mode(org, FAMILLE, "personnel", ADMIN)
    assert db.outils_plateforme_du_travail(travail()) == liste, (
        "hors pool, un partage d'admin ne prête rien : la liste se rouvre")


def test_le_don_a_qui_partage_par_prereglage_prete_et_ouvre(org_reelle):
    from oto_mcp import db
    org, _ = org_reelle
    _appel(org, sub=NOUVEAU, op="default_sharing", default_sharing="shared")
    t = _creer(org, private=True)

    rep = _appel(org, op="give", trigger_id=t["id"], share_with_sub=NOUVEAU)
    assert rep["trigger"]["sub"] == NOUVEAU
    assert (rep["preset_applied"], rep["lent"], rep["kept_previous_owner"],
            rep["lend_ended"]) == (True, True, True, False)
    assert rep["trigger"]["visibility"]["org"] == "editor"
    assert rep["trigger"]["subscription_lent"] is True
    assert db.forfaits_pretes([t["id"]], MEMBRE) == {t["id"]}, "prêté à l'org, en base"
    assert rep["trigger"]["can_edit"] is True, "l'ancien emprunte par le prêt consenti"
    assert _appel(org, sub=MEMBRE, op="update", trigger_id=t["id"],
                  input="par un collègue")["trigger"]["updated_by"] == MEMBRE


def test_le_don_garde_l_ancien_en_editeur_sans_preter_le_forfait_du_nouveau(org_reelle):
    from oto_mcp import db
    org, _ = org_reelle
    t = _creer(org, private=True)
    _appel(org, op="share", trigger_id=t["id"], everyone=True, role="editor")
    assert _appel(org, op="lend", trigger_id=t["id"])["trigger"]["subscription_lent"] is True

    rep = _appel(org, op="give", trigger_id=t["id"], share_with_sub=NOUVEAU)
    assert (rep["kept_previous_owner"], rep["preset_applied"], rep["lent"],
            rep["lend_ended"]) == (True, False, False, True)
    assert "Nothing is lent" in rep["handover_note"]
    assert (rep["trigger"]["my_access"], rep["trigger"]["can_edit"]) == ("editor", False)
    partages = _appel(org, sub=NOUVEAU, op="shares", trigger_id=t["id"])["shares"]
    assert {"principal_type": "user", "sub": PROPRIO, "role": "editor"}.items() <= next(
        p for p in partages if p["sub"] == PROPRIO).items()
    assert db.forfaits_pretes([t["id"]], PROPRIO) == set(), (
        "son partage est posé à son nom : il ne prête pas le forfait du nouveau")

    org_subscription_pool.poser_mode(org, FAMILLE, "pool", ADMIN)
    assert _appel(org, op="get", trigger_id=t["id"])["trigger"]["can_edit"] is True, (
        "là où le forfait du nouveau n'est pas en jeu, l'ancien garde la main")
