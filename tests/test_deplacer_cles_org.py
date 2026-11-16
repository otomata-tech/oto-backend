"""Le script qui déplace des clés d'org vers les clés personnelles d'un membre ou vers une
équipe dédiée, contre un vrai PostgreSQL (`scripts/deplacer_cles_org.py`).

Ce qui coûterait le plus cher à rater, dans l'ordre :
1. **un secret affiché** — aucune sortie ni aucun log ne porte la valeur ;
2. **une clé perdue ou transplantée** — relue au niveau membre à l'identique,
   indéchiffrable avec le sceau de l'org, ligne d'org partie, et rien du tout si la
   relecture échoue (ROLLBACK) ;
3. **une clé du membre écrasée** — jamais : renommage côté clé déplacée, ou refus ;
4. **un objet qui casse en silence** — un lien de projet qui désigne l'instance d'org
   fait refuser `--apply`, sauf option explicite.
"""
from __future__ import annotations

import logging
import os
import pathlib
import sys

import pytest

from oto_mcp import credentials_store as cs, crypto, instance_refs

# La racine du dépôt n'est pas dans `sys.path` en CI (pas de `__init__.py`) : `scripts`
# ne s'importait que parce qu'un banc collecté avant l'y avait mise (oto#116).
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

ORG = 7301
AUTRE_ORG = 7302
CIBLE = "usr_cible"
ADMIN = "usr_admin"
MEMBRE = "usr_membre"
ETRANGER = "usr_etranger"
SECRET = "sk-FICTIF-ne-doit-jamais-sortir-9f3a"
MULTI = "hunter"          # multi-compte, clé perso ET clé d'org
MONO = "pennylaneged"     # mono-compte, clé perso ET clé d'org
ORG_SEULE = "resend"     # clé d'org seulement : pas de clé personnelle


def _exec(sql, params=()):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute(sql, params)


def _one(sql, params=()):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        return conn.execute(sql, params).fetchone()


def _poser(entity_type, entity_id, connector, account, secret=SECRET, meta=None, set_by=ADMIN):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        cs._upsert(conn, entity_type, str(entity_id), connector, account, secret,
                   set_by, meta or {"note": "posée par le banc"})


def _ligne(entity_type, entity_id, connector, account):
    return _one("SELECT secret_enc, meta, set_by FROM connector_credentials "
                "WHERE entity_type=%s AND entity_id=%s AND connector=%s AND account=%s",
                (entity_type, str(entity_id), connector, account))


@pytest.fixture(scope="module")
def base(live):
    avant = os.environ.get("OTO_MCP_MASTER_KEY")
    os.environ.setdefault("OTO_MCP_MASTER_KEY", "0" * 64)
    for org in (ORG, AUTRE_ORG):
        _exec("INSERT INTO orgs (id, name) VALUES (%s, 'o') ON CONFLICT DO NOTHING", (org,))
    for s in (CIBLE, ADMIN, MEMBRE, ETRANGER):
        _exec("INSERT INTO users (sub) VALUES (%s) ON CONFLICT DO NOTHING", (s,))
    for s, role in ((CIBLE, "org_member"), (ADMIN, "org_admin"), (MEMBRE, "org_member")):
        _exec("INSERT INTO org_members (org_id, sub, org_role) VALUES (%s, %s, %s) "
              "ON CONFLICT DO NOTHING", (ORG, s, role))
    _exec("INSERT INTO org_members (org_id, sub, org_role) VALUES (%s, %s, 'org_member') "
          "ON CONFLICT DO NOTHING", (AUTRE_ORG, ETRANGER))
    yield
    if avant is None:
        os.environ.pop("OTO_MCP_MASTER_KEY", None)


@pytest.fixture
def propre(base):
    _exec("DELETE FROM connector_credentials WHERE entity_type IN ('org','member','group')")
    _exec("DELETE FROM project_links")
    _exec("DELETE FROM org_groups WHERE org_id = %s", (ORG,))
    _exec("UPDATE org_members SET is_active = (org_id = %s) WHERE org_id IN (%s, %s)",
          (ORG, ORG, AUTRE_ORG))
    yield


def _lancer(*connectors, **kw):
    from scripts.deplacer_cles_org import deplacer
    sortie = []
    code = deplacer(kw.pop("org", ORG), kw.pop("sub", CIBLE), list(connectors),
                    out=sortie.append, **kw)
    return code, "\n".join(sortie)


def test_dry_run_rejoue_puis_annule(propre):
    _poser(cs.ORG, ORG, MULTI, "")
    code, sortie = _lancer(MULTI)
    assert code == 0, sortie
    assert "ANNULÉ" in sortie
    assert _ligne(cs.ORG, ORG, MULTI, "") is not None
    assert _ligne(cs.MEMBER, cs.member_id(ORG, CIBLE), MULTI, "") is None


def test_apply_deplace_et_garde_meta_et_set_by(propre):
    _poser(cs.ORG, ORG, MULTI, "", meta={"note": "gardée"}, set_by=CIBLE)
    code, sortie = _lancer(MULTI, apply=True)
    assert code == 0, sortie
    eid = cs.member_id(ORG, CIBLE)
    row = _ligne(cs.MEMBER, eid, MULTI, "")
    assert row is not None and _ligne(cs.ORG, ORG, MULTI, "") is None
    assert row["set_by"] == CIBLE
    assert row["meta"]["note"] == "gardée"
    assert row["meta"]["_deplacement"]["from"] == f"org:{ORG}"
    # Relu à l'identique au niveau membre, et INDÉCHIFFRABLE avec le sceau de l'org.
    assert crypto.decrypt(row["secret_enc"], cs._aad(cs.MEMBER, eid, MULTI, "")) == SECRET
    with pytest.raises(Exception):
        crypto.decrypt(row["secret_enc"], cs._aad(cs.ORG, str(ORG), MULTI, ""))


def test_instance_d_org_archivee_et_instance_membre_nee(propre):
    from oto_mcp.db import connector_instances as ci
    _poser(cs.ORG, ORG, MULTI, "")
    avant = ci.instance_id_for_vault_row(cs.ORG, str(ORG), MULTI, "")
    assert avant is not None
    assert _lancer(MULTI, apply=True)[0] == 0
    assert ci.instance_id_for_vault_row(cs.ORG, str(ORG), MULTI, "") is None
    assert ci.instance_id_for_vault_row(cs.MEMBER, cs.member_id(ORG, CIBLE), MULTI, "") not in (None, avant)


def test_collision_multi_compte_renomme_la_cle_deplacee(propre):
    eid = cs.member_id(ORG, CIBLE)
    _poser(cs.MEMBER, eid, MULTI, "principal", secret="cle-perso-intacte")
    _poser(cs.ORG, ORG, MULTI, "")
    code, sortie = _lancer(MULTI, apply=True)
    assert code == 0, sortie
    assert "renommé" in sortie
    perso = _ligne(cs.MEMBER, eid, MULTI, "principal")
    assert crypto.decrypt(perso["secret_enc"], cs._aad(cs.MEMBER, eid, MULTI, "principal")) \
        == "cle-perso-intacte"
    deplacee = _ligne(cs.MEMBER, eid, MULTI, f"principal-org-{ORG}")
    assert deplacee is not None


def test_collision_mono_compte_refuse_sans_rien_ecrire(propre):
    eid = cs.member_id(ORG, CIBLE)
    _poser(cs.MEMBER, eid, MONO, "", secret="cle-perso-intacte")
    _poser(cs.ORG, ORG, MONO, "")
    code, sortie = _lancer(MONO, apply=True)
    assert code == 3, sortie
    assert "REFUS" in sortie
    assert _ligne(cs.ORG, ORG, MONO, "") is not None


def test_connecteur_sans_cle_perso_ou_sans_cle_d_org_refuse_tout(propre):
    _poser(cs.ORG, ORG, MULTI, "")
    code, sortie = _lancer(MULTI, ORG_SEULE, "apify", apply=True)
    assert code == 3, sortie
    assert f"REFUS {ORG_SEULE}" in sortie and "REFUS apify" in sortie
    # Échec d'ensemble : même la clé déplaçable reste où elle était.
    assert _ligne(cs.ORG, ORG, MULTI, "") is not None


def test_sub_hors_de_l_org_refuse(propre):
    _poser(cs.ORG, ORG, MULTI, "")
    code, sortie = _lancer(MULTI, sub=ETRANGER, apply=True)
    assert code == 2, sortie
    assert _ligne(cs.ORG, ORG, MULTI, "") is not None


def test_lien_de_projet_sur_l_instance_refuse_sans_option(propre):
    _poser(cs.ORG, ORG, MULTI, "")
    _exec("INSERT INTO projects (id, name, owner_type, owner_id) VALUES (91001, 'p', 'org', %s) "
          "ON CONFLICT DO NOTHING", (str(ORG),))
    ref = instance_refs.make_org_ref(ORG, MULTI, "")
    _exec("INSERT INTO project_links (project_id, target_type, target_ref, config) "
          "VALUES (91001, 'connecteur', %s, jsonb_build_object('instance_ref', %s::text))",
          (MULTI, ref))
    code, sortie = _lancer(MULTI, apply=True)
    assert code == 3, sortie
    assert "liens de projet 1" in sortie
    assert _ligne(cs.ORG, ORG, MULTI, "") is not None
    code, sortie = _lancer(MULTI, apply=True, force_orphan_bindings=True)
    assert code == 0, sortie


def test_impact_compte_les_membres_qui_perdent(propre):
    _poser(cs.ORG, ORG, MULTI, "")
    code, sortie = _lancer(MULTI, show_members=True)
    assert code == 0, sortie
    assert "autres membres de l'org : 2" in sortie
    assert f"{MULTI} : 2 membre(s)" in sortie
    assert f"membre : {ADMIN}" in sortie


def test_relecture_en_echec_annule_tout(propre, monkeypatch):
    import scripts.deplacer_cles_org as script
    _poser(cs.ORG, ORG, MULTI, "")
    empreintes = iter(["a", "b"])            # l'écriture et la relecture divergent
    monkeypatch.setattr(script, "_empreinte", lambda _secret: next(empreintes))
    code, sortie = _lancer(MULTI, apply=True)
    assert code == 4, sortie
    assert "ROLLBACK" in sortie
    assert _ligne(cs.ORG, ORG, MULTI, "") is not None
    assert _ligne(cs.MEMBER, cs.member_id(ORG, CIBLE), MULTI, "") is None


def test_aucun_secret_dans_la_sortie_ni_les_logs(propre, caplog, capsys):
    _poser(cs.ORG, ORG, MULTI, "")
    caplog.set_level(logging.DEBUG)
    from scripts.deplacer_cles_org import main
    assert main(["--org", str(ORG), "--sub", CIBLE, "--connectors", MULTI,
                 "--vers", "membre", "--show-members"]) == 0
    assert main(["--org", str(ORG), "--sub", CIBLE, "--connectors", MULTI,
                 "--vers", "membre", "--apply"]) == 0
    capture = capsys.readouterr()
    for texte in (capture.out, capture.err, caplog.text):
        assert SECRET not in texte


# --- vers une ÉQUIPE dédiée ---------------------------------------------------

NON_LUE_EN_EQUIPE = "crunchbase"   # byo_user, pas org-partageable : jamais lue en équipe
ORG_ONLY = "linear"                # org-partageable, pas byo_user : lue en équipe
EQUIPE = "Clés réservées"


def _equipe():
    return _one("SELECT id FROM org_groups WHERE org_id = %s AND name = %s", (ORG, EQUIPE))


def _membres_equipe(gid):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        return {r["sub"]: r["group_role"] for r in conn.execute(
            "SELECT sub, group_role FROM org_group_members WHERE group_id = %s", (gid,))}


def _vers_equipe(*connectors, **kw):
    kw.setdefault("membres", [CIBLE, ADMIN])
    return _lancer(*connectors, vers="equipe", equipe_nom=EQUIPE, **kw)


def test_equipe_passe_a_blanc_ne_cree_rien(propre):
    _poser(cs.ORG, ORG, MULTI, "")
    code, sortie = _vers_equipe(MULTI)
    assert code == 0, sortie
    assert "ANNULÉ" in sortie and "à créer" in sortie
    assert _equipe() is None
    assert _ligne(cs.ORG, ORG, MULTI, "") is not None


def test_equipe_apply_cree_l_equipe_avec_les_membres_designes_et_deplace(propre):
    _poser(cs.ORG, ORG, MULTI, "", meta={"note": "gardée"}, set_by=CIBLE)
    _poser(cs.ORG, ORG, ORG_ONLY, "")
    code, sortie = _vers_equipe(MULTI, ORG_ONLY, apply=True)
    assert code == 0, sortie
    gid = _equipe()["id"]
    assert _membres_equipe(gid) == {CIBLE: "group_admin", ADMIN: "group_member"}
    for connector in (MULTI, ORG_ONLY):
        row = _ligne("group", gid, connector, "")
        assert row is not None and _ligne(cs.ORG, ORG, connector, "") is None
        assert crypto.decrypt(row["secret_enc"], cs._aad("group", str(gid), connector, "")) == SECRET
        with pytest.raises(Exception):
            crypto.decrypt(row["secret_enc"], cs._aad(cs.ORG, str(ORG), connector, ""))
    row = _ligne("group", gid, MULTI, "")
    assert row["set_by"] == CIBLE and row["meta"]["note"] == "gardée"
    assert row["meta"]["_deplacement"]["to"] == "equipe"


def test_equipe_n_embarque_que_la_liste_explicite(propre):
    """Ni l'org_admin ni tout membre d'office : la liste, et elle seule (#1169)."""
    _poser(cs.ORG, ORG, MULTI, "")
    code, sortie = _vers_equipe(MULTI, apply=True, membres=[MEMBRE, CIBLE])
    assert code == 0, sortie
    assert _membres_equipe(_equipe()["id"]) == {CIBLE: "group_admin", MEMBRE: "group_member"}
    assert "hors de l'équipe : 1 (perdront ces clés)" in sortie


def test_equipe_sans_membres_refuse_en_le_nommant(propre):
    """Sans la table qui disait qui était autorisé, le script ne devine pas : il
    n'ouvre pas l'équipe à toute l'org en silence (#1169)."""
    _poser(cs.ORG, ORG, MULTI, "")
    code, sortie = _vers_equipe(MULTI, apply=True, membres=None)
    assert code == 2, sortie
    assert "--vers equipe exige --membres" in sortie and "connector_acl" in sortie
    assert _equipe() is None
    assert _ligne(cs.ORG, ORG, MULTI, "") is not None
    from scripts.deplacer_cles_org import main
    assert main(["--org", str(ORG), "--sub", CIBLE, "--connectors", MULTI, "--vers",
                 "equipe", "--equipe-nom", EQUIPE, "--apply"]) == 2
    assert _equipe() is None


def test_equipe_membre_hors_de_l_org_refuse(propre):
    _poser(cs.ORG, ORG, MULTI, "")
    code, sortie = _vers_equipe(MULTI, apply=True, membres=[ADMIN, ETRANGER])
    assert code == 2, sortie
    assert "pas membres de l'org" in sortie and ETRANGER in sortie
    assert _equipe() is None
    assert _ligne(cs.ORG, ORG, MULTI, "") is not None


def test_membres_refuse_hors_du_mode_equipe(propre):
    _poser(cs.ORG, ORG, MULTI, "")
    code, sortie = _lancer(MULTI, apply=True, membres=[ADMIN])
    assert code == 2, sortie
    assert _ligne(cs.ORG, ORG, MULTI, "") is not None


def test_equipe_connecteur_non_lu_au_palier_equipe_refuse_tout(propre):
    _poser(cs.ORG, ORG, MULTI, "")
    _poser(cs.ORG, ORG, NON_LUE_EN_EQUIPE, "")
    code, sortie = _vers_equipe(MULTI, NON_LUE_EN_EQUIPE, apply=True)
    assert code == 3, sortie
    assert f"REFUS {NON_LUE_EN_EQUIPE}" in sortie
    assert _equipe() is None
    assert _ligne(cs.ORG, ORG, MULTI, "") is not None


def test_equipe_deja_existante_refuse(propre):
    _poser(cs.ORG, ORG, MULTI, "")
    _exec("INSERT INTO org_groups (org_id, name) VALUES (%s, %s)", (ORG, EQUIPE.upper()))
    code, sortie = _vers_equipe(MULTI, apply=True)
    assert code == 3, sortie
    assert "REFUS équipe" in sortie
    assert _ligne(cs.ORG, ORG, MULTI, "") is not None


def test_equipe_relecture_en_echec_annule_aussi_la_creation(propre, monkeypatch):
    import scripts.deplacer_cles_org as script
    _poser(cs.ORG, ORG, MULTI, "")
    empreintes = iter(["a", "b"])
    monkeypatch.setattr(script, "_empreinte", lambda _secret: next(empreintes))
    code, sortie = _vers_equipe(MULTI, apply=True)
    assert code == 4, sortie
    assert _equipe() is None
    assert _ligne(cs.ORG, ORG, MULTI, "") is not None


def test_equipe_la_cle_n_est_lue_que_dans_l_equipe_active(propre):
    """Point 1 : un membre de l'équipe résout la clé d'équipe quand l'équipe est
    son équipe ACTIVE ; sans elle (équipe neuve, pas `--rendre-active`), il ne la
    résout pas ; un non-membre ne la résout jamais."""
    from oto_mcp.access import scope
    _poser(cs.ORG, ORG, MULTI, "")
    assert _vers_equipe(MULTI, apply=True)[0] == 0
    gid = _equipe()["id"]
    assert scope.current_group(CIBLE) is None          # équipe neuve : active de personne
    _exec("DELETE FROM org_group_members WHERE group_id = %s", (gid,))
    _exec("DELETE FROM org_groups WHERE id = %s", (gid,))
    _poser(cs.ORG, ORG, MULTI, "")
    code, sortie = _vers_equipe(MULTI, apply=True, rendre_active=True)
    assert code == 0, sortie
    gid = _equipe()["id"]
    assert "équipe rendue active pour 2 membre(s)" in sortie
    for s in (CIBLE, ADMIN):
        assert scope.current_group(s) == gid
        from oto_mcp import group_store
        assert group_store.get_group_secret(scope.current_group(s), MULTI) == SECRET
    assert scope.current_group(MEMBRE) is None         # hors équipe : jamais la clé


def test_rendre_active_ne_touche_pas_une_autre_equipe_active(propre):
    _poser(cs.ORG, ORG, MULTI, "")
    autre = _one("INSERT INTO org_groups (org_id, name) VALUES (%s, 'autre') RETURNING id",
                 (ORG,))["id"]
    _exec("INSERT INTO org_group_members (group_id, sub, group_role, is_active) "
          "VALUES (%s, %s, 'group_member', TRUE)", (autre, ADMIN))
    code, sortie = _vers_equipe(MULTI, apply=True, rendre_active=True)
    assert code == 0, sortie
    assert "autre équipe 1" in sortie
    from oto_mcp.access import scope
    assert scope.current_group(ADMIN) == autre         # laissé tel quel, compté


def test_equipe_aucun_secret_dans_la_sortie_ni_les_logs(propre, caplog, capsys):
    _poser(cs.ORG, ORG, MULTI, "")
    caplog.set_level(logging.DEBUG)
    from scripts.deplacer_cles_org import main
    base = ["--org", str(ORG), "--sub", CIBLE, "--connectors", MULTI,
            "--vers", "equipe", "--equipe-nom", EQUIPE, "--membres", f"{CIBLE},{ADMIN}",
            "--show-members"]
    assert main(base) == 0
    assert main(base + ["--rendre-active", "--apply"]) == 0
    capture = capsys.readouterr()
    for texte in (capture.out, capture.err, caplog.text):
        assert SECRET not in texte


def test_partage_par_defaut_n_est_pas_compte(propre):
    """`share_mode='open'` est la valeur PAR DÉFAUT : ce n'est pas un partage. La
    première version du script comptait chaque ligne (« 14 partages » sur 14)."""
    _poser(cs.ORG, ORG, MULTI, "")
    code, sortie = _lancer(MULTI)
    assert code == 0, sortie
    assert "partage d'instance" not in sortie
    _exec("UPDATE connector_credentials SET share_side = '[\"user:x\"]'::jsonb "
          "WHERE entity_type = 'org' AND entity_id = %s AND connector = %s", (str(ORG), MULTI))
    code, sortie = _lancer(MULTI)
    assert "partage d'instance non repris sur 1 ligne(s)" in sortie
