"""Un compte prêté HORS ORG (#1499, D1), sur une vraie base : une règle nomme une personne
qui n'est pas membre de l'org du compte. Le compte ne sert que NOMMÉ, au niveau de la
règle ; le journal dit la règle hors org ; les travaux de la personne, sous un jeton de
délégation verrouillé sur SON org, s'en servent encore — le verrou interdit d'agir dans
une autre org, pas de nommer un compte qu'une règle lui ouvre.
"""
from __future__ import annotations

import uuid

import pytest

psycopg = pytest.importorskip("psycopg")


@pytest.fixture
def monde(live, monkeypatch):
    from oto_mcp import credentials_store, db
    from oto_mcp.db import regles_de_comptes
    from oto_mcp.org_store import members, orgs
    monkeypatch.setenv("OTO_MCP_MASTER_KEY", "4" * 64)
    u = uuid.uuid4().hex[:6]
    preteur, moi = f"preteur_{u}", f"moi_{u}"
    for sub in (preteur, moi):
        db.upsert_user(sub, email=f"{sub}@x.test", name=sub)
    y = orgs.create_org(f"Preteuse {u}", created_by=preteur)
    x = orgs.create_org(f"Maison {u}", created_by=moi)
    members.add_org_member(y, preteur, "org_member")
    members.add_org_member(x, moi, "org_member")
    credentials_store.set_credential("member", f"{y}:{preteur}", "zoho", "s3cret",
                                     set_by=preteur, meta={}, account="sa-boite")
    inst = regles_de_comptes.comptes_des_lignes(
        "zoho", [("member", f"{y}:{preteur}", "sa-boite")])[0]["instance_id"]
    regles_de_comptes.poser_regle(y, inst, subject_user=moi, level="write", acteur=preteur)
    return {"x": x, "y": y, "preteur": preteur, "moi": moi, "inst": inst}


def test_le_journal_dit_la_regle_hors_org(monde):
    from oto_mcp.db import regles_de_comptes
    j = regles_de_comptes.journal_du_compte(monde["inst"])
    assert [(e["action"], e["subject_id"], e["hors_org"]) for e in j] == [
        ("set", monde["moi"], True)]


def _resoudre(monde, **k):
    from oto_mcp import access
    return access.resolve_credential("zoho", sub=monde["moi"], emit_on_failure=False, **k)


def _dans_l_org(monkeypatch, monde):
    from oto_mcp import access
    monkeypatch.setattr(access, "current_org", lambda sub: monde["x"])
    monkeypatch.setattr(access, "current_group", lambda sub: None)


def test_nomme_il_sert_sans_nom_jamais(monde, monkeypatch):
    from oto_mcp import access
    _dans_l_org(monkeypatch, monde)
    rc = _resoudre(monde, account="sa-boite")
    assert (rc.entity_type, rc.entity_id) == ("member", f"{monde['y']}:{monde['preteur']}")
    with pytest.raises(access.CredentialUnavailable) as e:
        _resoudre(monde)
    assert e.value.error.data["to_name"][0]["lent_by_org"].startswith("Preteuse")


def test_sous_un_jeton_de_delegation_verrouille_sur_son_org(monde, monkeypatch):
    """Un travail de la personne, verrouillé sur l'org X, nomme le compte prêté par Y :
    il sert, dans X. Le pin `_instance=` garde l'org X (il ne la bascule pas vers Y)."""
    from oto_mcp import access, db, instance_refs, verrou_org
    from oto_mcp.capabilities import runner_jobs
    _dans_l_org(monkeypatch, monde)
    job = db.enqueue_job(monde["x"], "start", payload={"procedure": "p", "input": "x"},
                         sub=monde["moi"])
    jeton = runner_jobs._delegue({"id": job["id"], "sub": monde["moi"],
                                  "org_id": monde["x"]}, bail_s=60,
                                 claimant="test")["delegated_token"]
    tok = verrou_org.poser(verrou_org.depuis_ligne(db.verify_api_token(jeton)))
    try:
        rc = _resoudre(monde, account="sa-boite")
        assert rc.entity_id == f"{monde['y']}:{monde['preteur']}"
        ref = instance_refs.parse_ref(instance_refs.make_member_ref(
            monde["y"], monde["preteur"], "zoho", "sa-boite"))
        assert access.guard_instance_access(monde["moi"], ref) == monde["x"]
    finally:
        verrou_org.lever(tok)
