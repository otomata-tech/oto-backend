"""Le pool exclusif et le don, EN BASE — ce qu'une doublure ne peut pas prouver.

1. Une org qui sert `claude_subscription` par son POOL (`mode = pool`) ne fait jamais
   tourner un agent `sub:*` sur le forfait de son propriétaire : un partage en écriture
   que le PROPRIÉTAIRE a posé à l'org le rend modifiable et rallumable par un membre,
   sans `op=lend` — la pose se juge alors sur le propriétaire, et le modèle reste celui
   du moment. Ne comptent jamais : un partage d'admin (la séquence de l'admin qui se
   partage l'agent d'un membre), ceux de la révision 0035 et de `partage_par_defaut`.
2. Une org en `personnel`, ou qui n'a rien réglé, garde la règle du prêt : le partage
   collectif du propriétaire, sans `op=lend`, ne donne que la pause.
3. Sur un agent servi par le pool, un partage en écriture DU PROPRIÉTAIRE ferme ses
   `oto_admin_*` ; celui d'un admin non.
4. QUITTER le pool éteint et liste les agents allumés qu'un autre que leur propriétaire
   a changés en dernier ; le propriétaire les rallume, et sa retouche le remet auteur.
5. `give` ne prête jamais le forfait du destinataire : son préréglage pose au plus le
   partage à l'org (rien si l'admin le refuse) ; un jeton de délégation ne donne pas ;
   l'ancien propriétaire reste éditeur (`keep_previous_owner`, défaut true) par un
   partage MARQUÉ qui ne prête rien : hors pool, la pause ; en pool, la main — à lui
   seul, jusqu'à ce que le nouveau le retire ou qu'un autre le repose.
Joué sur une vraie base.
"""
from __future__ import annotations

import asyncio

import psycopg
import pytest

from oto_mcp import org_store, verrou_org
from oto_mcp.capabilities import runner_triggers as RT
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
from oto_mcp.capabilities.orgs import model_subscriptions as MS
from oto_mcp.capabilities.orgs import partage_par_defaut as P
from oto_mcp.db import org_subscription_pool

PROPRIO, ADMIN, MEMBRE, NOUVEAU = "pool-proprio", "pool-admin", "pool-membre", "pool-nouveau"
MODELE, FAMILLE = "sub:sonnet", "claude_subscription"
MARQUE_0035 = "migration:0035_agents_partages_a_l_org"


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
    # Un agent ne se pose que sur une procédure que son propriétaire lit par nom (#1499).
    org_store.set_instruction("org", org, "veille", "Veille.")
    return org, poses


def _appel(org, sub=PROPRIO, **kw):
    return asyncio.run(RT._triggers(ResolvedCtx(sub=sub, org_id=org), RT.TriggerInput(**kw)))


def _creer(org, sub=PROPRIO, **kw):
    kw.setdefault("enabled", False)
    return _appel(org, sub=sub, op="create", procedure="veille", cron="0 8 * * *",
                  tools=["a"], model=MODELE, **kw)["trigger"]


def _refus(org, sub, **kw) -> AuthzDenied:
    with pytest.raises(AuthzDenied) as e:
        _appel(org, sub=sub, **kw)
    return e.value


def _quitter_le_pool(org) -> dict:
    return MS._set_mode(ResolvedCtx(sub=ADMIN, org_id=org), MS.SetOrgModeInput(
        org_id=org, family=FAMILLE, mode="personnel"))


# ── 1. le pool : le partage collectif du PROPRIÉTAIRE, et lui seul ─────────────────
def test_en_pool_le_partage_du_PROPRIETAIRE_a_l_org_ouvre_l_ecriture(org_reelle):
    from oto_mcp import db
    org, poses = org_reelle
    org_subscription_pool.poser_mode(org, FAMILLE, "pool", ADMIN)
    t = _creer(org, private=True)
    _appel(org, op="share", trigger_id=t["id"], everyone=True, role="editor")
    assert not _appel(org, op="get", trigger_id=t["id"])["trigger"].get("subscription_lent")

    vu = _appel(org, sub=MEMBRE, op="get", trigger_id=t["id"])["trigger"]
    assert (vu["my_access"], vu["can_edit"]) == ("editor", True)
    assert db.forfaits_pretes([t["id"]], MEMBRE) == {t["id"]}
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


def test_en_pool_l_admin_qui_se_partage_l_agent_d_un_membre_ne_le_modifie_pas(org_reelle):
    """La séquence de l'admin : org en pool → il se partage en éditeur l'agent `sub:*`
    d'un membre (à lui, puis à toute l'org) → il change la consigne : REFUSÉ. Le pool
    retire le forfait du jeu, pas l'identité sous laquelle l'agent agit."""
    from oto_mcp import db
    org, _ = org_reelle
    org_subscription_pool.poser_mode(org, FAMILLE, "pool", ADMIN)
    t = _creer(org, private=True)
    _appel(org, sub=ADMIN, op="share", trigger_id=t["id"], share_with_sub=ADMIN,
           role="editor")
    _appel(org, sub=ADMIN, op="share", trigger_id=t["id"], everyone=True, role="editor")

    for qui in (ADMIN, MEMBRE):
        assert _appel(org, sub=qui, op="get", trigger_id=t["id"])["trigger"][
            "can_edit"] is False, qui
        assert _refus(org, qui, op="update", trigger_id=t["id"],
                      input="consigne d'un autre").code == "subscription_personal_only", qui
        assert db.forfaits_pretes([t["id"]], qui) == set(), qui
    assert _appel(org, sub=ADMIN, op="update", trigger_id=t["id"],
                  enabled=False)["trigger"]["enabled"] is False, "la pause reste ouverte"


def test_en_pool_un_partage_d_admin_REPOSE_sur_celui_du_proprietaire_le_ferme(org_reelle):
    """L'upsert réécrit `granted_by` : reposé par un admin, le partage n'est plus celui
    du propriétaire."""
    org, _ = org_reelle
    org_subscription_pool.poser_mode(org, FAMILLE, "pool", ADMIN)
    t = _creer(org, private=True)
    _appel(org, op="share", trigger_id=t["id"], everyone=True, role="editor")
    assert _appel(org, sub=MEMBRE, op="get", trigger_id=t["id"])["trigger"]["can_edit"]
    _appel(org, sub=ADMIN, op="share", trigger_id=t["id"], everyone=True, role="editor")
    assert _appel(org, sub=MEMBRE, op="get", trigger_id=t["id"])["trigger"][
        "can_edit"] is False


@pytest.mark.parametrize("marque", [MARQUE_0035, "reglage:partage_par_defaut"])
def test_en_pool_un_partage_MARQUE_n_ouvre_pas_l_ecriture(org_reelle, marque):
    """Ni la révision 0035 (partage `editor` à l'org des agents d'avant), ni le réglage
    `partage_par_defaut` : aucun n'est un geste du propriétaire."""
    from oto_mcp import db
    assert marque.startswith("reglage:") is (marque == db.MARQUE_PARTAGE_PAR_DEFAUT)
    org, _ = org_reelle
    org_subscription_pool.poser_mode(org, FAMILLE, "pool", ADMIN)
    t = _creer(org, private=True)
    db.grant_resource("runner_trigger", str(t["id"]), "org", str(org), role="editor",
                      granted_by=marque)
    vu = _appel(org, sub=MEMBRE, op="get", trigger_id=t["id"])["trigger"]
    assert (vu["my_access"], vu["can_edit"]) == ("editor", False)
    assert _refus(org, MEMBRE, op="update", trigger_id=t["id"],
                  input="x").code == "subscription_personal_only"
    assert db.forfaits_pretes([t["id"]], MEMBRE) == set()


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


# ── 2. hors pool : la règle du prêt ─────────────────────────────────────────────────
@pytest.mark.parametrize("mode", [None, "personnel"])
def test_hors_pool_le_partage_du_proprietaire_sans_pret_ne_donne_que_la_pause(org_reelle,
                                                                             mode):
    org, _ = org_reelle
    if mode:
        org_subscription_pool.poser_mode(org, FAMILLE, mode, ADMIN)
    t = _creer(org, private=True)
    _appel(org, op="share", trigger_id=t["id"], everyone=True, role="editor")
    vu = _appel(org, sub=MEMBRE, op="get", trigger_id=t["id"])["trigger"]
    assert vu["can_edit"] is False
    assert _refus(org, MEMBRE, op="update", trigger_id=t["id"],
                  input="x").code == "subscription_personal_only"
    assert _appel(org, sub=MEMBRE, op="update", trigger_id=t["id"],
                  enabled=False)["trigger"]["enabled"] is False


# ── 3. les outils plateforme ───────────────────────────────────────────────────────
def test_en_pool_le_partage_du_PROPRIETAIRE_ferme_les_outils_plateforme(org_reelle):
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
    db.grant_resource("runner_trigger", str(t["id"]), "org", str(org), role="editor",
                      granted_by=ADMIN)
    assert db.outils_plateforme_du_travail(travail()) == liste, (
        "un partage d'admin n'ouvre pas l'écriture : la liste reste ouverte")
    db.grant_resource("runner_trigger", str(t["id"]), "org", str(org), role="editor",
                      granted_by=PROPRIO)
    assert db.outils_plateforme_du_travail(travail()) == [], "pool + partage du propriétaire"
    org_subscription_pool.poser_mode(org, FAMILLE, "personnel", ADMIN)
    assert db.outils_plateforme_du_travail(travail()) == liste, (
        "hors pool, sans prêt, le partage collectif n'ouvre rien : la liste se rouvre")


# ── 4. quitter le pool ─────────────────────────────────────────────────────────────
def test_quitter_le_pool_eteint_et_liste_ce_qu_un_autre_a_change(org_reelle):
    from oto_mcp import db
    org, _ = org_reelle
    org_subscription_pool.poser_mode(org, FAMILLE, "pool", ADMIN)
    retouche = _creer(org, private=True, enabled=True, label="retouché")
    _appel(org, op="share", trigger_id=retouche["id"], everyone=True, role="editor")
    _appel(org, sub=MEMBRE, op="update", trigger_id=retouche["id"],
           input="consigne du membre")
    intact = _creer(org, private=True, enabled=True, label="intact")
    du_proprio = _creer(org, private=True, enabled=True, label="du propriétaire")
    _appel(org, op="update", trigger_id=du_proprio["id"], input="la sienne")

    rep = _quitter_le_pool(org)
    assert rep["mode"] == "personnel"
    assert rep["disabled_agents"] == [{"id": retouche["id"], "owner": PROPRIO,
                                       "label": "retouché", "last_changed_by": MEMBRE}]
    assert db.get_trigger(retouche["id"], org)["enabled"] is False
    assert db.get_trigger(intact["id"], org)["enabled"] is True, "jamais changé : le sien"
    assert db.get_trigger(du_proprio["id"], org)["enabled"] is True

    assert _refus(org, MEMBRE, op="update", trigger_id=retouche["id"],
                  enabled=True).code == "subscription_personal_only", (
        "hors pool, le membre ne le rallume pas")
    rallume = _appel(org, op="update", trigger_id=retouche["id"], enabled=True)["trigger"]
    assert (rallume["enabled"], rallume["updated_by"]) == (True, PROPRIO), (
        "le rallumage du propriétaire le remet auteur")

    org_subscription_pool.poser_mode(org, FAMILLE, "pool", ADMIN)
    assert _quitter_le_pool(org)["disabled_agents"] == [], "revu par lui : il reste allumé"


def test_rester_hors_pool_n_eteint_rien(org_reelle):
    org, _ = org_reelle
    org_subscription_pool.poser_mode(org, FAMILLE, "personnel", ADMIN)
    rep = _quitter_le_pool(org)
    assert rep.get("disabled_agents") is None


# ── 5. le don ──────────────────────────────────────────────────────────────────────
def test_le_don_a_qui_partage_par_prereglage_partage_SANS_preter(org_reelle):
    from oto_mcp import db
    org, _ = org_reelle
    _appel(org, sub=NOUVEAU, op="default_sharing", default_sharing="shared")
    t = _creer(org, private=True)

    rep = _appel(org, op="give", trigger_id=t["id"], share_with_sub=NOUVEAU)
    assert rep["trigger"]["sub"] == NOUVEAU
    assert (rep["preset_applied"], rep["kept_previous_owner"], rep["lend_ended"]) == (
        True, True, False)
    assert "lent" not in rep
    assert rep["trigger"]["visibility"]["org"] == "editor"
    assert not rep["trigger"].get("subscription_lent"), "le don ne prête pas"
    assert db.forfaits_pretes([t["id"]], MEMBRE) == set()
    assert _refus(org, MEMBRE, op="update", trigger_id=t["id"],
                  input="x").code == "subscription_personal_only"

    # Le destinataire prête LUI-MÊME : son partage, à son nom, ouvre alors le prêt.
    assert _appel(org, sub=NOUVEAU, op="lend", trigger_id=t["id"])["trigger"][
        "subscription_lent"] is True
    assert db.forfaits_pretes([t["id"]], MEMBRE) == {t["id"]}


def test_le_don_en_pool_le_partage_du_destinataire_ouvre_l_ecriture(org_reelle):
    org, _ = org_reelle
    org_subscription_pool.poser_mode(org, FAMILLE, "pool", ADMIN)
    _appel(org, sub=NOUVEAU, op="default_sharing", default_sharing="shared")
    t = _creer(org, private=True)
    rep = _appel(org, op="give", trigger_id=t["id"], share_with_sub=NOUVEAU)
    assert "its members can edit it" in rep["handover_note"]
    assert _appel(org, sub=MEMBRE, op="update", trigger_id=t["id"],
                  input="par un collègue")["trigger"]["updated_by"] == MEMBRE


def test_le_don_quand_l_admin_refuse_le_prereglage_ne_partage_rien(org_reelle):
    from oto_mcp import db
    org, _ = org_reelle
    _appel(org, sub=NOUVEAU, op="default_sharing", default_sharing="shared")
    P.poser(org, {P.PRESET: P.REFUSE})
    t = _creer(org, private=True)

    rep = _appel(org, op="give", trigger_id=t["id"], share_with_sub=NOUVEAU,
                 keep_previous_owner=False)
    assert rep["preset_applied"] is False
    assert rep["trigger"]["visibility"] == {"org": None, "people": 0}
    assert "Nothing is shared with the org or lent" in rep["handover_note"]
    assert db.forfaits_pretes([t["id"]], MEMBRE) == set()


def test_le_don_par_un_jeton_de_delegation_est_refuse(org_reelle, monkeypatch):
    from oto_mcp import db
    org, _ = org_reelle
    t = _creer(org, private=True)
    monkeypatch.setattr(verrou_org, "travail_de_la_requete", lambda: object())
    e = _refus(org, PROPRIO, op="give", trigger_id=t["id"], share_with_sub=NOUVEAU)
    assert (e.status, e.code) == (403, "give_human_only")
    assert db.get_trigger(t["id"], org)["sub"] == PROPRIO, "rien n'est transféré"


def test_hors_pool_le_donneur_garde_ne_peut_que_mettre_en_pause(org_reelle):
    """Sur le forfait personnel du nouveau, le partage gardé ne prête rien."""
    from oto_mcp import db
    org, _ = org_reelle
    t = _creer(org, private=True)
    _appel(org, op="share", trigger_id=t["id"], everyone=True, role="editor")
    assert _appel(org, op="lend", trigger_id=t["id"])["trigger"]["subscription_lent"]
    rep = _appel(org, op="give", trigger_id=t["id"], share_with_sub=NOUVEAU)
    assert (rep["kept_previous_owner"], rep["lend_ended"]) == (True, True)
    assert (rep["trigger"]["my_access"], rep["trigger"]["can_edit"]) == ("editor", False)
    assert "only pause it" in rep["handover_note"]
    assert db.forfaits_pretes([t["id"]], PROPRIO) == set()
    assert db.forfaits_pretes([t["id"]], MEMBRE) == set()
    assert _refus(org, PROPRIO, op="update", trigger_id=t["id"],
                  input="x").code == "subscription_personal_only"


def test_en_pool_le_donneur_garde_la_main_et_lui_seul(org_reelle):
    """On construit un agent pour quelqu'un, on le lui donne, on le maintient."""
    from oto_mcp import db
    org, _ = org_reelle
    org_subscription_pool.poser_mode(org, FAMILLE, "pool", ADMIN)
    t = _creer(org, private=True)
    rep = _appel(org, op="give", trigger_id=t["id"], share_with_sub=NOUVEAU)
    assert (rep["trigger"]["my_access"], rep["trigger"]["can_edit"]) == ("editor", True)
    assert "can keep changing it" in rep["handover_note"]
    ecrit = _appel(org, op="update", trigger_id=t["id"], input="maintenu")["trigger"]
    assert (ecrit["input"], ecrit["updated_by"]) == ("maintenu", PROPRIO)
    assert db.forfaits_pretes([t["id"]], PROPRIO) == {t["id"]}
    assert db.forfaits_pretes([t["id"]], MEMBRE) == set(), "à lui seul"
    e = _refus(org, PROPRIO, op="update", trigger_id=t["id"], model="sub:haiku")
    assert e.code == "subscription_personal_only", "lié au modèle, comme le pool"

    _appel(org, sub=NOUVEAU, op="unshare", trigger_id=t["id"], share_with_sub=PROPRIO)
    assert _refus(org, PROPRIO, op="update", trigger_id=t["id"],
                  input="x").status in (403, 404), "le nouveau le retire"


def test_en_pool_le_partage_garde_REPOSE_par_un_admin_perd_sa_marque(org_reelle):
    org, _ = org_reelle
    org_subscription_pool.poser_mode(org, FAMILLE, "pool", ADMIN)
    t = _creer(org, private=True)
    _appel(org, op="give", trigger_id=t["id"], share_with_sub=NOUVEAU)
    _appel(org, sub=ADMIN, op="share", trigger_id=t["id"], share_with_sub=PROPRIO,
           role="editor")
    assert _appel(org, op="get", trigger_id=t["id"])["trigger"]["can_edit"] is False


def test_le_don_sans_garder_ne_laisse_rien(org_reelle):
    org, _ = org_reelle
    org_subscription_pool.poser_mode(org, FAMILLE, "pool", ADMIN)
    t = _creer(org, private=True)
    rep = _appel(org, op="give", trigger_id=t["id"], share_with_sub=NOUVEAU,
                 keep_previous_owner=False)
    assert rep["kept_previous_owner"] is False
    assert rep["trigger"]["my_access"] is None


def test_le_partage_garde_au_don_ferme_les_outils_plateforme(org_reelle):
    from oto_mcp import db
    org, _ = org_reelle
    liste = ["oto_admin_org"]
    t = db.create_trigger(org, PROPRIO, procedure="veille", tz="UTC", tools=["a", *liste],
                          cron="0 8 * * *", model=MODELE)
    db.update_trigger(t["id"], org, {"outils_plateforme": liste})

    def travail():
        return db.enqueue_job(org, "start", {"trigger_id": t["id"]}, sub=PROPRIO)["id"]

    assert db.outils_plateforme_du_travail(travail()) == liste
    db.grant_resource("runner_trigger", str(t["id"]), "user", MEMBRE, role="editor",
                      granted_by=db.MARQUE_GARDE_AU_DON)
    assert db.outils_plateforme_du_travail(travail()) == [], "fermée du côté sûr"
