"""Changer le propriétaire d'un compte, et l'auteur du défaut multi-compte (#1499), sur une
vraie base.

- Le transfert garde le secret (la même ligne de coffre, chiffré intact) et l'identifiant
  de l'instance ; il retire les règles qui n'ont plus de sens, perd un défaut que le
  nouveau palier a déjà, et le journal des partages le note.
- Chaque pose du défaut d'un compte Google enregistre son auteur et sa date ; les autres
  comptes du palier perdent défaut et marque.
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


def _ligne(entity_type, entity_id, connector, account=""):
    rows = _rows("SELECT secret_enc, meta FROM connector_credentials WHERE entity_type = %s "
                 "AND entity_id = %s AND connector = %s AND account = %s",
                 (entity_type, str(entity_id), connector, account))
    return rows[0] if rows else None


@pytest.fixture
def monde(live, monkeypatch):
    monkeypatch.setenv("OTO_MCP_MASTER_KEY", "4" * 64)
    from oto_mcp import db, org_store
    u = uuid.uuid4().hex[:8]
    org = org_store.create_org(f"org_{u}")
    proprio, pair = f"p_{u}", f"q_{u}"
    for sub in (proprio, pair):
        db.upsert_user(sub)
        _rows("INSERT INTO org_members (org_id, sub) VALUES (%s, %s) RETURNING org_id",
              (org, sub))
    eq = _rows("INSERT INTO org_groups (org_id, name) VALUES (%s, %s) RETURNING id",
               (org, f"eq_{u}"))[0]["id"]
    return {"org": org, "proprio": proprio, "pair": pair, "eq": eq}


def _poser(entity_type, entity_id, connector, account="", meta=None):
    from oto_mcp import credentials_store
    credentials_store.set_credential(entity_type, str(entity_id), connector, "s3cret",
                                     set_by="test", meta=meta or {}, account=account)
    return _rows("SELECT id FROM connector_instances WHERE owner_type = %s AND owner_id = %s "
                 "AND connector = %s AND account = %s AND revoked_at IS NULL",
                 (entity_type, str(entity_id), connector, account))[0]["id"]


def _journal(inst):
    return [(r["action"], r["subject_kind"], r["subject_id"], r["owner_before"],
             r["actor_sub"])
            for r in _rows("SELECT * FROM connector_account_share_events "
                           "WHERE instance_id = %s ORDER BY id", (inst,))]


# ── Le transfert ────────────────────────────────────────────────────────────────

def test_d_org_a_personne_garde_secret_et_id_et_nettoie_les_regles(monde):
    from oto_mcp.db import regles_de_comptes, transfert_de_compte
    s = monde
    inst = _poser("org", s["org"], "zoho")
    chiffre = _ligne("org", s["org"], "zoho")["secret_enc"]
    regles_de_comptes.poser_regle(s["org"], inst, subject_org=True, level="sensitive",
                                  acteur="admin")
    regles_de_comptes.poser_regle(s["org"], inst, subject_user=s["proprio"], level="read",
                                  acteur="admin")
    regles_de_comptes.poser_regle(s["org"], inst, subject_user=s["pair"], level="write",
                                  acteur="admin")
    vers = f"{s['org']}:{s['proprio']}"
    res = transfert_de_compte.transferer(inst, vers_type="member", vers_id=vers,
                                         acteur="admin")
    assert res == {"instance_id": inst, "owner_before": f"org:{s['org']}",
                   "owner_after": f"member:{vers}", "rules_removed": 2,
                   "default_dropped": False}
    assert _ligne("org", s["org"], "zoho") is None
    assert _ligne("member", vers, "zoho")["secret_enc"] == chiffre
    assert _rows("SELECT owner_type, owner_id FROM connector_instances WHERE id = %s",
                 (inst,)) == [{"owner_type": "member", "owner_id": vers}]
    # Reste la règle du pair ; l'org et le nouveau propriétaire n'ont plus la leur.
    assert [(r["subject_user"], r["subject_org"], r["level"])
            for r in regles_de_comptes.regles_du_compte(inst)] == [(s["pair"], False, "write")]
    journal = _journal(inst)
    assert journal[-3:] == [
        ("removed", "org", None, None, "admin"),
        ("removed", "user", s["proprio"], None, "admin"),
        ("transferred", "user", s["proprio"], f"org:{s['org']}", "admin"),
    ]


def test_de_personne_a_equipe_garde_les_regles(monde):
    from oto_mcp.db import regles_de_comptes, transfert_de_compte
    s = monde
    de = f"{s['org']}:{s['proprio']}"
    inst = _poser("member", de, "attio")
    regles_de_comptes.poser_regle(s["org"], inst, subject_user=s["pair"], level="read",
                                  acteur=s["proprio"])
    res = transfert_de_compte.transferer(inst, vers_type="group", vers_id=str(s["eq"]),
                                         acteur=s["proprio"])
    assert res["rules_removed"] == 0 and res["owner_after"] == f"group:{s['eq']}"
    assert len(regles_de_comptes.regles_du_compte(inst)) == 1
    assert _journal(inst)[-1] == ("transferred", "group", str(s["eq"]), f"member:{de}",
                                  s["proprio"])


def test_le_defaut_tombe_si_le_nouveau_palier_a_le_sien(monde):
    from oto_mcp.db import transfert_de_compte
    s = monde
    _poser("org", s["org"], "zoho", "fr", meta={"is_default": True})
    de = f"{s['org']}:{s['proprio']}"
    inst = _poser("member", de, "zoho", "us",
                  meta={"is_default": True, "default_set_by": s["proprio"]})
    res = transfert_de_compte.transferer(inst, vers_type="org", vers_id=str(s["org"]),
                                         acteur="admin")
    assert res["default_dropped"] is True
    meta = _ligne("org", s["org"], "zoho", "us")["meta"]
    assert meta["is_default"] is False and "default_set_by" not in meta
    assert _ligne("org", s["org"], "zoho", "fr")["meta"]["is_default"] is True


def test_le_defaut_reste_s_il_garde_un_sens(monde):
    from oto_mcp.db import transfert_de_compte
    s = monde
    inst = _poser("org", s["org"], "zoho", "fr", meta={"is_default": True})
    res = transfert_de_compte.transferer(inst, vers_type="group", vers_id=str(s["eq"]),
                                         acteur="admin")
    assert res["default_dropped"] is False
    assert _ligne("group", s["eq"], "zoho", "fr")["meta"]["is_default"] is True


@pytest.mark.parametrize("cas", ["meme_proprietaire", "nom_pris", "inconnu"])
def test_les_refus_ne_touchent_a_rien(monde, cas):
    from oto_mcp.db import transfert_de_compte
    s = monde
    inst = _poser("org", s["org"], "zoho")
    vers = ("org", str(s["org"]))
    attendu = "same_owner"
    if cas == "nom_pris":
        _poser("group", s["eq"], "zoho")
        vers, attendu = ("group", str(s["eq"])), "account_name_taken"
    elif cas == "inconnu":
        inst, attendu = 10 ** 9, "not_found"
    with pytest.raises(transfert_de_compte.TransfertRefuse) as e:
        transfert_de_compte.transferer(inst, vers_type=vers[0], vers_id=vers[1],
                                       acteur="admin")
    assert e.value.code == attendu
    assert _ligne("org", s["org"], "zoho") is not None


def test_une_sous_instance_de_canal_ne_se_transfere_pas(monde):
    from oto_mcp import db
    from oto_mcp.db import transfert_de_compte
    s = monde
    db.set_unipile_account(s["proprio"], "ACC", None, org_id=s["org"], provider="LINKEDIN")
    si = _rows("SELECT id FROM connector_instances WHERE connector = 'unipile' "
               "AND owner_id = %s AND account = 'canal:LINKEDIN'",
               (f"{s['org']}:{s['proprio']}",))[0]["id"]
    with pytest.raises(transfert_de_compte.TransfertRefuse) as e:
        transfert_de_compte.transferer(si, vers_type="org", vers_id=str(s["org"]),
                                       acteur="admin")
    assert e.value.code == "not_transferable"


# ── L'auteur du défaut ──────────────────────────────────────────────────────────

def test_poser_le_defaut_google_marque_l_auteur_et_retire_l_ancien(monde):
    from oto_mcp import db
    s = monde
    for email in ("a@x.test", "b@x.test"):
        db.set_google_oauth(s["proprio"], s["org"], google_email=email,
                               refresh_token="r", scopes="openid")
    ent = f"{s['org']}:{s['proprio']}"
    premier = _ligne("member", ent, "google", "a@x.test")["meta"]
    assert premier["is_default"] is True and premier["default_set_by"] == s["proprio"]
    assert premier["default_set_at"]
    assert db.set_default_google_account(s["proprio"], s["org"], "b@x.test")
    a, b = (_ligne("member", ent, "google", e)["meta"] for e in ("a@x.test", "b@x.test"))
    assert a["is_default"] is False and "default_set_by" not in a
    assert b["is_default"] is True and b["default_set_by"] == s["proprio"]
    # Reconnecter le défaut garde sa marque.
    db.set_google_oauth(s["proprio"], s["org"], google_email="b@x.test",
                           refresh_token="r2", scopes="openid")
    assert _ligne("member", ent, "google", "b@x.test")["meta"]["default_set_by"] == s["proprio"]


def test_le_defaut_partage_porte_l_admin_qui_le_pose(monde):
    from oto_mcp import db
    s = monde
    for email in ("a@x.test", "b@x.test"):
        db.set_shared_google_oauth("org", s["org"], set_by="admin1", google_email=email,
                                   refresh_token="r", scopes="openid")
    assert _ligne("org", s["org"], "google", "a@x.test")["meta"]["default_set_by"] == "admin1"
    assert db.set_default_shared_google_account("org", s["org"], "b@x.test", par="admin2")
    assert _ligne("org", s["org"], "google", "b@x.test")["meta"]["default_set_by"] == "admin2"
    db.delete_shared_google_oauth("org", s["org"], account="b@x.test", par="admin3")
    a = _ligne("org", s["org"], "google", "a@x.test")["meta"]
    assert a["is_default"] is True and a["default_set_by"] == "admin3"


def test_un_defaut_sans_auteur_tranche_encore():
    """Un défaut d'avant #1499 n'a pas d'auteur : il a été posé sous les gardes de
    gestion (admin d'org, responsable d'équipe, la personne), il tranche toujours (D3)."""
    from oto_mcp.access import niveaux
    a = niveaux.Compte(1, "org", "7", "zoho", "fr", 7, is_default=True)
    b = niveaux.Compte(2, "org", "7", "zoho", "us", 7)
    assert niveaux.defaut_qui_tranche([a, b]) is a
