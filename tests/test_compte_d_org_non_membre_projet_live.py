"""Le compte d'une org, ouvert par une règle qui NOMME une personne hors de cette org
(#1499 : D1, ou l'héritage d'un projet partagé repris, #480), sur une vraie base.

Sous `_project=` d'un projet de cette org, l'org de l'appel est l'org du compte mais la
personne n'en est pas membre : le palier org lui est fermé (`scope.org_d_appartenance`).
Le compte sert alors à l'étape 5, dans le groupe du prêt de la même org, sans nom et au
niveau exigé, ou nommé ; la réponse et le statut disent `lent_by_org`. Sans règle, il ne
sert pas. Un membre reste servi au palier org.
"""
from __future__ import annotations

import uuid

import pytest

psycopg = pytest.importorskip("psycopg")


@pytest.fixture
def monde(live, monkeypatch):
    from oto_mcp import access, credentials_store, db, session_org
    from oto_mcp.db import regles_de_comptes
    from oto_mcp.org_store import members, orgs
    monkeypatch.setenv("OTO_MCP_MASTER_KEY", "4" * 64)
    u = uuid.uuid4().hex[:6]
    membre, dehors = f"membre_{u}", f"dehors_{u}"
    for sub in (membre, dehors):
        db.upsert_user(sub, email=f"{sub}@x.test", name=sub)
    y = orgs.create_org(f"Preteuse {u}", created_by=membre)
    x = orgs.create_org(f"Maison {u}", created_by=dehors)
    members.add_org_member(y, membre, "org_member")
    members.add_org_member(x, dehors, "org_member")
    credentials_store.set_credential("org", str(y), "zoho", "s3cret", set_by=membre,
                                     meta={}, account="boite-org")
    inst = regles_de_comptes.comptes_des_lignes(
        "zoho", [("org", str(y), "boite-org")])[0]["instance_id"]
    # L'appel porte `_project=` d'un projet de Y : l'org de l'appel est Y, la personne
    # hors de Y (ce que `call_axes._pin_project` pose).
    monkeypatch.setattr(access, "current_org", lambda sub: y)
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    jeton = session_org.set_call_hors_org(dehors, y)
    yield {"x": x, "y": y, "membre": membre, "dehors": dehors, "inst": inst}
    session_org.reset_call_hors_org(jeton)


def _resoudre(sub, **k):
    from oto_mcp.mcp_errors import McpError
    from oto_mcp import access
    try:
        return access.resolve_credential("zoho", sub=sub, emit_on_failure=False, **k)
    except McpError:
        return None


def _regle_de_reprise(monde):
    """La règle que la reprise d'héritage pose (`regles_d_heritage`) : `sensitive`,
    origine `reprise`, sur le compte d'org, pour le bénéficiaire hors org."""
    import os
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as c:
        c.execute("INSERT INTO connector_account_rules (org_id, instance_id, subject_user, "
                  "level, origine) VALUES (%s, %s, %s, 'sensitive', 'reprise')",
                  (monde["y"], monde["inst"], monde["dehors"]))


def _sert_sans_nom_et_nomme(monde):
    from oto_mcp import session_org
    compte_d_org = ("org", str(monde["y"]), "boite-org")
    trace: dict = {}
    tok = session_org.set_call_trace(trace)
    try:
        rc = _resoudre(monde["dehors"])
    finally:
        session_org.reset_call_trace(tok)
    assert rc is not None and (rc.entity_type, rc.entity_id, rc.account) == compte_d_org
    assert trace["compte_prete"]["account"] == "boite-org"
    assert trace["compte_prete"]["lent_by_org"].startswith("Preteuse")
    rc = _resoudre(monde["dehors"], account="boite-org")
    assert rc is not None and (rc.entity_type, rc.entity_id, rc.account) == compte_d_org


def test_le_beneficiaire_hors_org_d_une_reprise_d_heritage_est_servi(monde):
    _regle_de_reprise(monde)
    _sert_sans_nom_et_nomme(monde)


def test_une_regle_hors_org_posee_a_la_main_sert_aussi(monde):
    from oto_mcp.db import regles_de_comptes
    regles_de_comptes.poser_regle(monde["y"], monde["inst"], subject_user=monde["dehors"],
                                  level="write", acteur=monde["membre"])
    _sert_sans_nom_et_nomme(monde)


def test_sans_regle_il_ne_sert_pas(monde):
    assert _resoudre(monde["dehors"]) is None
    assert _resoudre(monde["dehors"], account="boite-org") is None


def test_un_membre_reste_servi_au_palier_org(monde):
    rc = _resoudre(monde["membre"])
    assert rc is not None and rc.mode == "org"


def test_le_statut_et_le_contexte_voient_le_compte(monde):
    from oto_mcp import access
    from oto_mcp.access import comptes
    _regle_de_reprise(monde)
    sonde = access.preloaded_presence_probe(monde["dehors"], org=monde["y"], groups=[])
    assert access.credential_mode_for(monde["dehors"], "zoho", org=monde["y"], group=None,
                                      probe=sonde) == "elsewhere"
    vu = access.compte_d_ailleurs(monde["dehors"], "zoho", monde["y"], sonde)
    assert list(vu) == ["lent_by_org"] and vu["lent_by_org"].startswith("Preteuse")
    ctx = comptes.comptes_du_contexte(monde["dehors"], monde["y"], None)
    entree = next(e for e in ctx["to_name"] if e["instance"] == f"inst:{monde['inst']}")
    assert entree["lent_by_org"].startswith("Preteuse") and entree["serves_unnamed"]
    # Sans `_project=`, depuis son org : un compte d'une autre org qu'une règle lui
    # ouvre (D1), dit de même.
    ailleurs = comptes.comptes_du_contexte(monde["dehors"], monde["x"], None)
    assert any(e["instance"] == f"inst:{monde['inst']}" and "lent_by_org" in e
               for e in ailleurs["to_name"])
