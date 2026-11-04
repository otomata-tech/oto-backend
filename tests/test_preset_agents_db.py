"""Le préréglage personnel EN BASE — ce qu'une doublure ne peut pas prouver.

1. `op=default_sharing` se joue sans `trigger_id`, se pose et se relit ; quitter l'org
   l'efface ; `shared` à la création partage ET prête, et le prêt s'ouvre vraiment à un
   membre de l'org (`forfaits_pretes`).
2. Le refus de l'admin : plus de `shared` posé ni appliqué, `private` reste permis, et
   l'agent né avant ne bouge pas.
3. Une personne seule le pose : un jeton de délégation lit, il ne pose pas.
4. Le préréglage ne vit plus dans la fiche : l'écrire là ne consent à rien.
Joué sur une vraie base.
"""
from __future__ import annotations

import asyncio

import psycopg
import pytest

from oto_mcp import verrou_org
from oto_mcp.capabilities import runner_triggers as RT
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
from oto_mcp.capabilities.orgs import partage_par_defaut as P

PROPRIO, COLLEGUE = "preset-proprio", "preset-collegue"
MODELE = "sub:sonnet"


@pytest.fixture
def org_reelle(live, pg_module_dsn, monkeypatch):
    # Ce banc ne parle ni du modèle, ni du runner, ni des clés : seulement du partage.
    for nom in ("famille_declaree", "exige_un_modele", "exige_un_runner", "exige_servi"):
        monkeypatch.setattr(RT._modele, nom, lambda *a, **k: None)
    monkeypatch.setattr(RT._cle_exigee, "exiger_a_la_pose", lambda *a, **k: None)
    monkeypatch.setattr(RT._abonnement, "exiger_a_la_pose", lambda *a, **k: None)
    with psycopg.connect(pg_module_dsn, autocommit=True) as c:
        org = c.execute("INSERT INTO orgs (name) VALUES ('Préréglage') RETURNING id"
                        ).fetchone()[0]
        for m in (PROPRIO, COLLEGUE):
            c.execute("INSERT INTO users (sub) VALUES (%s) ON CONFLICT DO NOTHING", (m,))
            c.execute("INSERT INTO org_members (org_id, sub) VALUES (%s, %s)", (org, m))
    # Un agent ne se pose que sur une procédure que son propriétaire lit par nom (#1499).
    from oto_mcp import org_store
    org_store.set_instruction("org", org, "veille", "Veille.")
    return org


def _appel(org, sub=PROPRIO, **kw):
    return asyncio.run(RT._triggers(ResolvedCtx(sub=sub, org_id=org), RT.TriggerInput(**kw)))


def _creer(org, **kw):
    return _appel(org, op="create", procedure="veille", cron="0 8 * * *", tools=["a"],
                  model=MODELE, **kw)["trigger"]


def test_se_pose_sans_trigger_id_et_se_relit(org_reelle):
    lu = _appel(org_reelle, op="default_sharing")
    assert (lu["default_sharing"], lu["default_sharing_refused"]) == ("private", False)
    assert _appel(org_reelle, op="default_sharing", default_sharing="shared")[
        "default_sharing"] == "shared"
    assert _appel(org_reelle, sub=COLLEGUE, op="default_sharing")["default_sharing"] == "private"
    assert _appel(org_reelle, op="default_sharing", default_sharing="private")[
        "default_sharing"] == "private"


def test_quitter_l_org_l_efface(org_reelle, pg_module_dsn):
    from oto_mcp import db
    assert db.poser_preset_agent_partage(PROPRIO, org_reelle, True)
    assert db.membres_en_preset_partage(org_reelle)[0]["sub"] == PROPRIO
    with psycopg.connect(pg_module_dsn, autocommit=True) as c:
        c.execute("DELETE FROM org_members WHERE org_id = %s AND sub = %s", (org_reelle, PROPRIO))
        c.execute("INSERT INTO org_members (org_id, sub) VALUES (%s, %s)", (org_reelle, PROPRIO))
    assert db.preset_agent_partage(PROPRIO, org_reelle) is False
    assert db.poser_preset_agent_partage("jamais-membre", org_reelle, True) is False


def test_shared_partage_et_prete_vraiment(org_reelle, monkeypatch):
    """Le test de vérité : le prêt collectif s'ouvre, en base, à un membre de l'org."""
    from oto_mcp import db
    monkeypatch.setattr(RT.runner_models, "MODELES_PERSONNELS", {MODELE})
    _appel(org_reelle, op="default_sharing", default_sharing="shared")
    t = _creer(org_reelle)
    assert t["visibility"]["org"] == "editor"
    assert db.forfaits_pretes([t["id"]], COLLEGUE) == {t["id"]}
    assert db.forfaits_pretes([t["id"]], COLLEGUE, nomme=True) == set()

    prive = _creer(org_reelle, private=True)
    assert prive["visibility"]["org"] is None
    assert db.forfaits_pretes([prive["id"]], COLLEGUE) == set()


def test_le_refus_de_l_admin_ne_passe_pas_et_n_est_pas_retroactif(org_reelle, monkeypatch):
    from oto_mcp import db
    monkeypatch.setattr(RT.runner_models, "MODELES_PERSONNELS", {MODELE})
    _appel(org_reelle, op="default_sharing", default_sharing="shared")
    ne_avant = _creer(org_reelle)

    P.poser(org_reelle, {P.PRESET: P.REFUSE})
    with pytest.raises(AuthzDenied) as e:
        _appel(org_reelle, sub=COLLEGUE, op="default_sharing", default_sharing="shared")
    assert (e.value.status, e.value.code) == (403, "agent_preset_refused")
    assert _appel(org_reelle, op="default_sharing")["default_sharing_refused"] is True

    apres = _creer(org_reelle)
    assert apres["visibility"]["org"] is None, "le préréglage posé n'est plus appliqué"
    assert db.forfaits_pretes([ne_avant["id"]], COLLEGUE) == {ne_avant["id"]}, (
        "le refus ne touche à aucun agent né")

    assert _appel(org_reelle, op="default_sharing", default_sharing="private")[
        "default_sharing"] == "private", "on peut toujours se refermer"


def test_un_jeton_de_delegation_lit_mais_ne_pose_pas(org_reelle, monkeypatch):
    monkeypatch.setattr(verrou_org, "travail_de_la_requete", lambda: object())
    assert _appel(org_reelle, op="default_sharing")["default_sharing"] == "private"
    for valeur in ("shared", "private"):
        with pytest.raises(AuthzDenied) as e:
            _appel(org_reelle, op="default_sharing", default_sharing=valeur)
        assert (e.value.status, e.value.code) == (403, "agent_preset_human_only")


def test_le_champ_hors_de_son_op_est_refuse(org_reelle):
    with pytest.raises(AuthzDenied) as e:
        _appel(org_reelle, op="list", default_sharing="shared")
    assert e.value.code == "default_sharing_only_on_its_op"


def test_il_ne_passe_ni_par_la_fiche_ni_par_son_ecriture(org_reelle):
    from oto_mcp import db
    db.update_account_profile(PROPRIO, {"agent_sharing": {str(org_reelle): "shared"}})
    assert _appel(org_reelle, op="default_sharing")["default_sharing"] == "private", (
        "écrire la fiche (`oto_profile`, `PUT /api/me/profile`) ne consent à rien")
    _appel(org_reelle, sub=COLLEGUE, op="default_sharing", default_sharing="shared")
    assert "agent_sharing" not in db.get_account_profile(COLLEGUE)["profile"]
