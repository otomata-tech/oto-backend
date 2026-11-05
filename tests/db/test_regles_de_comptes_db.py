"""Les règles d'accès des comptes (#1499, étape 3, L1), sur une vraie base.

Ce que la base porte : les contraintes (sujet unique, niveau, une règle par compte et
sujet, `owner_subject`), le journal écrit par déclencheur, la sous-instance d'un compte
Unipile posée à sa connexion, la fusion de deux comptes et la suppression d'une équipe ou
d'une org. La reprise des anciens partages est celle de la révision de la bascule
(`tests/test_migration_20261105_bascule_comptes.py`).
"""
from __future__ import annotations

import uuid

import pytest

psycopg = pytest.importorskip("psycopg")


def _c():
    import os
    from psycopg.rows import dict_row
    return psycopg.connect(os.environ["DATABASE_URL"], autocommit=True, row_factory=dict_row)


def _rows(sql, params=()):
    with _c() as c:
        return c.execute(sql, params).fetchall()


def _exec(sql, params=()):
    with _c() as c:
        return c.execute(sql, params)


def _org(nom):
    from oto_mcp import org_store
    return org_store.create_org(nom)


def _membre(org, sub):
    from oto_mcp import db
    db.upsert_user(sub)
    _exec("INSERT INTO org_members (org_id, sub) VALUES (%s, %s) ON CONFLICT DO NOTHING",
          (org, sub))


def _groupe(org, nom):
    return _rows("INSERT INTO org_groups (org_id, name) VALUES (%s, %s) RETURNING id",
                 (org, nom))[0]["id"]


def _compte(owner_type, owner_id, connector, account=""):
    """Une ligne de coffre (secret factice) et son instance."""
    _exec("INSERT INTO connector_credentials (entity_type, entity_id, connector, account, "
          "secret_enc) VALUES (%s, %s, %s, %s, 'x')", (owner_type, owner_id, connector, account))
    return _rows("INSERT INTO connector_instances (connector, owner_type, owner_id, account) "
                 "VALUES (%s, %s, %s, %s) RETURNING id",
                 (connector, owner_type, owner_id, account))[0]["id"]


def _regle(org, instance, *, user=None, group=None, org_entiere=False, level="read",
           origine="manuelle", created_by=None, conn=None):
    sql = ("INSERT INTO connector_account_rules (org_id, instance_id, subject_user, "
           "subject_group, subject_org, level, origine, created_by) "
           "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id")
    params = (org, instance, user, group, org_entiere, level, origine, created_by)
    if conn is not None:
        return conn.execute(sql, params).fetchone()["id"]
    return _rows(sql, params)[0]["id"]


@pytest.fixture
def u():
    return uuid.uuid4().hex[:8]


@pytest.fixture
def org_et_compte(live, u):
    org = _org(f"org_{u}")
    proprio, pair = f"proprio_{u}", f"pair_{u}"
    _membre(org, proprio)
    _membre(org, pair)
    inst = _compte("member", f"{org}:{proprio}", "zoho")
    return {"org": org, "proprio": proprio, "pair": pair, "inst": inst}


# ── Contraintes ─────────────────────────────────────────────────────────────────

def test_une_regle_a_exactement_un_sujet(org_et_compte):
    s = org_et_compte
    with pytest.raises(psycopg.errors.CheckViolation):
        _regle(s["org"], s["inst"])
    with pytest.raises(psycopg.errors.CheckViolation):
        _regle(s["org"], s["inst"], user=s["pair"], org_entiere=True)


def test_le_niveau_est_borne(org_et_compte):
    s = org_et_compte
    with pytest.raises(psycopg.errors.CheckViolation):
        _regle(s["org"], s["inst"], user=s["pair"], level="admin")
    for niveau in ("none", "read", "write", "sensitive", "manage"):
        rid = _regle(s["org"], s["inst"], user=s["pair"], level=niveau)
        _exec("DELETE FROM connector_account_rules WHERE id = %s", (rid,))


def test_une_regle_par_compte_et_sujet_org_comprise(org_et_compte):
    s = org_et_compte
    _regle(s["org"], s["inst"], user=s["pair"])
    with pytest.raises(psycopg.errors.UniqueViolation):
        _regle(s["org"], s["inst"], user=s["pair"], level="write")
    _regle(s["org"], s["inst"], org_entiere=True)
    with pytest.raises(psycopg.errors.UniqueViolation):
        _regle(s["org"], s["inst"], org_entiere=True)


def test_une_regle_qui_nomme_le_proprietaire_est_refusee(org_et_compte):
    s = org_et_compte
    with pytest.raises(psycopg.errors.CheckViolation) as e:
        _regle(s["org"], s["inst"], user=s["proprio"])
    assert e.value.diag.constraint_name == "connector_account_rules_owner_subject"
    rid = _regle(s["org"], s["inst"], user=s["pair"])
    with pytest.raises(psycopg.errors.CheckViolation):
        _exec("UPDATE connector_account_rules SET subject_user = %s WHERE id = %s",
              (s["proprio"], rid))


# ── Le journal ──────────────────────────────────────────────────────────────────

def _journal(inst):
    return [(r["action"], r["subject_kind"], r["subject_id"], r["level"],
             r["previous_level"], r["services"], r["actor_sub"])
            for r in _rows("SELECT * FROM connector_account_share_events "
                           "WHERE instance_id = %s ORDER BY id", (inst,))]


def test_le_journal_suit_chaque_geste_et_son_auteur(org_et_compte):
    s = org_et_compte
    rid = _regle(s["org"], s["inst"], user=s["pair"], created_by=s["proprio"])
    _exec("UPDATE connector_account_rules SET updated_at = NOW() WHERE id = %s", (rid,))
    with _c() as c, c.transaction():
        c.execute("SELECT set_config('oto.acteur', %s, true)", ("admin_x",))
        c.execute("UPDATE connector_account_rules SET level = 'write' WHERE id = %s", (rid,))
    _exec("UPDATE connector_account_rules SET services = ARRAY['gmail'] WHERE id = %s",
          (rid,))
    _exec("DELETE FROM connector_account_rules WHERE id = %s", (rid,))
    assert _journal(s["inst"]) == [
        ("set", "user", s["pair"], "read", None, None, s["proprio"]),
        ("set", "user", s["pair"], "write", "read", None, "admin_x"),
        ("set", "user", s["pair"], "write", "write", ["gmail"], None),
        ("removed", "user", s["pair"], "write", None, ["gmail"], None),
    ]


def test_la_suppression_d_une_equipe_emporte_ses_regles_et_le_dit(org_et_compte, u):
    from oto_mcp import group_store
    s = org_et_compte
    g = _groupe(s["org"], f"eq_{u}")
    _regle(s["org"], s["inst"], group=g)
    group_store.delete_group(g)
    assert _rows("SELECT 1 FROM connector_account_rules WHERE subject_group = %s", (g,)) == []
    assert _journal(s["inst"])[-1][:3] == ("removed", "group", str(g))


def test_la_suppression_d_une_org_emporte_ses_regles_sans_journal(org_et_compte):
    s = org_et_compte
    _regle(s["org"], s["inst"], org_entiere=True)
    _exec("DELETE FROM orgs WHERE id = %s", (s["org"],))
    assert _rows("SELECT 1 FROM connector_account_rules WHERE org_id = %s", (s["org"],)) == []
    assert [e[0] for e in _journal(s["inst"])] == ["set"]


# ── La sous-instance d'un compte Unipile ───────────────────────────────────────

def _sous_instance(org, sub, provider="LINKEDIN"):
    rows = _rows("SELECT id, parent_id FROM connector_instances WHERE owner_type = 'member' "
                 "AND owner_id = %s AND connector = 'unipile' AND account = %s "
                 "AND revoked_at IS NULL", (f"{org}:{sub}", f"canal:{provider}"))
    return rows[0] if rows else None


def test_connecter_un_compte_unipile_pose_sa_sous_instance(live, u):
    from oto_mcp import db
    org = _org(f"org_{u}")
    sub = f"lk_{u}"
    _membre(org, sub)
    cle = _compte("org", str(org), "unipile")
    db.set_unipile_account(sub, "ACC1", "Nom", org_id=org, provider="LINKEDIN")
    si = _sous_instance(org, sub)
    assert si is not None and si["parent_id"] == cle
    # Reconnecter ne double rien ; une maintenance des orphelines ne l'archive pas.
    db.set_unipile_account(sub, "ACC2", "Nom", org_id=org, provider="LINKEDIN")
    assert _sous_instance(org, sub)["id"] == si["id"]
    assert si["id"] not in {r["id"] for r in db.list_orphan_instances()}


def test_un_siege_plateforme_a_pour_parent_la_cle_plateforme_unique(live, u):
    from oto_mcp import db
    org = _org(f"org_{u}")
    sub = f"seat_{u}"
    _membre(org, sub)
    _exec("UPDATE connector_instances SET revoked_at = NOW() WHERE owner_type = 'platform' "
          "AND connector = 'unipile'")
    plateforme = _compte("platform", f"cle_{u}", "unipile")
    db.set_unipile_account(sub, "ACC9", None, org_id=org, provider="WHATSAPP",
                           platform_seat=True)
    assert _sous_instance(org, sub, "WHATSAPP")["parent_id"] == plateforme


# ── La fusion de deux comptes ───────────────────────────────────────────────────

def test_la_fusion_repointe_sujet_auteur_et_sous_instance(live, u):
    from oto_mcp import db
    org = _org(f"org_{u}")
    proprio, ancien, nouveau = f"p_{u}", f"old_{u}", f"new_{u}"
    for sub in (proprio, ancien, nouveau):
        _membre(org, sub)
    inst = _compte("member", f"{org}:{proprio}", "zoho")
    deux = _compte("member", f"{org}:{proprio}", "attio")
    _regle(org, inst, user=ancien, created_by=ancien)
    # Les deux comptes de la personne nommés sur le même compte : le survivant garde la sienne.
    _regle(org, deux, user=ancien, level="read")
    _regle(org, deux, user=nouveau, level="write")
    db.set_unipile_account(ancien, "ACC_OLD", None, org_id=org, provider="TELEGRAM")
    si = _sous_instance(org, ancien, "TELEGRAM")["id"]
    assert db.migrate_sub(ancien, nouveau, operator_source="test")
    assert _rows("SELECT subject_user, created_by FROM connector_account_rules "
                 "WHERE instance_id = %s", (inst,)) == [
        {"subject_user": nouveau, "created_by": nouveau}]
    assert _rows("SELECT subject_user, level FROM connector_account_rules "
                 "WHERE instance_id = %s", (deux,)) == [
        {"subject_user": nouveau, "level": "write"}]
    assert _sous_instance(org, nouveau, "TELEGRAM")["id"] == si
